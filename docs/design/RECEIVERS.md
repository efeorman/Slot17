# Receivers: the Airspy Mini, the SDRplay RSP1B and RSP1A, and a pair of RTL-SDRs

**State, 26 November 2026: released in 1.7.0.** The Airspy Mini is the receiver Slot17 has been driven with; through
this receiver layer since 26 November. Support for the SDRplay RSP1B (and RSP1A) and for a pair of RTL-SDRs is tested
against the receivers' own header files, their real libraries where that was possible without installing them,
and stand-ins that behave like the hardware. The checks to run with each receiver are listed under
[With the hardware](#with-the-hardware).

## Which receiver, and why it is never guessed

`setup.sh car` chooses the receiver once, from the USB IDs it sees (or `--receiver KIND`), installs what that
receiver needs, and keeps the choice in `~/.config/slot17-receiver.json`. With no such file the receiver is the
Airspy Mini, so a car computer set up before this change behaves exactly as before. The detector never opens a
receiver to find out what it is: that would be a second session, which the Airspy Mini does not survive.

| Kind | Receiver | USB ID | What it needs installed | Tuning in the settings file (`"gain"`) |
|---|---|---|---|---|
| `airspy` | Airspy Mini | 1d50:60a1 | `airspy`, `libairspy0` (Debian) | none (gains on the command line, as before) |
| `rsp1b` | SDRplay RSP1B | 1df7:3050 | SDRplay API 3.15.2 (SDRplay's installer) | `lna_state` 0 to 9 (2), `if_gain_reduction_db` 20 to 59 (40) |
| `rsp1a` | SDRplay RSP1A | 1df7:3000 | the same | the same |
| `rtlsdr2` | two RTL-SDRs | 0bda:2838, 0bda:2832 | `librtlsdr0`, `rtl-sdr` (Debian) | `tuner_gain_db10` (297), `rate_khz` 2560 or 2880, `ppm_a`, `ppm_b` |

Only the chosen receiver's code is loaded (`p0/receiver.py`, `backend()`): an Airspy car computer never imports
SDRplay or RTL-SDR code, and the other way round (tested). Every receiver delivers the same thing to the
detector: 6 MSPS int16 I/Q centred on 382.5 MHz, the 200 channels of 380 to 385 MHz. Everything after that is
the same detector, unchanged.

Costs, measured on the Raspberry Pi 5: the Airspy as before. The RSP1B adds the SDRplay API (a few MB, a
service of SDRplay's) and its callbacks; their Python side was cut from 19 % to 8 % of a core by batching. The
RTL pair adds two Debian packages (under 1 MB), 40 MB of memory for the two sticks' waiting rooms, and 38 % of a
core for joining the two halves (below).

## The Airspy Mini

Unchanged. Its code moved from `watch.py` into `p0/receiver_airspy.py` without a change of order or setting:
a test replays the exact sequence of library calls of 1.6.0 (`tests/test_receivers.py`, `AirspyUnchanged`),
and every earlier test passes unaltered.

## The SDRplay RSP1B and RSP1A

**Driver.** SDRplay's own API, 3.15, closed source. `deploy/install-sdrplay.sh` downloads SDRplay's installer
from sdrplay.com (`SDRplay_RSP_API-Linux-3.15.2.run`), checks its SHA-256 (`3a97ca76...`) before any of it runs,
and runs SDRplay's own `install_lib.sh`. That installs the library in `/usr/local/lib`, a service of SDRplay's in
`/opt/sdrplay_api` (run as **root** by systemd as `sdrplay.service`, started at every power-up), and USB rules.
Its licence forbids passing the API on, so it is never in this repository or in a Slot17 release, and installing
it means accepting SDRplay's licence: the installer shows it and asks, or `--accept-sdrplay-licence` says you
have read it. **Section 6 of that licence says the API may send details of the receiver to SDRplay's server.**
The ARM64 build works on the Pi 5's 16 KB memory pages (its segments are aligned to 64 KB).

**Settings** (`p0/receiver_sdrplay.py`, in SDRplay's own order: Open, ApiVersion, LockDeviceApi, GetDevices,
SelectDevice, UnlockDeviceApi, GetDeviceParams, parameters, Init; at the end Uninit, ReleaseDevice, Close):

- 6 MSPS, zero IF, 5 MHz bandwidth. 6 MSPS is still the RSP1B's native 14-bit range (2 to 6.048 MSPS). Zero IF
  puts the DC spike on 382.5 MHz, between channels 99 and 100; DC and IQ correction stay on.
- AGC off and a fixed gain: the noise floor and "Rising" compare levels over minutes, so the gain must not move.
- Bias-T off (no power up the antenna cable), the broadcast notch filters off, ppm 0 (the frequency is corrected
  from `calibration.json`, as for the Airspy).
- Only API 3.15 is accepted. Before anything is written, the parameters the API hands over are checked for
  values that would mean the structures are laid out differently.

**Samples** come as two arrays (I and Q), about 4,500 callbacks a second. Each callback only copies them; every
16,384 samples they are interleaved and handed on together. Lost samples are found from `firstSampleNum`, the
running number of a callback's first sample. Overloads are counted and acknowledged as the API requires. A
removed receiver ends the session.

**The end of a session** (second review, 24 November 2026):
- Once closing begins, a callback hands nothing on and is counted (`late_callbacks`).
- What is still waiting is handed on only after a successful Uninit. After a failed one, it is counted as
  lost, and the receiver object, with its callbacks and buffers, is kept until the process ends: the API's
  thread may still call it.
- A last batch that can't be handed on is counted as lost too.
- A second close does nothing.

**How it is verified, without an RSP1B:**

| Check | Result |
|---|---|
| Every structure's size and every field's offset, as ctypes lays them out, compiled as static assertions against SDRplay's 3.15 headers for 64-bit ARM and x86 (`scripts/verify_sdrplay.py`) | 145 of 145 agree on both |
| Every bound function's arguments and result, parsed from the headers | 14 of 14 agree |
| A C stand-in for the API, built against the same headers, calling back from its own thread (`scripts/sdrplay_fake.c`, `sdrplay_fake_test.py`) | 14 of 14 checks, on Windows and on the Pi, including the whole detector recognising bursts |
| SDRplay's installer on the Pi: downloaded, checksum, unpacked, nothing run (`install-sdrplay.sh --download-only`) | works; not installed, which would accept the licence |

## A pair of RTL-SDRs

One RTL-SDR delivers 2.56 MS/s without losing samples, about 2.5 MHz; the band is 5 MHz. So two sticks
share it: **stick A** listens to 381.25 MHz and **stick B** to 383.75 MHz. `p0/rtl_combine.py` joins them into
the 6 MSPS stream.

**Joining.** Every 2 ms, each stick's samples (with the 1 ms before them) go through an FFT. They are moved to
their place in the 6 MSPS band, 1.25 MHz below or above 382.5 MHz. That is a whole number of bins, and a whole
number of turns per block, so the phase runs on from block to block. Then they are weighted, and an inverse FFT
gives 6 MSPS (overlap-save: 0.5 ms at each end of each result is discarded, so block edges leave no trace, and
the joined stream is 0.5 ms behind the sticks). The session's start is dated 0.5 ms earlier to match
(`Sink.first(n, delay_s)`; until the second review it wasn't, and events were stamped 0.5 ms late).
- **The block edges, corrected on 24 November 2026.** The weighting is zero-phase, so it reaches both ways in time
  and wraps round both ends of a block. Until that date the whole 1 ms was discarded from the start, so the end
  of every block kept the wrap-round.
  - **How it was found:** a code review. It was measured against an independent reference: many tones off the
    FFT's bins, evaluated exactly.
  - **What it was:** in the last 16 samples of each 2 ms block, an error larger than the signal itself, against
    −47 dB everywhere else.
  - **Now:** the same error everywhere in the block, at the same cost.
  - **Why the tests missed it:** they had compared the stitcher with itself, or with what was detected.

- Stick A gives 380.0 to 382.5 MHz and stick B 382.5 to 385.0 MHz, handing over in 2 kHz at 382.5 MHz. That is
  the boundary between channels 99 and 100, so **no channel is made of both sticks**. Their clocks never need to
  agree to the sample.
- Each stick's DC spike (381.25 and 383.75 MHz) falls between two channels too. The one bin on it is left out,
  which costs no channel anything.
- Not 2.4 MS/s: with two simulated sticks, six channels fell outside. Bursts just beyond a stick's reach folded
  back and were recognised on the wrong channels (3 to 5, 195 to 196). At 2.56 and 2.88 MS/s all 200 channels
  are covered.

**Each stick, once per session:** sample rate, ppm correction (librtlsdr corrects the tuner and the sample rate
from it), centre frequency, manual tuner gain (the nearest the tuner offers), the RTL2832's AGC off, bias-T
off, buffers reset; then `rtlsdr_read_async` on a thread of its own. Stick A is the one whose USB serial sorts
first. Many sticks share the factory serial 00000001; then which is A can change between starts. That only
matters if they need different ppm corrections, and then the session refuses to start and says how to change a
serial (`rtl_eeprom -s 00000002`, with only that stick plugged in; it writes to the stick, so it is yours to do).

**Two crystals.** The two streams must not drift apart. Every USB transfer is timed when it arrives. The earliest
arrivals over 5 s, less the samples counted, give each stick's start, and their difference is how far apart the
halves are. Past 5 ms, one stick's waiting samples are skipped until they agree again. That is a
**realignment**: counted in the status, not a fault. At a 2 ppm difference it happens every 40 minutes or
so, and costs nothing: the faster crystal only has more samples. Skipped samples are still lost to the
channels in that half; a burst under way there loses its part, as with any loss.

**Lost samples.** librtlsdr does not report them, so the same timing finds them.
- **Finding them:** a stick whose arrivals fall 20 ms or more further behind than the other's has lost that many.
  It is reported as dropped samples, like the Airspy's, up to 5 s after the loss. **Losses shorter than 20 ms
  are not seen.**
- **What the joined stream has lost: one rule** (second review, 24 November 2026).
  - Each stick passes over samples it never gives the join: overwritten unread, skipped to realign, a skip
    still owed, and lost on USB.
  - Once the halves agree again, both have passed over the same stretch of time. The smaller of the two
    totals is the joined stream's loss, and that is what is reported (`joined_lost_samples`).
  - The smaller also leaves out the skips that only keep a faster crystal in step.
  - It is checked against the test's own count: what a stick delivered, less what was joined and what
    still waits.
- **What is not counted:** a computer that is merely busy delays both sticks alike.
- **What can't be told apart:**
  - **Queueing from loss:** arrival times can't tell a loss from one stick's transfers queueing behind the
    other's. libusb can hold about 0.3 s of them, so that stick's reader would have to stay 20 ms late
    throughout 5 s.
  - **If it happens:** it causes a realignment there and back, and both halves pass over the same stretch.
    That stretch is reported as lost: it is gone from the joined stream. This holds whether or not the
    queueing was long enough to count as a loss: a reader 15 ms late for 7 s costs 15 ms. Until the second
    review that went unreported, and every later timestamp was 15 ms off.
  - **Where it is marked:** a loss is marked where it is found, not where it happened. Timestamps between the
    two are early by the whole loss, however long it is, for up to 5 s. (I first wrote "20 to 100 ms": the
    review pointed out that nothing in the code bounds it.)
- **Waiting rooms overflowing:** when the joining falls more than 4 s behind, a waiting room overwrites its
  oldest samples.
  - **Both overflowing at once:** over the same stretch, so it is lost once. Until 24 November 2026 the sum
    was reported, twice the time.
  - **One, and later the other:** for example while the other stick's reader stalls. Both stretches are
    lost. The larger of the two totals, reported between the two reviews, counted one of them.
  - **A stick running behind:** `backlog_max_ms` shows one stick's reader running behind; `backlog_ms` is the
    smaller of the two. Both are read at every attempt to join, so a stick that delivers nothing shows too.

**Calibration.** One correction can't fit two crystals. On a pair's calibration capture, `calibrate.py` measures
stick A from the carriers below 392.5 MHz and stick B from those above. It saves each stick's error as `ppm_a`
and `ppm_b` in whole ppm, which is what librtlsdr takes, and keeps what is left over (at most 0.5 ppm) as the
shared correction. librtlsdr's sign convention was taken from its source: the tuner's crystal is taken to run at
nominal × (1 + ppm/10⁶).

**Which captures calibration measures.** Only those made the way this computer's receiver is set up now:
- the same kind;
- for the pair, the same sticks (by serial) with the same ppm settings, tuned the same way (the
  same handover frequency; added in the second review: a carrier between two handovers was put on the
  wrong stick).

It takes the newest such group. Setup measures exactly the capture it just made. Until 24 November 2026, any
pair capture among the files made the newest pair capture win, so an older one could override the Airspy just
captured. A pair's captures on another receiver's computer only print their suggestion and write nothing.
Calibration against the 25 kHz raster only works within about ±25 ppm. Sticks with a TCXO (RTL-SDR Blog V3 and
V4: about 1 ppm) are well inside that; for cheaper sticks, measure the ppm first with `rtl_test -p`.

**Drivers.** `deploy/install-rtlsdr.sh` installs Debian's `librtlsdr0` and `rtl-sdr` (2.0.2 on Debian 13; it
knows the RTL-SDR Blog V4). Their udev rules give the sticks to the plugdev group, which the detector's service
has. It blacklists Linux's DVB-T television drivers, which would otherwise take the sticks at power-up;
librtlsdr also detaches them itself. It reads the sticks' serials from the USB bus, without opening them.

**How it is verified, without the sticks:**

| Check | Result |
|---|---|
| Every bound function against `rtl-sdr.h`: Debian 2.0.2's, identical to osmocom's own (`scripts/verify_rtlsdr.py`) | 19 of 19 agree |
| The real library, 2.0.2-2+b1, unpacked on the Pi but not installed | exports all 19; loads through the bindings; counts 0 sticks; the receiver refuses cleanly |
| A C stand-in, compiled against the real header with the Pi's gcc (`-Werror`), calling back from C (`scripts/rtlsdr_fake.c`, `rtlsdr_fake_test.py`) | 16 of 16 checks on the Pi and on Windows; below |
| `tests/test_rtlsdr.py`: settings, errors, timing on a clock of the test's own (alignment, loss, busy computer, drift, silence, overflow at once and one after the other, odd-length transfers, the join's delay), the join against an independent reference, calibration per stick and which captures it takes, the whole detector | 40 tests |
| A second engineer's review (24 November 2026), in two rounds, each finding checked by reproducing it first: the join's block edges, the overflow counted twice and then once too few, calibration's choice of captures, the order of setup, the installers' checks, the doctor, the verifiers, a test the updater's copy couldn't load | fixed, each with a test that fails without the fix: `docs/CHANGELOG.md` |
| Setup's own steps run in bash with stand-ins (`scripts/test_setup_steps.py`): one receiver opening per power-up, calibration of its own capture, the choice saved only after its driver | 11 tests; 9 fail on the branch's first version, and the other 2 are cases it already handled |
| Deliberate faults (halves swapped, no comparison with the other stick, too low a loss threshold, never realigning, an odd transfer swapping I and Q): each must make a test fail | all caught |
| `deploy/install-rtlsdr.sh` in a sandbox on the Pi (`scripts/test_install_rtlsdr.py`); its `--check` run for real, read-only | 7 of 7; ShellCheck clean |

The C stand-in's checks on the Pi:
- Every setting reaches each stick, and A is the lower serial.
- A stream ends cleanly: each stick cancelled once, its thread ended, then closed once.
- The joined output is the stitcher's to within one count. The FFT's last rounding depends on how many blocks
  a batch holds on the Pi's ARM cores, and 0.01 % of values differ by one; on x86 it is bit for bit.
- An unplugged stick ends the session as a stall.
- A 30 ms loss on one stick, with real threads and real sleeping, was found to within 3 samples and the halves
  realigned.
- The receiver path costs **38 % of one Pi 5 core**. The whole detector running on the pair, with six busy
  channels, used **95 % of one core**: the same range as with the Airspy. No throttling; 41 to 45 °C.
- The whole detector recognised bursts on channels 1, 49, 99, 100, 150 and 198 (both ends of the band, both
  sides of each DC spike, and both sides of the handover) and on no other channel.

**The updater's time.** A signed release is tested in a copy within 70 s on the car computer (`p0/update.py`).
The tests that run the whole detector on stand-in receivers, and the pair's calibration on synthetic captures,
take seconds each. With them, that copy took 138 s on the development PC, so every release of this branch would
have run out of time. They run in the full checks only (`FULL_ONLY`). The branch then costs about 1.23 times
the released code (73.6 s against 60.0 s, measured minutes apart on a PC whose speed varies by more than
that). **Before a release, measure the updater's copy on the Pi.**

## With the hardware

What only the real receivers can tell. Don't run any of these tools while the detector is running: one program
per receiver.

**SDRplay RSP1B**

1. `bash ~/Slot17/setup.sh car --receiver rsp1b`: shows SDRplay's licence and installs the API and its service.
2. `bash ~/Slot17/setup.sh doctor`: "Receiver" and "SDRplay API" ok.
3. `bash ~/Slot17/setup.sh car --calibrate --no-display --no-notify`, with the antenna connected.
4. Ten minutes parked, then read the heartbeat's `receiver` counters:
   - `gaps` should be 0, and `gap_detection` should still be `on`. If it switched itself off, `firstSampleNum`
     means something else on this receiver.
   - also read `overloads` and `samples_per_packet`, and check `dropped` after the start-up minute.
5. Gain: try `lna_state` 1 to 4 and `if_gain_reduction_db` 30 to 50 in the settings file. Compare the noise
   floor and overloads, and note what is best.
6. The DC spike: channels 99 and 100 should be no noisier than their neighbours.
7. `top`: what `sdrplay_apiService` and the detector cost.

**The RTL-SDR pair**

1. If the sticks share a serial and are not TCXO sticks, give one of them another serial first.
2. `bash ~/Slot17/setup.sh car --receiver rtlsdr2`, then `bash ~/Slot17/setup.sh doctor`: "Receiver" (both sticks) and
   "librtlsdr" ok.
3. Sample losses at the rate used, before the detector runs: `rtl_test -d 0 -s 2560000` for five minutes, then
   `-d 1`, then both at once in two terminals. Each should say nothing about lost bytes.
4. Without a TCXO: `rtl_test -d 0 -p` for a few minutes per stick, and put the figures in `ppm_a` and `ppm_b`.
5. `bash ~/Slot17/setup.sh car --calibrate --no-display --no-notify` saves `ppm_a` and `ppm_b`. **Calibrate a second
   time**: both halves should then read within about 1 ppm. That proves the sign of librtlsdr's correction on
   real sticks.
6. Ten minutes parked, then read the heartbeat's `receiver` counters:
   - `lost_samples_a` and `lost_samples_b` should be 0;
   - also read `realignments` and `alignment_ms`;
   - `saturation_a` and `saturation_b` are the share of bytes at 0 or 255 (the 8-bit converter clipping):
     lower `tuner_gain_db10` if they are not near 0;
   - `backlog_ms` should stay well under a second.
7. The edges of each stick's reach: channels 0, 99, 100 and 199 against a channel in the middle of each half,
   and whether a strong signal just outside 380 or 385 MHz shows up inside.
8. Power: two sticks draw about 0.6 A between them, besides the SSD and a 4G stick. If `setup.sh doctor`
   reports undervoltage, a powered USB hub.

## Limits

- **The quiet-input fault** ("receiver input very low") compares the input with levels measured on the Airspy
  at its gain. For the other receivers there are no such levels yet, so it reports "unknown" and never raises
  that fault. Measuring them needs the hardware and an antenna.
- **RTL pair:**
  - 8 bits per sample against the Airspy's 12.
  - Losses under 20 ms are not seen. The ones that are seen are reported up to 5 s late, and marked where they
    are found; the windows between are dated early by the whole loss.
  - One stick queueing behind the other for more than 5 s costs a realignment there and back, and the stretch
    is lost from the joined stream (reported as lost).
  - One stick's transfers queueing for over 5 s can't be told from a loss.
  - The halves may be up to 5 ms apart in time, which matters only to the advisory timing across 382.5 MHz.
  - Frequency correction is in whole ppm per stick.
- **RSP1B:**
  - Closed source, a root service, SDRplay's licence and data clause, and API 3.15 only.
  - The stream callback hands its batches on from the API's own thread, as the Airspy's does from libairspy's.
    For the detector that is a memory copy; a calibration capture writes the file there.
  - API calls that fail without ending the session are kept in the status (`api_errors`).
- The defaults for every receiver's gain are starting points, not measurements.
