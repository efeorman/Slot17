# A hardware-free Airspy drive

This smoke test runs the production `supervise.py --no-restart` and its real `watch.py` child against the native libairspy stand-in. It checks whether one known training pattern reaches the event log and whether a receiver stall ends the session after one opening.

## Run locally

From the repository root, use the detector's Python environment with NumPy and SciPy. Building the library uses the existing ziglang package. Windows process containment also uses psutil. Choose a fresh run directory with about 100 MiB free. The supplied scene contains 3.06 s of IQ; the service then waits for its ordinary 20 s stream timeout.

```sh
python -B sim/native/build.py
python -B -m unittest sim.oracle_test
python -B -m sim.run airspy-smoke --seed 17 --pace realtime --out sim/runs/drive-001
python -B -m sim.score sim/runs/drive-001
```

A scene JSON path also works. On a Pi, the operator runs this only during an idle test window and adds `--car-root /actual/path/to/installed/Slot17`. The launcher takes that installation's idle lock before rendering and holds it through shutdown. Use the real installation path even when invoking the simulator from a separate checkout.

The launcher copies production Python sources and the two shipped calibration/channel files into the run directory. It supplies a fresh home, receiver settings, runtime, capture and state paths and disables outbound notifications in both processes. It probes a private copy of the native library by requesting an injected Init failure and verifying the corresponding ledger entries before allowing Open. The producer consumes a pre-rendered non-looping IQ file. The flags for uplink, highway sensitivity, power recording, single launch and the watchdog match the unit. Linux also observes READY on a private notify socket. Windows records the supervisor's standalone readiness decision.

On a normal run the receiver stall ends watch, and the supervisor waits for it and exits. On interruption or launcher timeout, Linux sends SIGTERM to the isolated process group and allows up to 45 s before escalation. Windows uses a kill-on-close Job Object to contain both processes. A forced Windows teardown is recorded as an interrupted run.

## Signal and oracle

The supplied scene places a control uplink burst with its public training word on channel 99 at sample 5,440,000. A second burst with the same modulation and an untrained, fixed payload is on channel 100 at sample 10,880,000. Slot boundaries use exactly 85,000 samples at 6 MS/s. The renderer uses tetra_gen's shaped waveform, checks the central differential symbols against a fixed reference word and sets the frequency using the scene's explicit receiver offset. Payloads are fixed; the seed controls the background noise. Expected times and channels come from the scene before the detector starts. Each control has its own channel and window. [ETSI air interface, training sequences in clause 9.4.3.2](https://www.etsi.org/deliver/etsi_EN/300300_300399/30039202/03.04.01_60/en_30039202v030401p.pdf)

A match uses the candidate's window_start_sample plus its matched envelope onset in samples. The scorer maps that delivered-ring index into the native source timeline using first_sample on each transfer. The sample_start on saved power metadata has a different origin. The acceptance tolerance is ±12,000 samples (2 ms). The negative control must produce an energy candidate with completed confirmation and a negative result. Silence on that channel earns no credit.

## Results

| Exit | Verdict | Meaning |
|---|---|---|
| 0 | pass | Both controls, source accounting, a single receiver session, completed analysis, readiness, persisted events after recorder drain and the expected stall classification passed. |
| 1 | fail | A qualified run violated an assertion, for example an extra confirmation, wrong channel, missing match, second open or analysis failure. A witnessed second opening or an explicit unexpected supervisor diagnosis also fails with incomplete evidence. |
| 2 | inconclusive / setup error | The producer was late, critical evidence was missing, the run was interrupted, or setup could not start. A diagnostic identifies the reason. |

Every transfer is logged. The stand-in's own lateness (queued minus deadline) decides whether the run's timings count as performance figures: `timing_qualified` is true when its p99 is within a quarter of a transfer (2.731 ms at 6 MS/s) and no transfer is more than a whole transfer (10.9 ms) late. A run whose timings don't qualify keeps its functional verdict (what was recognised, the opening, the stall). A transfer more than a whole period late makes the run inconclusive, since the detector may then have seen a gap the scene doesn't have. (Until 24 November 2026 any single late transfer made the whole run inconclusive; on the Pi, isolated 3 to 4 ms wake-ups while the detector's threads were busy did that to runs that were right in every other way.) Callback wait (delivered minus queued) is reported separately. A planned stall is witnessed in the native ledger, watch's runtime_failure event and final summary, and the supervisor log. The supervisor returns 75 for multiple failure categories, so the scorer also checks watch's category and exit code. The smoke profile allows 19–30 s from the native stall to stop_rx to cover polling and worker shutdown around the 20 s timeout. Fast pace disqualifies a timing run. An independently observed second opening or unexpected supervisor diagnosis still fails.

run.json contains the input seed, IQ and library hashes, scene, labels, ledger, sampled heartbeats, events, final summary, exit observations and score. labels.json and control.json are written before launch. The native ledger, heartbeat history, console output and captures remain beside it for independent inspection. The score command recomputes the verdict from the observations in run.json.

A pass covers this Windows or Linux host, scene and software revision. Physical RF/USB behaviour, Pi CPU headroom, ignition power loss, actual systemd startup, signed installation, publisher, notifications and phone links require separate tests. The release tar leaves sim/ out (a pathspec in scripts/make_release.sh, checked by scripts/test_release_contents.py); the signed updater already restricts its inputs. The score tests are invoked explicitly outside the detector suite.

## First results on the Pi (24 November 2026)

- **The setting:** the operator's Raspberry Pi 5, idle at home, with the detector stopped for the run.
- **The stand-in:** built with the Pi's gcc 14.2 and `-Werror`. Its contract tests passed 11 of 11, and the direct
  `watch.py` tests 3 of 3.
- **The drives:** every scored drive of `airspy-smoke` was right functionally.
  - **Recognition:** channel 99 at −560 samples, and channel 100's control examined and rejected.
  - **Lifecycle:** one launch, and the stall ending the session in 20.1 s.
  - **Readiness:** `READY=1` on the private notify socket.
- **The stand-in's timing:**
  - p99 lateness between 0.36 and 3.31 ms, at most 4.80 ms. About one drive in four didn't qualify for timing.
  - Reading each transfer ahead of its deadline made no measurable difference over four drives each way.
  - The lateness is the Pi's scheduler while the detector's threads are busy. The stand-in runs at normal
    priority; raising it needs a real-time limit the operator would have to grant.
- **The callback's wait:** up to 72 ms before the detector's callback took a transfer, against libairspy's queue of
  8 transfers (87 ms).
  - In libairspy 1.0.10 `dropped_samples` comes only from that queue being full; a failed USB transfer stops
    streaming instead.
  - The car's own dropped samples on 24 November were whole transfers (7, 1, and 17 at start-up). So they look like
    the same thing: the callback falling behind, not USB.
- **The record:** kept with the project's own records (the runs of 24 November 2026).
