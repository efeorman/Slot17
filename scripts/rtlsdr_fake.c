/* A stand-in for librtlsdr with two (or more) pretend RTL-SDRs, for testing Slot17's receiver code through the real
 * ctypes bindings: argument conversion, the device handle written through rtlsdr_dev_t **, string and gain buffers
 * filled by C, and rtlsdr_read_async calling back from C on the caller's thread until rtlsdr_cancel_async.
 *
 * It is compiled against librtlsdr's own header, rtl-sdr.h, so a prototype that disagrees with the header does not
 * compile. The header is osmocom's (Debian: librtlsdr-dev; or include/ of https://github.com/osmocom/rtl-sdr) and
 * is not part of this repository.
 *
 *   Linux:   cc -O2 -shared -fPIC -Drtlsdr_EXPORTS -I /usr/include -o librtlsdr_fake.so scripts/rtlsdr_fake.c
 *   Windows: python -m ziglang cc -O2 -shared -target x86_64-windows-gnu -I DIR -o rtlsdr_fake.dll scripts/rtlsdr_fake.c
 *
 * Then: python scripts/rtlsdr_fake_test.py LIB
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
/* The header's export macro, as it is for an MSVC build of the DLL (rtl-sdr_export.h): the functions exported. */
#define RTLSDR_EXPORT_H
#define RTLSDR_API __declspec(dllexport)
#endif
#include "rtl-sdr.h"

#ifdef _WIN32
#include <windows.h>
#define FAKE_API __declspec(dllexport)
static double now_s(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}
static void sleep_s(double s) { if (s > 0) Sleep((DWORD)(s * 1000.0)); }
#else
#include <time.h>
#define FAKE_API __attribute__((visibility("default")))
static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec * 1e-9;
}
static void sleep_s(double s) {
    if (s <= 0) return;
    struct timespec t = {(time_t)s, (long)((s - (double)(time_t)s) * 1e9)};
    nanosleep(&t, NULL);
}
#endif

#define MAX_STICKS 4
static const int R820T_GAINS[] = {0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297,
                                  328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496};

struct rtlsdr_dev {                       /* opaque to the caller, as in librtlsdr */
    int index;
};

typedef struct {
    char serial[256];
    int tuner;
    uint32_t rate, freq;
    int corr, gain, manual, agc, bias, opened, closed, cancels, reads, buf_num, buf_len;
    volatile int cancel;
    uint8_t *data;
    size_t len;
    size_t fail_after;                    /* 0: never */
    size_t loss_at, loss_bytes;           /* skip loss_bytes of data (and that much time) once, after loss_at bytes */
    size_t sent;
    struct rtlsdr_dev dev;
} stick_t;

static stick_t sticks[MAX_STICKS];
static int stick_count = 2;
static int open_rc = 0;

/* --- control, for the test ------------------------------------------------------------------------------- */
FAKE_API void fake_configure(int count, const char *serials, int tuner, int open_result) {
    /* serials: count serials, each 8 characters, one after the other */
    stick_count = count < MAX_STICKS ? count : MAX_STICKS;
    open_rc = open_result;
    for (int i = 0; i < MAX_STICKS; i++) {
        free(sticks[i].data);
        memset(&sticks[i], 0, sizeof sticks[i]);
        sticks[i].tuner = tuner;
        sticks[i].dev.index = i;
        if (i < stick_count) {
            memcpy(sticks[i].serial, serials + 8 * i, 8);
            sticks[i].serial[8] = 0;
        }
    }
}

FAKE_API void fake_load(int index, const uint8_t *data, size_t len) {
    stick_t *s = &sticks[index];
    free(s->data);
    s->data = (uint8_t *)malloc(len);
    memcpy(s->data, data, len);
    s->len = len;
}

FAKE_API void fake_fail_after(int index, size_t bytes) { sticks[index].fail_after = bytes; }

FAKE_API void fake_loss(int index, size_t at, size_t bytes) { sticks[index].loss_at = at; sticks[index].loss_bytes = bytes; }

/* rate, freq, corr, gain, manual, agc, bias, opened, closed, cancels, reads, buf_num, buf_len, sent/1024 */
FAKE_API void fake_report(int index, int64_t *out) {
    stick_t *s = &sticks[index];
    int64_t v[14] = {s->rate, s->freq, s->corr, s->gain, s->manual, s->agc, s->bias, s->opened, s->closed,
                     s->cancels, s->reads, s->buf_num, s->buf_len, (int64_t)(s->sent / 1024)};
    memcpy(out, v, sizeof v);
}

/* --- librtlsdr ------------------------------------------------------------------------------------------- */
static stick_t *of(rtlsdr_dev_t *dev) { return dev ? &sticks[dev->index] : NULL; }

uint32_t rtlsdr_get_device_count(void) { return (uint32_t)stick_count; }

int rtlsdr_get_device_usb_strings(uint32_t index, char *manufact, char *product, char *serial) {
    if (index >= (uint32_t)stick_count) return -2;
    if (manufact) strcpy(manufact, "Realtek");
    if (product) strcpy(product, "RTL2838UHIDIR");
    if (serial) strcpy(serial, sticks[index].serial);
    return 0;
}

int rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index) {
    if (index >= (uint32_t)stick_count) return -1;
    if (open_rc) return open_rc;
    sticks[index].opened++;
    *dev = &sticks[index].dev;
    return 0;
}

int rtlsdr_close(rtlsdr_dev_t *dev) { stick_t *s = of(dev); if (!s) return -1; s->closed++; return 0; }
int rtlsdr_set_sample_rate(rtlsdr_dev_t *dev, uint32_t rate) { of(dev)->rate = rate; return 0; }
uint32_t rtlsdr_get_sample_rate(rtlsdr_dev_t *dev) { return of(dev)->rate; }
int rtlsdr_set_center_freq(rtlsdr_dev_t *dev, uint32_t freq) { of(dev)->freq = freq; return 0; }
uint32_t rtlsdr_get_center_freq(rtlsdr_dev_t *dev) { return of(dev)->freq; }

int rtlsdr_set_freq_correction(rtlsdr_dev_t *dev, int ppm) {
    stick_t *s = of(dev);
    if (s->corr == ppm) return -2;        /* as librtlsdr: already that value */
    s->corr = ppm;
    return 0;
}

enum rtlsdr_tuner rtlsdr_get_tuner_type(rtlsdr_dev_t *dev) { return (enum rtlsdr_tuner)of(dev)->tuner; }

int rtlsdr_get_tuner_gains(rtlsdr_dev_t *dev, int *gains) {
    int n = (int)(sizeof R820T_GAINS / sizeof R820T_GAINS[0]);
    (void)dev;
    if (gains) memcpy(gains, R820T_GAINS, sizeof R820T_GAINS);
    return n;
}

int rtlsdr_set_tuner_gain_mode(rtlsdr_dev_t *dev, int manual) { of(dev)->manual = manual; return 0; }
int rtlsdr_set_tuner_gain(rtlsdr_dev_t *dev, int gain) { of(dev)->gain = gain; return 0; }
int rtlsdr_get_tuner_gain(rtlsdr_dev_t *dev) { return of(dev)->gain; }
int rtlsdr_set_agc_mode(rtlsdr_dev_t *dev, int on) { of(dev)->agc = on; return 0; }
int rtlsdr_set_bias_tee(rtlsdr_dev_t *dev, int on) { of(dev)->bias = on; return 0; }
int rtlsdr_reset_buffer(rtlsdr_dev_t *dev) { (void)dev; return 0; }

int rtlsdr_cancel_async(rtlsdr_dev_t *dev) { stick_t *s = of(dev); s->cancel = 1; s->cancels++; return 0; }

int rtlsdr_read_async(rtlsdr_dev_t *dev, rtlsdr_read_async_cb_t cb, void *ctx, uint32_t buf_num, uint32_t buf_len) {
    stick_t *s = of(dev);
    uint8_t *buf;
    size_t pos = 0;
    double t0, late = 0.0;
    if (!s || !cb || buf_len == 0 || buf_len % 512) return -1;
    s->reads++;
    s->buf_num = (int)buf_num;
    s->buf_len = (int)buf_len;
    buf = (uint8_t *)malloc(buf_len);
    t0 = now_s();
    while (!s->cancel) {
        if (s->fail_after && s->sent >= s->fail_after) { free(buf); return -5; }
        if (s->loss_at && s->sent >= s->loss_at) {
            pos = s->len ? (pos + s->loss_bytes) % s->len : 0;
            late += (double)s->loss_bytes / 2.0 / (double)s->rate;
            s->loss_at = 0;
        }
        if (!s->len) {
            memset(buf, 128, buf_len);
        } else {
            for (size_t done = 0; done < buf_len;) {      /* whole runs, so the stand-in costs next to nothing */
                size_t run = s->len - pos < buf_len - done ? s->len - pos : buf_len - done;
                memcpy(buf + done, s->data + pos, run);
                done += run;
                pos = (pos + run) % s->len;
            }
        }
        sleep_s(t0 + late + (double)(s->sent + buf_len) / 2.0 / (double)s->rate - now_s());
        if (s->cancel) break;
        cb(buf, buf_len, ctx);
        s->sent += buf_len;
    }
    free(buf);
    return 0;
}
