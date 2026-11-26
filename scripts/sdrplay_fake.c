/* A stand-in for the SDRplay API 3.15 library, for testing Slot17's ctypes bindings without SDRplay hardware.
 *
 * Built against SDRplay's own headers (which are SDRplay's, not part of this repository), so it reads and writes
 * the real C structures: every parameter Slot17 sets is checked here, through C, at sdrplay_api_Init. Samples come
 * from a thread of this library's own, like the API's, so the callbacks arrive on a foreign thread through real
 * ctypes trampolines.
 *
 * What it streams is chosen with fake_configure() before Init:
 *   - a ramp (I = n, Q = -n for sample number n, both modulo 2^15), or int16 I/Q read from a file;
 *   - blocks of `block` samples at about real time (6 MSPS), or as fast as possible;
 *   - a gap of `gap_samples` after `gap_after` blocks, a reset flag on block `reset_at`;
 *   - a power-overload event after `overload_at` blocks (Slot17 must acknowledge it);
 *   - a device-removed event after `remove_at` blocks.
 * fake_report() returns what it saw as one line of JSON.
 *
 *   python -m ziglang cc -target x86_64-windows-gnu -shared -O2 -I <headers> sdrplay_fake.c -o sdrplay_fake.dll
 *   cc -shared -fPIC -O2 -I /usr/local/include sdrplay_fake.c -o libsdrplay_fake.so -lpthread
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "sdrplay_api.h"

#ifdef _WIN32
#include <windows.h>
#define EXPORT __declspec(dllexport)
typedef HANDLE thread_t;
static void sleep_us(long us) { Sleep((DWORD)(us / 1000)); }
static double now_s(void) { LARGE_INTEGER f, c; QueryPerformanceFrequency(&f); QueryPerformanceCounter(&c); return (double)c.QuadPart / f.QuadPart; }
#else
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#define EXPORT __attribute__((visibility("default")))
typedef pthread_t thread_t;
static void sleep_us(long us) { usleep(us); }
static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
#endif

static sdrplay_api_DevParamsT dev_params;
static sdrplay_api_RxChannelParamsT rx_a;
static sdrplay_api_DeviceParamsT device_params;
static sdrplay_api_CallbackFnsT callbacks;
static void *cb_context;
static volatile int running, stop_requested;
static thread_t streamer;

static int cfg_block = 1344, cfg_realtime = 1, cfg_gap_after = -1, cfg_gap_samples = 0, cfg_reset_at = -1;
static int cfg_overload_at = -1, cfg_remove_at = -1, cfg_max_blocks = -1, cfg_hw = SDRPLAY_RSP1B_ID;
static short *file_iq; static long file_samples;
static char log_line[4096];
static int n_open, n_close, n_lock, n_unlock, n_select, n_release, n_init, n_uninit, n_ack, n_updates_other;
static long blocks_sent, samples_sent; static int params_ok = -1; static char params_problem[512];

static void reset_params(void) {
    memset(&dev_params, 0, sizeof dev_params); memset(&rx_a, 0, sizeof rx_a);
    dev_params.fsFreq.fsHz = 2000000.0; dev_params.mode = sdrplay_api_ISOCH;
    rx_a.tunerParams.bwType = sdrplay_api_BW_0_200; rx_a.tunerParams.ifType = sdrplay_api_IF_Zero;
    rx_a.tunerParams.loMode = sdrplay_api_LO_Auto; rx_a.tunerParams.gain.gRdB = 50; rx_a.tunerParams.gain.LNAstate = 0;
    rx_a.tunerParams.gain.minGr = sdrplay_api_NORMAL_MIN_GR; rx_a.tunerParams.rfFreq.rfHz = 200000000.0;
    rx_a.tunerParams.dcOffsetTuner.dcCal = 3; rx_a.tunerParams.dcOffsetTuner.trackTime = 1;
    rx_a.tunerParams.dcOffsetTuner.refreshRateTime = 2048;
    rx_a.ctrlParams.dcOffset.DCenable = 1; rx_a.ctrlParams.dcOffset.IQenable = 1;
    rx_a.ctrlParams.decimation.decimationFactor = 1; rx_a.ctrlParams.agc.enable = sdrplay_api_AGC_50HZ;
    rx_a.ctrlParams.agc.setPoint_dBfs = -60;
    device_params.devParams = &dev_params; device_params.rxChannelA = &rx_a; device_params.rxChannelB = NULL;
}

EXPORT void fake_configure(int block, int realtime, int gap_after, int gap_samples, int reset_at, int overload_at,
                           int remove_at, int max_blocks, int hw) {
    cfg_block = block; cfg_realtime = realtime; cfg_gap_after = gap_after; cfg_gap_samples = gap_samples;
    cfg_reset_at = reset_at; cfg_overload_at = overload_at; cfg_remove_at = remove_at; cfg_max_blocks = max_blocks;
    cfg_hw = hw;
}

EXPORT int fake_load_iq(const char *path) {   /* interleaved int16 I/Q */
    FILE *f = fopen(path, "rb"); long bytes; if (!f) return -1;
    fseek(f, 0, SEEK_END); bytes = ftell(f); fseek(f, 0, SEEK_SET);
    free(file_iq); file_iq = (short *)malloc(bytes); file_samples = bytes / 4;
    if (fread(file_iq, 1, bytes, f) != (size_t)bytes) { fclose(f); return -2; }
    fclose(f); return 0;
}

EXPORT const char *fake_report(void) {
    snprintf(log_line, sizeof log_line,
             "{\"open\":%d,\"close\":%d,\"lock\":%d,\"unlock\":%d,\"select\":%d,\"release\":%d,\"init\":%d,"
             "\"uninit\":%d,\"overload_acks\":%d,\"other_updates\":%d,\"blocks\":%ld,\"samples\":%ld,\"params_ok\":%d,"
             "\"params_problem\":\"%s\",\"fs\":%.1f,\"rf\":%.1f,\"bw\":%d,\"if\":%d,\"lo\":%d,\"grdb\":%d,\"lna\":%d,"
             "\"mingr\":%d,\"agc\":%d,\"dc\":%d,\"iq\":%d,\"dec\":%d,\"decf\":%d,\"bias\":%d,\"notch\":%d,\"dab\":%d,"
             "\"ppm\":%.3f,\"mode\":%d}",
             n_open, n_close, n_lock, n_unlock, n_select, n_release, n_init, n_uninit, n_ack, n_updates_other,
             blocks_sent, samples_sent, params_ok, params_problem, dev_params.fsFreq.fsHz,
             rx_a.tunerParams.rfFreq.rfHz, rx_a.tunerParams.bwType, rx_a.tunerParams.ifType, rx_a.tunerParams.loMode,
             rx_a.tunerParams.gain.gRdB, rx_a.tunerParams.gain.LNAstate, rx_a.tunerParams.gain.minGr,
             rx_a.ctrlParams.agc.enable, rx_a.ctrlParams.dcOffset.DCenable, rx_a.ctrlParams.dcOffset.IQenable,
             rx_a.ctrlParams.decimation.enable, rx_a.ctrlParams.decimation.decimationFactor,
             rx_a.rsp1aTunerParams.biasTEnable, dev_params.rsp1aParams.rfNotchEnable,
             dev_params.rsp1aParams.rfDabNotchEnable, dev_params.ppm, dev_params.mode);
    return log_line;
}

/* What Slot17 must have set before Init, read through the C structures. */
static void check_params(void) {
    const char *p = NULL;
    if (dev_params.fsFreq.fsHz != 6000000.0) p = "fsHz";
    else if (rx_a.tunerParams.bwType != sdrplay_api_BW_5_000) p = "bwType";
    else if (rx_a.tunerParams.ifType != sdrplay_api_IF_Zero) p = "ifType";
    else if (rx_a.tunerParams.loMode != sdrplay_api_LO_Auto) p = "loMode";
    else if (rx_a.tunerParams.rfFreq.rfHz < 380e6 || rx_a.tunerParams.rfFreq.rfHz > 430e6) p = "rfHz";
    else if (rx_a.tunerParams.gain.gRdB < 20 || rx_a.tunerParams.gain.gRdB > 59) p = "gRdB";
    else if (rx_a.tunerParams.gain.LNAstate > 9) p = "LNAstate";
    else if (rx_a.tunerParams.gain.minGr != sdrplay_api_NORMAL_MIN_GR) p = "minGr";
    else if (rx_a.ctrlParams.agc.enable != sdrplay_api_AGC_DISABLE) p = "agc";
    else if (rx_a.ctrlParams.dcOffset.DCenable != 1 || rx_a.ctrlParams.dcOffset.IQenable != 1) p = "dcOffset";
    else if (rx_a.ctrlParams.decimation.enable != 0) p = "decimation";
    else if (rx_a.rsp1aTunerParams.biasTEnable != 0) p = "biasT";
    else if (dev_params.rsp1aParams.rfNotchEnable != 0 || dev_params.rsp1aParams.rfDabNotchEnable != 0) p = "notch";
    else if (dev_params.ppm != 0.0) p = "ppm";
    params_ok = p == NULL; snprintf(params_problem, sizeof params_problem, "%s", p ? p : "");
}

#ifdef _WIN32
static DWORD WINAPI stream_main(LPVOID unused)
#else
static void *stream_main(void *unused)
#endif
{
    short *xi = (short *)malloc(sizeof(short) * cfg_block), *xq = (short *)malloc(sizeof(short) * cfg_block);
    sdrplay_api_StreamCbParamsT p; sdrplay_api_EventParamsT ev;
    unsigned int first = 0xFFFFF000u;          /* wraps early, as a long session would */
    long pos = 0; double t0 = now_s(); int k, reset;
    (void)unused;
    for (blocks_sent = 0; !stop_requested && (cfg_max_blocks < 0 || blocks_sent < cfg_max_blocks); blocks_sent++) {
        for (k = 0; k < cfg_block; k++) {
            if (file_iq) { long s = (pos + k) % file_samples; xi[k] = file_iq[2 * s]; xq[k] = file_iq[2 * s + 1]; }
            else { xi[k] = (short)((first + k) & 0x7FFF); xq[k] = (short)(-(short)((first + k) & 0x7FFF)); }
        }
        pos += cfg_block;
        memset(&p, 0, sizeof p); p.firstSampleNum = first; p.numSamples = (unsigned int)cfg_block;
        reset = blocks_sent == 0 || blocks_sent == cfg_reset_at;
        callbacks.StreamACbFn(xi, xq, &p, (unsigned int)cfg_block, (unsigned int)reset, cb_context);
        samples_sent += cfg_block;
        first += (unsigned int)cfg_block;
        if (blocks_sent == cfg_gap_after) { first += (unsigned int)cfg_gap_samples; pos += cfg_gap_samples; }
        if (blocks_sent == cfg_overload_at) {
            memset(&ev, 0, sizeof ev); ev.powerOverloadParams.powerOverloadChangeType = sdrplay_api_Overload_Detected;
            callbacks.EventCbFn(sdrplay_api_PowerOverloadChange, sdrplay_api_Tuner_A, &ev, cb_context);
        }
        if (blocks_sent == cfg_remove_at) {
            memset(&ev, 0, sizeof ev);
            callbacks.EventCbFn(sdrplay_api_DeviceRemoved, sdrplay_api_Tuner_A, &ev, cb_context);
            break;
        }
        if (cfg_realtime) {
            double ahead = (double)samples_sent / 6e6 - (now_s() - t0);
            if (ahead > 0.002) sleep_us((long)(ahead * 1e6));
        }
    }
    free(xi); free(xq); running = 0;
    return 0;
}

EXPORT sdrplay_api_ErrT sdrplay_api_Open(void) {
    /* Each session starts from nothing: counters, the stream's position and the parameters. */
    n_open = n_close = n_lock = n_unlock = n_select = n_release = n_init = n_uninit = n_ack = n_updates_other = 0;
    blocks_sent = samples_sent = 0; params_ok = -1; params_problem[0] = 0;
    n_open++; reset_params(); return sdrplay_api_Success;
}
EXPORT sdrplay_api_ErrT sdrplay_api_Close(void) { n_close++; return sdrplay_api_Success; }
EXPORT sdrplay_api_ErrT sdrplay_api_ApiVersion(float *apiVer) { *apiVer = SDRPLAY_API_VERSION; return sdrplay_api_Success; }
EXPORT sdrplay_api_ErrT sdrplay_api_LockDeviceApi(void) { n_lock++; return sdrplay_api_Success; }
EXPORT sdrplay_api_ErrT sdrplay_api_UnlockDeviceApi(void) { n_unlock++; return sdrplay_api_Success; }
EXPORT sdrplay_api_ErrT sdrplay_api_GetDevices(sdrplay_api_DeviceT *devices, unsigned int *numDevs, unsigned int maxDevs) {
    if (maxDevs < 1) return sdrplay_api_InvalidParam;
    memset(devices, 0, sizeof *devices);
    snprintf(devices[0].SerNo, sizeof devices[0].SerNo, "FAKE%08X", 0x1B1Bu);
    devices[0].hwVer = (unsigned char)cfg_hw; devices[0].tuner = sdrplay_api_Tuner_A; devices[0].valid = 1;
    devices[0].dev = (HANDLE)&dev_params;
    *numDevs = 1; return sdrplay_api_Success;
}
EXPORT sdrplay_api_ErrT sdrplay_api_SelectDevice(sdrplay_api_DeviceT *device) {
    n_select++; return device && device->dev == (HANDLE)&dev_params ? sdrplay_api_Success : sdrplay_api_InvalidParam;
}
EXPORT sdrplay_api_ErrT sdrplay_api_ReleaseDevice(sdrplay_api_DeviceT *device) { n_release++; (void)device; return sdrplay_api_Success; }
EXPORT const char *sdrplay_api_GetErrorString(sdrplay_api_ErrT err) { return err == sdrplay_api_Success ? "Success" : "fake error"; }
EXPORT sdrplay_api_ErrorInfoT *sdrplay_api_GetLastError(sdrplay_api_DeviceT *device) { (void)device; return NULL; }
EXPORT sdrplay_api_ErrT sdrplay_api_GetDeviceParams(HANDLE dev, sdrplay_api_DeviceParamsT **deviceParams) {
    if (dev != (HANDLE)&dev_params) return sdrplay_api_InvalidParam;
    *deviceParams = &device_params; return sdrplay_api_Success;
}
EXPORT sdrplay_api_ErrT sdrplay_api_Init(HANDLE dev, sdrplay_api_CallbackFnsT *callbackFns, void *cbContext) {
    n_init++;
    if (dev != (HANDLE)&dev_params || !callbackFns || !callbackFns->StreamACbFn || !callbackFns->EventCbFn)
        return sdrplay_api_InvalidParam;
    check_params();
    if (!params_ok) return sdrplay_api_InvalidParam;
    callbacks = *callbackFns; cb_context = cbContext; dev_params.samplesPerPkt = (unsigned int)cfg_block;
    running = 1; stop_requested = 0;
#ifdef _WIN32
    streamer = CreateThread(NULL, 0, stream_main, NULL, 0, NULL);
#else
    pthread_create(&streamer, NULL, stream_main, NULL);
#endif
    return sdrplay_api_Success;
}
EXPORT sdrplay_api_ErrT sdrplay_api_Uninit(HANDLE dev) {
    (void)dev; n_uninit++; stop_requested = 1;
#ifdef _WIN32
    WaitForSingleObject(streamer, 5000); CloseHandle(streamer);
#else
    pthread_join(streamer, NULL);
#endif
    return sdrplay_api_Success;
}
EXPORT sdrplay_api_ErrT sdrplay_api_Update(HANDLE dev, sdrplay_api_TunerSelectT tuner, sdrplay_api_ReasonForUpdateT reason,
                                           sdrplay_api_ReasonForUpdateExtension1T ext) {
    (void)tuner; (void)ext;
    if (dev != (HANDLE)&dev_params) return sdrplay_api_InvalidParam;
    if (reason == sdrplay_api_Update_Ctrl_OverloadMsgAck) n_ack++; else n_updates_other++;
    return sdrplay_api_Success;
}
