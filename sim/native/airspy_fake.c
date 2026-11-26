/* A stand-in for libairspy 1.0.10, for running the real detector without an Airspy: the same ABI (compiled against
 * libairspy's own airspy.h, in airspy/), samples from a file instead of the radio, faults on request, and a ledger of
 * every call and transfer. It never touches USB.
 *
 * libairspy's own threading is kept (airspy.c, 1.0.10): a producer stands for the USB transfer thread and puts each
 * buffer into a queue of RAW_BUFFER_COUNT (8); a consumer takes them, hands each to the callback, and stops when the
 * callback returns non-zero. When the queue is full the producer drops the buffer, and the next buffer that gets in
 * carries dropped_samples = dropped buffers x samples per transfer. So a detector whose callback is too slow loses
 * samples here as it would on the car, and says so the same way.
 *
 * Environment (read at airspy_open; see sim/README.md):
 *   SLOT17_SIM_IQ          raw little-endian int16 interleaved I/Q at the rate set, delivered from its sample 0 at each
 *                          start; zeros after its end, or the file again with SLOT17_SIM_LOOP=1. Required: without it
 *                          airspy_open finds no receiver.
 *   SLOT17_SIM_CONTROL     optional JSON: stall_after_samples, drop [[at_sample, transfers], ...], fail {call, rc},
 *                          wedge_after_starts, pace ("realtime" or "fast").
 *   SLOT17_SIM_STATE       optional JSON file {"boot": id, "starts": n}: starts counted across processes, so the
 *   SLOT17_SIM_BOOT        board's wedge (a second start in one power-up fails) can be modelled. A new boot id resets.
 *   SLOT17_SIM_LEDGER      JSONL file: one line per call and per transfer, flushed at once. t_mono is this
 *                          computer's monotonic clock (QueryPerformanceCounter / CLOCK_MONOTONIC), Python's
 *                          time.monotonic() on both.
 *   SLOT17_SIM_LEDGER_EVERY  log every Nth normal transfer (anomalies always).
 *
 * Build: python sim/native/build.py   (zig: airspy_fake.dll on Windows, libairspy_fake.so on Linux)
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "airspy/airspy.h"

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
typedef CRITICAL_SECTION mutex_t;
typedef CONDITION_VARIABLE cond_t;
static void mutex_init(mutex_t *m) { InitializeCriticalSection(m); }
static void mutex_lock(mutex_t *m) { EnterCriticalSection(m); }
static void mutex_unlock(mutex_t *m) { LeaveCriticalSection(m); }
static void cond_init(cond_t *c) { InitializeConditionVariable(c); }
static void cond_wait(cond_t *c, mutex_t *m, int ms) { SleepConditionVariableCS(c, m, (DWORD)ms); }
static void cond_signal(cond_t *c) { WakeAllConditionVariable(c); }
static double now_s(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
/* Sleep() wakes on Windows' 15.6 ms tick, so transfers came up to 20 ms late. A high-resolution waitable timer
 * (Windows 10 1803 and later) wakes within a millisecond; without it, Sleep as before. One timer per thread. */
static void sleep_s(double s) {
    static __thread HANDLE timer;
    static __thread int tried;
    if (s <= 0) return;
    if (!tried) {
        tried = 1;
        timer = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    }
    if (timer) {
        LARGE_INTEGER due;
        due.QuadPart = -(LONGLONG)(s * 1e7);          /* relative, in 100 ns */
        if (SetWaitableTimer(timer, &due, 0, NULL, NULL, FALSE)) {
            WaitForSingleObject(timer, INFINITE);
            return;
        }
    }
    Sleep((DWORD)(s * 1000.0));
}
typedef DWORD (WINAPI *thread_fn)(LPVOID);
static int thread_start(thread_t *t, thread_fn fn, void *arg) {
    *t = CreateThread(NULL, 0, fn, arg, 0, NULL);
    return *t ? 0 : -1;
}
static void thread_join(thread_t t) { WaitForSingleObject(t, INFINITE); CloseHandle(t); }
static void thread_priority(int radio) {
    /* libairspy raises its consumer thread to HIGHEST on Windows; the producer stands for the radio itself,
     * which no busy host delays, so it goes above. */
    SetThreadPriority(GetCurrentThread(), radio ? THREAD_PRIORITY_TIME_CRITICAL : THREAD_PRIORITY_HIGHEST);
}
#define THREAD_RET DWORD WINAPI
#define THREAD_END return 0
static int getpid_(void) { return (int)GetCurrentProcessId(); }
#else
#include <pthread.h>
#include <time.h>
#include <unistd.h>
typedef pthread_t thread_t;
typedef pthread_mutex_t mutex_t;
typedef pthread_cond_t cond_t;
static void mutex_init(mutex_t *m) { pthread_mutex_init(m, NULL); }
static void mutex_lock(mutex_t *m) { pthread_mutex_lock(m); }
static void mutex_unlock(mutex_t *m) { pthread_mutex_unlock(m); }
static void cond_init(cond_t *c) { pthread_cond_init(c, NULL); }
static void cond_wait(cond_t *c, mutex_t *m, int ms) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);                 /* pthread_cond_timedwait's clock */
    t.tv_sec += ms / 1000;
    t.tv_nsec += (long)(ms % 1000) * 1000000L;
    if (t.tv_nsec >= 1000000000L) { t.tv_sec += 1; t.tv_nsec -= 1000000000L; }
    pthread_cond_timedwait(c, m, &t);
}
static void cond_signal(cond_t *c) { pthread_cond_broadcast(c); }
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
typedef void *(*thread_fn)(void *);
static int thread_start(thread_t *t, thread_fn fn, void *arg) { return pthread_create(t, NULL, fn, arg); }
static void thread_join(thread_t t) { pthread_join(t, NULL); }
static void thread_priority(int radio) { (void)radio; }  /* libairspy leaves Linux's priorities alone */
#define THREAD_RET void *
#define THREAD_END return NULL
static int getpid_(void) { return (int)getpid(); }
#endif

/* The process's environment. On Windows straight from the OS: this library's C runtime may not be the caller's, and
 * each keeps its own copy of the environment, so getenv could miss a variable the caller set after loading it. */
static const char *env(const char *name) {
#ifdef _WIN32
    static char values[8][1024];
    static int next;
    char *v = values[next++ % 8];
    DWORD n = GetEnvironmentVariableA(name, v, sizeof values[0]);
    return (n > 0 && n < sizeof values[0]) ? v : NULL;
#else
    return getenv(name);
#endif
}

#define RAW_BUFFER_COUNT 8                 /* libairspy 1.0.10, airspy.c */
#define TRANSFER_SAMPLES 65536             /* complex int16 samples per transfer, packing off (262,144 USB bytes) */
#define MAX_DROPS 64

struct airspy_device { int magic; };       /* opaque to the caller, as in libairspy */

static struct {
    /* settings */
    uint32_t samplerate, freq;
    int sample_type, packing, rf_bias, lna_agc, mixer_agc, lna, mixer, vga;
    /* the scene and the faults */
    char iq_path[1024];
    int loop, fast, every;
    int64_t stall_after;                   /* -1: none */
    int64_t drop_at[MAX_DROPS], drop_n[MAX_DROPS];
    int drops;
    char fail_call[64];
    int fail_rc, wedge_after;
    /* streaming */
    volatile int streaming, stop_requested;
    airspy_sample_block_cb_fn callback;
    void *ctx;
    thread_t producer, consumer;
    mutex_t lock;
    cond_t cv;
    int16_t *queue[RAW_BUFFER_COUNT];
    uint32_t queue_dropped[RAW_BUFFER_COUNT];
    int64_t queue_first[RAW_BUFFER_COUNT];
    double queue_deadline[RAW_BUFFER_COUNT], queue_time[RAW_BUFFER_COUNT];
    int head, tail, count;
    int16_t *output;
    uint32_t dropped_pending;
    /* the ledger */
    FILE *ledger;
    mutex_t ledger_lock;
    int ready;
} S;

static struct airspy_device the_device = {0x5117};

/* --- a little JSON, enough for the control file -------------------------------------------------------------- */
static const char *json_key(const char *text, const char *key) {
    char pat[80];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(text, pat);
    if (!p) return NULL;
    p = strchr(p + strlen(pat), ':');
    if (!p) return NULL;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    return p;
}

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n < 0 || n > (1 << 20)) { fclose(f); return NULL; }
    char *buf = (char *)calloc(1, (size_t)n + 1);
    if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); buf = NULL; }
    fclose(f);
    return buf;
}

/* --- the ledger ---------------------------------------------------------------------------------------------- */
static void ledger(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
#include <stdarg.h>
static void ledger(const char *fmt, ...) {
    if (!S.ledger) return;
    mutex_lock(&S.ledger_lock);
    fprintf(S.ledger, "{\"t_mono\": %.6f, \"pid\": %d, ", now_s(), getpid_());
    va_list ap;
    va_start(ap, fmt);
    vfprintf(S.ledger, fmt, ap);
    va_end(ap);
    fputs("}\n", S.ledger);
    fflush(S.ledger);
    mutex_unlock(&S.ledger_lock);
}

static int injected(const char *call) {
    if (S.fail_call[0] && strcmp(S.fail_call, call) == 0) {
        ledger("\"event\": \"fail\", \"call\": \"%s\", \"rc\": %d", call, S.fail_rc);
        return S.fail_rc;
    }
    return 0;
}

static void setup_once(void) {
    if (S.ready) return;
    S.ready = 1;
    mutex_init(&S.lock);
    mutex_init(&S.ledger_lock);
    cond_init(&S.cv);
    const char *path = env("SLOT17_SIM_LEDGER");
    if (path && *path) S.ledger = fopen(path, "ab");
}

static void read_control(void) {
    S.stall_after = -1;
    S.drops = 0;
    S.fail_call[0] = 0;
    S.fail_rc = 0;
    S.wedge_after = 1;
    S.fast = 0;
    const char *e = env("SLOT17_SIM_LEDGER_EVERY");
    S.every = e && atoi(e) > 0 ? atoi(e) : 1;
    e = env("SLOT17_SIM_LOOP");
    S.loop = e && strcmp(e, "1") == 0;
    const char *path = env("SLOT17_SIM_CONTROL");
    char *text = path && *path ? read_file(path) : NULL;
    if (!text) return;
    const char *v;
    if ((v = json_key(text, "stall_after_samples")) && *v != 'n') S.stall_after = strtoll(v, NULL, 10);
    if ((v = json_key(text, "wedge_after_starts"))) S.wedge_after = atoi(v);
    if ((v = json_key(text, "pace")) && strncmp(v, "\"fast\"", 6) == 0) S.fast = 1;
    if ((v = json_key(text, "fail"))) {
        const char *c = json_key(v, "call"), *r = json_key(v, "rc");
        if (c && *c == '"') {
            const char *end = strchr(c + 1, '"');
            size_t n = end ? (size_t)(end - c - 1) : 0;
            if (n >= sizeof S.fail_call) n = sizeof S.fail_call - 1;
            memcpy(S.fail_call, c + 1, n);
            S.fail_call[n] = 0;
        }
        S.fail_rc = r ? atoi(r) : AIRSPY_ERROR_OTHER;
    }
    if ((v = json_key(text, "drop")) && *v == '[') {
        const char *p = v + 1;
        while (S.drops < MAX_DROPS && (p = strchr(p, '[')) != NULL) {
            char *end;
            S.drop_at[S.drops] = strtoll(p + 1, &end, 10);
            p = strchr(end, ',');
            if (!p) break;
            S.drop_n[S.drops] = strtoll(p + 1, &end, 10);
            S.drops++;
            p = end;
        }
    }
    free(text);
}

/* The board's wedge: starts counted per simulated boot, across processes. 0, or AIRSPY_ERROR_LIBUSB. */
static int wedge_check(void) {
    const char *path = env("SLOT17_SIM_STATE"), *boot = env("SLOT17_SIM_BOOT");
    if (!path || !*path) return 0;
    char saved_boot[128] = "";
    long starts = 0;
    char *text = read_file(path);
    if (text) {
        const char *b = json_key(text, "boot"), *n = json_key(text, "starts");
        if (b && *b == '"') {
            const char *end = strchr(b + 1, '"');
            size_t len = end ? (size_t)(end - b - 1) : 0;
            if (len >= sizeof saved_boot) len = sizeof saved_boot - 1;
            memcpy(saved_boot, b + 1, len);
            saved_boot[len] = 0;
        }
        if (n) starts = strtol(n, NULL, 10);
        free(text);
    }
    if (!boot) boot = "";
    if (strcmp(saved_boot, boot) != 0) starts = 0;
    starts++;
    FILE *f = fopen(path, "wb");
    if (f) {
        fprintf(f, "{\"boot\": \"%s\", \"starts\": %ld}\n", boot, starts);
        fclose(f);
    }
    if (starts > S.wedge_after) {
        ledger("\"event\": \"wedge\", \"boot\": \"%s\", \"start\": %ld, \"rc\": %d", boot, starts, AIRSPY_ERROR_LIBUSB);
        return AIRSPY_ERROR_LIBUSB;
    }
    return 0;
}

/* --- streaming ----------------------------------------------------------------------------------------------- */
static int dropped_here(int64_t first) {
    for (int i = 0; i < S.drops; i++)
        if (first >= S.drop_at[i] && first < S.drop_at[i] + S.drop_n[i] * TRANSFER_SAMPLES) return 1;
    return 0;
}

static THREAD_RET producer_proc(void *arg) {
    (void)arg;
    thread_priority(1);
    FILE *src = fopen(S.iq_path, "rb");
    int16_t *buf = (int16_t *)malloc(2 * TRANSFER_SAMPLES * sizeof(int16_t));
    double period = (double)TRANSFER_SAMPLES / (double)(S.samplerate ? S.samplerate : 6000000);
    double epoch = now_s();
    int64_t seq = 0, first = 0;
    int stalled = 0, ended = 0;
    while (!S.stop_requested && buf) {
        double deadline = epoch + (double)(seq + 1) * period;     /* when this transfer's last sample exists */
        int stall_now = S.stall_after >= 0 && first >= S.stall_after;
        if (!stall_now) {
            /* Read ahead, before the deadline: after waking only the hand-over is left (on the Pi, with the
             * detector's threads busy, a read after waking added to late wake-ups). */
            size_t want = 2 * TRANSFER_SAMPLES, got = 0;
            int failed = !src, rewound = 0;
            while (!failed && got < want) {        /* as many wraps as a short looped file needs */
                size_t n = fread(buf + got, sizeof(int16_t), want - got, src);
                got += n;
                if (got == want) break;
                if (ferror(src) || (rewound && n == 0)) { failed = 1; break; }   /* an error, or emptied under us */
                if (!S.loop) break;                /* the end of the scene: zeros from here, as documented */
                if (fseek(src, 0, SEEK_SET) != 0) { failed = 1; break; }
                rewound = 1;
            }
            if (failed) {                          /* a read error is not the end of the scene */
                ledger("\"event\": \"read_error\", \"first_sample\": %lld", (long long)first);
                break;
            }
            if (got < want) {
                if (!ended && src) {
                    ended = 1;
                    ledger("\"event\": \"scene_end\", \"first_sample\": %lld", (long long)(first + (int64_t)(got / 2)));
                }
                memset(buf + got, 0, (want - got) * sizeof(int16_t));
            }
        }
        if (!S.fast) {
            double wait = deadline - now_s();
            if (wait > 0) sleep_s(wait);
        }
        if (S.stop_requested) break;
        if (stall_now) {
            if (!stalled) {
                stalled = 1;
                ledger("\"event\": \"stall\", \"first_sample\": %lld", (long long)first);
            }
            sleep_s(0.01);                         /* alive and streaming, but nothing comes: the board's wedge */
            continue;
        }
        size_t want = 2 * TRANSFER_SAMPLES;
        mutex_lock(&S.lock);
        if (dropped_here(first)) {
            S.dropped_pending++;
            mutex_unlock(&S.lock);
            ledger("\"event\": \"drop\", \"seq\": %lld, \"first_sample\": %lld, \"why\": \"scheduled\"",
                   (long long)seq, (long long)first);
        } else if (S.count < RAW_BUFFER_COUNT) {
            memcpy(S.queue[S.head], buf, want * sizeof(int16_t));
            S.queue_dropped[S.head] = S.dropped_pending;
            S.queue_first[S.head] = first;
            S.queue_deadline[S.head] = deadline;
            S.queue_time[S.head] = now_s();
            S.dropped_pending = 0;
            S.head = (S.head + 1) % RAW_BUFFER_COUNT;
            S.count++;
            cond_signal(&S.cv);
            mutex_unlock(&S.lock);
        } else {                                  /* the queue is full: the callback is too slow (libairspy) */
            S.dropped_pending++;
            mutex_unlock(&S.lock);
            ledger("\"event\": \"drop\", \"seq\": %lld, \"first_sample\": %lld, \"why\": \"queue_full\"",
                   (long long)seq, (long long)first);
        }
        seq++;
        first += TRANSFER_SAMPLES;
    }
    if (src) fclose(src);
    free(buf);
    THREAD_END;
}

static THREAD_RET consumer_proc(void *arg) {
    (void)arg;
    thread_priority(0);
    int64_t seq = 0;
    double period = (double)TRANSFER_SAMPLES / (double)(S.samplerate ? S.samplerate : 6000000);
    mutex_lock(&S.lock);
    while (!S.stop_requested) {
        while (S.count == 0 && !S.stop_requested) cond_wait(&S.cv, &S.lock, 50);
        if (S.stop_requested) break;
        int slot = S.tail;
        S.tail = (S.tail + 1) % RAW_BUFFER_COUNT;
        uint32_t dropped = S.queue_dropped[slot];
        int64_t first = S.queue_first[slot];
        double deadline = S.queue_deadline[slot], queued = S.queue_time[slot];
        memcpy(S.output, S.queue[slot], 2 * TRANSFER_SAMPLES * sizeof(int16_t));
        mutex_unlock(&S.lock);

        airspy_transfer_t t;
        t.device = &the_device;
        t.ctx = S.ctx;
        t.samples = S.output;
        t.sample_count = TRANSFER_SAMPLES;
        t.dropped_samples = (uint64_t)dropped * (uint64_t)TRANSFER_SAMPLES;
        t.sample_type = (enum airspy_sample_type)S.sample_type;
        double delivered = now_s();
        int rc = S.callback ? S.callback(&t) : 0;
        double took = now_s() - delivered;
        memset(S.output, 0x5A, 2 * TRANSFER_SAMPLES * sizeof(int16_t));   /* poisoned: valid only during the call */
        if (seq % S.every == 0 || dropped || rc || took > period || delivered - deadline > period / 4)
            ledger("\"event\": \"transfer\", \"seq\": %lld, \"first_sample\": %lld, \"samples\": %d, \"dropped\": %llu, "
                   "\"deadline\": %.6f, \"queued\": %.6f, \"delivered\": %.6f, \"callback_s\": %.6f, \"rc\": %d",
                   (long long)seq, (long long)first, TRANSFER_SAMPLES, (unsigned long long)t.dropped_samples,
                   deadline, queued, delivered, took, rc);
        seq++;
        mutex_lock(&S.lock);
        S.count--;
        if (rc != 0) S.stop_requested = 1;        /* libairspy: a non-zero return ends the delivery */
    }
    mutex_unlock(&S.lock);
    THREAD_END;
}

/* --- the API ------------------------------------------------------------------------------------------------- */
#define DEV_OK(d) do { if ((d) != &the_device) return AIRSPY_ERROR_INVALID_PARAM; } while (0)
#define SETTER(name, field, fmt)                                                         \
    ADDAPI int ADDCALL airspy_set_##name(struct airspy_device *d, uint8_t value) {      \
        DEV_OK(d);                                                                       \
        int rc = injected("airspy_set_" #name);                                          \
        if (rc) return rc;                                                               \
        S.field = value;                                                                 \
        ledger("\"event\": \"set_" #name "\", \"value\": " fmt, value);                  \
        return AIRSPY_SUCCESS;                                                           \
    }

ADDAPI void ADDCALL airspy_lib_version(airspy_lib_version_t *v) {
    v->major_version = AIRSPY_VER_MAJOR; v->minor_version = AIRSPY_VER_MINOR; v->revision = AIRSPY_VER_REVISION;
}

ADDAPI int ADDCALL airspy_init(void) {
    setup_once();
    read_control();
    int rc = injected("airspy_init");
    ledger("\"event\": \"init\", \"rc\": %d", rc);
    return rc;
}

ADDAPI int ADDCALL airspy_exit(void) {
    ledger("\"event\": \"exit\"");
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_list_devices(uint64_t *serials, int count) {
    const char *iq = env("SLOT17_SIM_IQ");
    if (!iq || !*iq) return 0;
    if (serials && count > 0) serials[0] = 0x5117000000000017ULL;
    return 1;
}

ADDAPI int ADDCALL airspy_open(struct airspy_device **device) {
    setup_once();
    read_control();
    const char *iq = env("SLOT17_SIM_IQ");
    if (!iq || !*iq) {                           /* never a stand-in for a real receiver by accident */
        ledger("\"event\": \"open\", \"rc\": %d, \"why\": \"SLOT17_SIM_IQ not set\"", AIRSPY_ERROR_NOT_FOUND);
        return AIRSPY_ERROR_NOT_FOUND;
    }
    int rc = injected("airspy_open");
    if (rc) return rc;
    /* The scene must be there and whole before the open succeeds: a missing or short file used to become zeros,
     * which a negative control would pass as a quiet road (found in review, F35). */
    FILE *probe = fopen(iq, "rb");
    long size = -1;
    if (probe) {
        if (fseek(probe, 0, SEEK_END) == 0) size = ftell(probe);
        fclose(probe);
    }
    if (!probe || size <= 0 || size % 4 != 0) {
        const char *why = !probe ? "SLOT17_SIM_IQ can't be read" : size == 0 ? "SLOT17_SIM_IQ is empty"
                          : "SLOT17_SIM_IQ is not whole int16 I/Q samples";
        ledger("\"event\": \"open\", \"rc\": %d, \"why\": \"%s\"", AIRSPY_ERROR_NOT_FOUND, why);
        return AIRSPY_ERROR_NOT_FOUND;
    }
    snprintf(S.iq_path, sizeof S.iq_path, "%s", iq);
    S.samplerate = 6000000;
    S.sample_type = AIRSPY_SAMPLE_FLOAT32_IQ;   /* libairspy's default until set */
    *device = &the_device;
    ledger("\"event\": \"open\", \"rc\": 0, \"iq\": \"%s\", \"stall_after_samples\": %lld, \"drops\": %d, "
           "\"wedge_after_starts\": %d, \"pace\": \"%s\"", "(set)", (long long)S.stall_after, S.drops, S.wedge_after,
           S.fast ? "fast" : "realtime");
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_open_sn(struct airspy_device **device, uint64_t serial_number) {
    (void)serial_number;
    return airspy_open(device);
}

ADDAPI int ADDCALL airspy_close(struct airspy_device *d) {
    DEV_OK(d);
    ledger("\"event\": \"close\"");
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_get_samplerates(struct airspy_device *d, uint32_t *buffer, const uint32_t len) {
    DEV_OK(d);
    static const uint32_t rates[] = {6000000, 3000000};      /* an Airspy Mini's */
    if (len == 0) { buffer[0] = 2; return AIRSPY_SUCCESS; }
    for (uint32_t i = 0; i < len && i < 2; i++) buffer[i] = rates[i];
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_set_samplerate(struct airspy_device *d, uint32_t samplerate) {
    DEV_OK(d);
    int rc = injected("airspy_set_samplerate");
    if (rc) return rc;
    if (samplerate == 0) samplerate = 6000000;                /* indices, as libairspy accepts them */
    else if (samplerate == 1) samplerate = 3000000;
    if (samplerate != 6000000 && samplerate != 3000000) return AIRSPY_ERROR_INVALID_PARAM;
    S.samplerate = samplerate;
    ledger("\"event\": \"set_samplerate\", \"value\": %u", samplerate);
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_set_sample_type(struct airspy_device *d, enum airspy_sample_type sample_type) {
    DEV_OK(d);
    int rc = injected("airspy_set_sample_type");
    if (rc) return rc;
    if (sample_type != AIRSPY_SAMPLE_INT16_IQ) return AIRSPY_ERROR_INVALID_PARAM;  /* the only one the scene is */
    S.sample_type = sample_type;
    ledger("\"event\": \"set_sample_type\", \"value\": %d", (int)sample_type);
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_set_freq(struct airspy_device *d, const uint32_t freq_hz) {
    DEV_OK(d);
    int rc = injected("airspy_set_freq");
    if (rc) return rc;
    S.freq = freq_hz;
    ledger("\"event\": \"set_freq\", \"value\": %u, \"streaming\": %d", freq_hz, S.streaming);
    return AIRSPY_SUCCESS;
}

SETTER(lna_gain, lna, "%u")
SETTER(mixer_gain, mixer, "%u")
SETTER(vga_gain, vga, "%u")
SETTER(lna_agc, lna_agc, "%u")
SETTER(mixer_agc, mixer_agc, "%u")
SETTER(rf_bias, rf_bias, "%u")
SETTER(packing, packing, "%u")

ADDAPI int ADDCALL airspy_start_rx(struct airspy_device *d, airspy_sample_block_cb_fn callback, void *ctx) {
    DEV_OK(d);
    int rc = injected("airspy_start_rx");
    if (!rc) rc = wedge_check();
    if (rc) {
        ledger("\"event\": \"start_rx\", \"rc\": %d", rc);
        return rc;
    }
    if (S.sample_type != AIRSPY_SAMPLE_INT16_IQ || S.packing) {
        ledger("\"event\": \"start_rx\", \"rc\": %d, \"why\": \"the scene is unpacked INT16_IQ only\"",
               AIRSPY_ERROR_INVALID_PARAM);
        return AIRSPY_ERROR_INVALID_PARAM;
    }
    for (int i = 0; i < RAW_BUFFER_COUNT; i++) {
        if (!S.queue[i]) S.queue[i] = (int16_t *)malloc(2 * TRANSFER_SAMPLES * sizeof(int16_t));
    }
    if (!S.output) S.output = (int16_t *)malloc(2 * TRANSFER_SAMPLES * sizeof(int16_t));
    S.head = S.tail = S.count = 0;
    S.dropped_pending = 0;
    S.callback = callback;
    S.ctx = ctx;
    S.stop_requested = 0;
    S.streaming = 1;
    ledger("\"event\": \"start_rx\", \"rc\": 0, \"samplerate\": %u, \"lna\": %d, \"mixer\": %d, \"vga\": %d, "
           "\"lna_agc\": %d, \"mixer_agc\": %d, \"rf_bias\": %d, \"packing\": %d",
           S.samplerate, S.lna, S.mixer, S.vga, S.lna_agc, S.mixer_agc, S.rf_bias, S.packing);
    if (thread_start(&S.consumer, (thread_fn)consumer_proc, NULL) || thread_start(&S.producer, (thread_fn)producer_proc, NULL)) {
        S.streaming = 0;
        return AIRSPY_ERROR_THREAD;
    }
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_stop_rx(struct airspy_device *d) {
    DEV_OK(d);
    if (!S.streaming) return AIRSPY_SUCCESS;
    mutex_lock(&S.lock);
    S.stop_requested = 1;
    cond_signal(&S.cv);
    mutex_unlock(&S.lock);
    thread_join(S.producer);
    thread_join(S.consumer);
    S.streaming = 0;
    ledger("\"event\": \"stop_rx\"");
    return injected("airspy_stop_rx");
}

ADDAPI int ADDCALL airspy_is_streaming(struct airspy_device *d) {
    if (d != &the_device) return 0;
    return S.streaming == 1;                     /* as libairspy: true until stopped, also after a stall */
}

ADDAPI int ADDCALL airspy_board_id_read(struct airspy_device *d, uint8_t *value) {
    DEV_OK(d);
    *value = 0;                                  /* AIRSPY_BOARD_ID_PROTO_AIRSPY */
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_version_string_read(struct airspy_device *d, char *version, uint8_t length) {
    DEV_OK(d);
    snprintf(version, length, "AirSpy MINI v1.0.0-rc10-6 (Slot17 stand-in)");
    return AIRSPY_SUCCESS;
}

ADDAPI int ADDCALL airspy_board_partid_serialno_read(struct airspy_device *d, airspy_read_partid_serialno_t *r) {
    DEV_OK(d);
    r->part_id[0] = 0x6906002B; r->part_id[1] = 0x00000030;
    r->serial_no[0] = 0; r->serial_no[1] = 0; r->serial_no[2] = 0x51170000; r->serial_no[3] = 0x00000017;
    return AIRSPY_SUCCESS;
}

ADDAPI const char *ADDCALL airspy_error_name(enum airspy_error errcode) {
    switch (errcode) {
        case AIRSPY_SUCCESS: return "AIRSPY_SUCCESS";
        case AIRSPY_ERROR_INVALID_PARAM: return "AIRSPY_ERROR_INVALID_PARAM";
        case AIRSPY_ERROR_NOT_FOUND: return "AIRSPY_ERROR_NOT_FOUND";
        case AIRSPY_ERROR_BUSY: return "AIRSPY_ERROR_BUSY";
        case AIRSPY_ERROR_NO_MEM: return "AIRSPY_ERROR_NO_MEM";
        case AIRSPY_ERROR_LIBUSB: return "AIRSPY_ERROR_LIBUSB";
        case AIRSPY_ERROR_THREAD: return "AIRSPY_ERROR_THREAD";
        case AIRSPY_ERROR_STREAMING_THREAD_ERR: return "AIRSPY_ERROR_STREAMING_THREAD_ERR";
        case AIRSPY_ERROR_STREAMING_STOPPED: return "AIRSPY_ERROR_STREAMING_STOPPED";
        default: return "AIRSPY_ERROR_OTHER";
    }
}

ADDAPI const char *ADDCALL airspy_board_id_name(enum airspy_board_id board_id) {
    return board_id == 0 ? "AIRSPY" : "UNKNOWN";
}
