# sim/native: stand-in receivers for running the real detector without hardware

The first piece of the hardware-free test environment. It was designed with a second reviewer on
24 November 2026; the plan is in the private archive's review record.

## The Airspy stand-in (`airspy_fake.c`)

A shared library with libairspy 1.0.10's ABI. It is compiled against upstream's own `airspy.h`, kept in
`airspy/` with its BSD licence. The detector loads it through `SLOT17_AIRSPY_LIB`; nothing in `p0/` changes.

- **Samples:**
  - **Source:** a file (`SLOT17_SIM_IQ`, little-endian int16 interleaved I/Q at the rate the detector sets),
    delivered from its sample 0 at each start.
  - **Delivery:** in transfers of 65,536 samples, paced from an absolute monotonic start.
  - **After the file ends:** zeros, or the file again (`SLOT17_SIM_LOOP=1`).
- **libairspy's own threading:** a producer, standing for the USB thread, and a consumer that calls back, with
  libairspy's queue of 8 buffers between them.
  - When the queue is full a buffer is dropped, and the next one carries `dropped_samples`, as libairspy reports
    it. So a detector whose callback is too slow loses samples here as it would on the car.
  - A non-zero return from the callback stops delivery.
  - The consumer runs at libairspy's priority (HIGHEST on Windows). The producer runs above it, because it stands
    for the radio, which no busy host delays.
- **Faults** (`SLOT17_SIM_CONTROL`, a JSON file):
  - `stall_after_samples`: nothing comes while the streaming flag stays up, as with this board's wedge.
  - `drop`: transfers lost at given samples.
  - `fail`: one call returns an error. The calls that take it: `airspy_init`, `airspy_open`,
    `airspy_set_samplerate`, `airspy_set_sample_type`, `airspy_set_freq`, the gain, AGC, bias and packing
    setters, `airspy_start_rx` and `airspy_stop_rx` (not the getters, `airspy_close` or `airspy_exit`).
  - `wedge_after_starts` with `SLOT17_SIM_STATE` and `SLOT17_SIM_BOOT`: a second start in one simulated power-up
    fails, across processes.
  - `pace`: `fast` for pure tests.
- **The ledger** (`SLOT17_SIM_LEDGER`): one JSON line per call and per transfer, on the clock Python's
  `time.monotonic()` reads (QueryPerformanceCounter, CLOCK_MONOTONIC).
  - A transfer line has `deadline` (when its last sample exists), `queued` (when the stand-in put it in the
    queue) and `delivered` (when the callback got it).
  - So `queued − deadline` is the stand-in's own lateness, and `delivered − queued` is the host's.
- **It never touches USB.** Without `SLOT17_SIM_IQ` it finds no receiver, so it can't stand in for a real one
  by accident.
- **A scene it can't play whole is refused at `airspy_open`** (missing, unreadable, empty, or not whole int16
  I/Q samples), with the reason in the ledger. A looped file fills every transfer, however short it is. A read
  error ends delivery (`read_error` in the ledger) instead of handing over zeros. Found in the review's fourth
  round (F35): a missing file used to become zeros, which a negative control would pass as quiet.

## Running it

    python -m pip install ziglang        # once; zig builds for Windows, Linux x86-64 and the Pi's aarch64
    python sim/native/build.py           # sim/native/build/airspy_fake.dll (or libairspy_fake.so)
    python sim/native/test_airspy_fake.py sim/native/build/airspy_fake.dll
    python sim/native/test_watch_on_airspy_fake.py sim/native/build/airspy_fake.dll

These are not in `tests/`. The updater tests a release in a copy that has `p0/` and `tests/` only.

## What it has shown (24 November 2026, on the development PC)

- **The contract,** through the detector's own receiver class and bindings (`test_airspy_fake.py`): 11 of 11.
- **The real `watch.py`,** run as its own process with nothing patched (`test_watch_on_airspy_fake.py`):
  - **Bursts:** generated bursts on channels 1, 99, 100 and 198 were recognised there and on no other channel:
    10 matches on each of the four, 40 candidates, 23 windows, 0 samples dropped. The receiver was opened
    once.
  - **A stall** ends the session as a stall (exit 75), still with one opening.
  - **A board wedged at its second start** is refused once and not tried again.
- **Timing** over 556 transfers:
  - the stand-in's own lateness: median 0.48 ms, p99 1.11 ms, worst 2.44 ms, all inside a quarter of a
    transfer (2.73 ms);
  - the host's delay to the callback: median 0.04 ms, worst 3.0 ms.
  - With `Sleep()` instead of a high-resolution timer, transfers had come up to 20 ms late.

## What it doesn't show

- Anything about real RF, or the Airspy's own behaviour beyond what libairspy's source and our board's
  documented wedge say.
- systemd, the publisher and the updater: the next increments. The supervisor is run by `sim/run.py` (see
  `sim/README.md`).
- Timing on the Pi. The library builds for aarch64; it hasn't run there yet.
