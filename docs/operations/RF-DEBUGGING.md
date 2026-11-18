# Signal strength, antenna checks and live debugging

Updated 9 September 2026. The operator correctly identified a visibility gap:
the detector computed input-health statistics but discarded them. Status mostly
showed process counters; the separate antenna meter could not share the receiver.
This update exposes and records reception and processing evidence during a normal
detector run, without a second SDR connection or retuning.

## Why signal strength is measurable but distance is ambiguous

The antenna delivers a voltage waveform. Its level depends on transmitter power,
both antennas, propagation, cable losses and receiver gains. One measured level
does not tell us the separate contribution of each factor.

An ideal free-space illustration at approximately 383 MHz:

| Distance | Free-space path loss | Change from 100 m |
|---|---:|---:|
| 100 m | about 64 dB | 0 dB |
| 500 m | about 78 dB | 14 dB weaker |
| 1 km | about 84 dB | 20 dB weaker |

These are calculations, not observed detection ranges. They follow the
[ITU-R P.525 free-space model](https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.525-5-202411-I%21%21PDF-E.pdf).
A 20 dB difference could represent ten times the distance under those assumptions,
or a difference in transmitter power/losses. TETRA mobile radios use adaptive power
control; their output is not one known constant. See
[ETSI's air-interface specification, clauses 10.2 and 23.4.4](https://www.etsi.org/deliver/etsi_i_ets/300300_300399/30039202/01_60/ets_30039202e01p.pdf).

The drive has no independently matched transmitter positions or power settings.
RSSI-to-distance calibration could support a statistical estimate in a controlled
test, with uncertainty measured on separate test runs. It would not automatically
identify which received radio belongs to a police vehicle. A simple weak/strong
bar describes reception; interpreting it as far/near can be wrong.

## What the receiver can and cannot tell us

| Measurement | Available now | Interpretation |
|---|---|---|
| Candidate peak above its channel's quiet-time floor | Yes, existing `peak_snr_db` | Relative burst contrast; includes noise and interference effects |
| Digital I/Q RMS and component headroom | Yes, live and logged | Receiver output level, not calibrated antenna-port power |
| Per-channel floor, peak and sampled activity | Yes, every completed window | Find changing or concentrated RF background and compare fixed setups |
| Antenna physically connected | Not certified | Low input can prompt inspection; strong interference can exist with a bad antenna |
| Antenna efficiency, impedance or VSWR | Not measured by this setup | Requires reference measurements and/or appropriate RF test equipment |
| Absolute received dBm | Not calibrated | Requires a known input and receiver/gain/frequency calibration |
| Police/ambulance identity or distance | Not measured | Requires independent evidence, not just energy or a public training word |

The [Airspy Mini specifications](https://airspy.com/airspy-mini/) describe a 12-bit
ADC with DSP. Our stream is processed int16 I/Q, not the raw ADC stream; the
[official API distinguishes those formats](https://raw.githubusercontent.com/airspy/airspyone_host/master/libairspy/src/airspy.h).
The previous code incorrectly called 32767 output counts an ADC rail and compared
complex magnitude to a single-component limit. It now checks I and Q separately,
including the valid negative endpoint -32768 and invalid/non-finite values.
**No digital near-rail warning does not prove the analog receiver is linear.**

For cable/match testing, a calibrated VNA can measure reflection/VSWR and suitable
transmission loss measurements. This is separate from the receiver and does not
by itself certify installed radiation efficiency. See
[Keysight's measurement overview](https://www.keysight.com/nl/en/products/vector-network-analyzers.html).

## Read the live view

Start the detector only as directed by [TESTING.md](TESTING.md). In another
SSH terminal on the Pi, during that same run:

```bash
cd /home/pi/Slot17
python -B p0/status.py --watch --interval 3
```

Ctrl-C closes this viewer; it does not stop the detector. It never opens the SDR.
To retain repeated snapshots in an existing test-evidence directory:

```bash
python -B p0/status.py --watch --interval 3 --json | tee "$TEST_RUN/live-status.jsonl"
```

Keep the output in a folder for this test. Normal polling reads the
small heartbeat and OS status, not all growing event logs. `--history` explicitly
requests a one-shot historical count and cannot be combined with `--watch`.
Old/stopped sessions have missing or historical RF data; restarting the viewer
cannot create new antenna measurements. The heartbeat and RF/host observations
have separately displayed ages. RF older than 10 seconds and host data older than
45 seconds are labelled stale. Current firmware flags and flags since boot are
separate; a historical undervoltage flag is not evidence of a current fault.

The display shows gains (indices, not dB), threshold, input levels, channel floor
changes, recent match/SNR, per-window confirmation costs and incomplete coverage,
processing time/backlog, dropped samples, recorder queues/errors, notification
counters/errors, Pi temperature/load/process RAM and power flags.
Firmware flag definitions follow the
[Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled).
The 0–70 C temperature advisory is a conservative project setting, not a claim
about the Pi's rated operating range.

## Interpret the numbers without drawing the wrong conclusion

- `rms_dbfs`: complex digital RMS relative to 32768 output counts. A full-scale
  complex tone has 0 dBFS by this convention. It covers the sampled wideband input
  and can be dominated by interference. It is not a particular radio's dBm.
- `headroom_db` and `fraction_near_rail`: use the largest I or Q component.
  Measurements sample every 32nd IQ sample; brief peaks can be missed.
- Channel `floor_db`/`peak_db`: dB relative to one **channeliser power unit**,
  not dBm or dBFS. Compare only with the same processing, rate, gain and tuning.
- Floor is the 25th temporal percentile of at most 512 evenly spaced frames per
  channel. Persistent RF contributes to it; activity percentages can miss short
  bursts. Actual candidate selection still uses the original full-resolution data.
- Floor change uses the **first observed window**, not a known-good antenna. Its
  reference resets when rate, tuning, gains, calibration or channel grid changes.
  Moving to a new environment can change the floor without a hardware fault.
- `uneven_channel_floor` flags channels more than 6 dB above the band median;
  legitimate continuous transmitters can also cause this. No new blacklist results.
- `confirmation_incomplete` exposes partial/unchecked candidates. A recogniser
  cannot reject RF it did not inspect. `processing_budget_tight` starts at 80% of
  the window duration before telemetry; inspect total processing/backlog too.
- `notification_state` on a candidate explains queued, same-channel cooldown,
  not queued, disabled/unconfigured, producer error, or not confirmed. Queued
  does not mean the phone displayed it; API success does not measure phone delay.

## Practical antenna/debugging comparison

1. Park in a fixed location. Record each arm's length, dipole orientation, cable
   type/length, connectors, mount position, engine/charger state and receiver gains.
   Do not assume a windscreen's coating or cable loss without measurement.
2. Keep receiver settings fixed. Record at least 30–60 seconds per arrangement,
   including exact times. Compare A/B/A so a changing RF environment is less easily
   mistaken for improvement. Physical changes are performed by the operator.
3. Compare both a repeatable wanted signal and its local background. More digital
   power alone can mean more interference. Noise alone cannot rank antenna quality:
   disconnecting an antenna can reduce wanted signals and noise together.
4. During ordinary uplink detection, the new live view runs from the same receiver
   stream. If there is no repeatable wanted source, mark antenna performance
   inconclusive. Do not rank placements using unrelated passing vehicles.
5. A separate planned downlink reference test can use `antenna.py` while the
   detector is stopped. It is a nearby-frequency comparison, not proof of identical
   performance across both bands or every arrival direction. Do not run
   `antenna.py`, live `diagnostics.py`, capture tools or hardware-info/open commands
   against the already-running detector. Avoid repeated stop/start on this board.
6. For the range goal, arrange a controlled known-source receive test with fixed
   power and independent distance labels, then test on different data. Meanwhile
   record sightings and alerts separately; observations made in darkness are not
   reliable negative labels. A passenger monitors the screen; a solo driver reviews
   it after parking.

## What changed and how it was checked

*This section records the change of 9 September 2026 and its figures are from that day. Checked against the code on 13 November 2026: the per-channel cooldown is 60 s (not 180), the offline suite has 131 tests (not 36), analysis windows are 0.5 s (not 3 s, so the hourly telemetry volume differs), and boot autostart has been enabled since 12 September.*

`rfhealth.py` builds telemetry after confirmation using the existing power matrix;
it neither performs another FFT nor changes RF thresholds or recognition. Full
channel summaries are recorded as `event_type: rf_health` in `events.jsonl`;
compact readings enter the heartbeat and final summary. Host probes run on a
separate thread roughly every 15 seconds and are recorded as `host_health` events.
No automatic retuning/gain changes or extra phone messages were added. The
180-second per-channel phone cooldown is preserved.

Telemetry failure is counted/logged without discarding completed RF decisions.
Heartbeat-write failures are counted instead of silently ignored. Runtime failures
include stage/reason events. Session and final summary JSON are flushed, fsynced
and atomically replaced (directory fsync on POSIX), addressing the empty-summary
failure mode without pretending this establishes the previous shutdown cause.
Abrupt loss before a first complete write can still leave no summary.

The offline suite has **36 tests**, including seven new tests for diagnostic
validity, reference reset, non-mutating measurements, stale readouts, telemetry
failure isolation, summary-write failure, heartbeat-write failures and firmware
flags. Existing short-burst recognition, 21 saved compact fixtures, recording,
notification and runtime tests remain part of it.

On the idle Pi, saved drive windows 5, 252 and 325 were each measured 20 times.
Channel telemetry plus JSON serialization took approximately **5.3–5.6 ms median**
and **5.9 ms maximum** (rounded upward); sampled input health took **13.9–14.2 ms
median**. These exclude channelisation/confirmation, host probes and disk costs
and are not live USB/load acceptance. RF telemetry was about **37–38 kB/window**,
roughly **43 MiB/hour** at three-second windows, before other event records.
The next live test must check total processing time, queue growth and sample loss.

Current source hash:
`b0c7899580fea11200ff80beb5dbeee58bf95da89c216043b56d8b0a4f54d49d`.
Evidence, exact benchmark code, sample output, tests, deployment and rollback
installation; this work does not certify field range, RF accuracy or antenna gain.
