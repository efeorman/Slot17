# Field acceptance test for p0-2.0-rc1

*Written for rc1; the measurement design is still current for rc3. See [README.md](../../README.md).*

Use [TESTING.md](TESTING.md) for the ordered commands, expected results and
result template. This document explains the RF measurement design.

The purpose of this drive is to measure alert reliability and diagnose reception
loss. An ordinary drive can expose defects, but it cannot prove a 500 m–1 km
detection range without an independently known transmitting source and distance.

## 1. Establish a repeatable baseline

Record antenna type, **total effective tip-to-tip length**, orientation, mounting
position, coax type/length, power source, receiver gains and software source hash.
Start with the current windscreen installation and unchanged gain 12/12/12,
6 MSPS, 382.5 MHz, threshold 6 dB. These gains are a reproducible baseline, not a
verified optimum. Keep the tested source fixed for a session.

A half-wave dipole near 383 MHz is roughly 37–39 cm total effective length. The
holder/conductors contribute; do not confuse total length with each telescopic
rod. A balanced dipole does not need the car roof as its ground plane. Keep its
axis approximately vertical initially, separate the coax from USB/power wiring
where practical, and record the arrangement. Do not assume a windscreen
has a particular coating loss without measuring it.

Run preflight with the service stopped. Start one capture session as documented
in README, wait for completed-analysis readiness, and record the start time.

## 2. Collect matched observations

Use a passenger or parked operator for marks. For each sighting record the exact
time with timezone, visually identified vehicle type, direction, closest approach
or measured position if known, obstacles, and whether an alert appeared. Record
uncertainty. A sighting does not prove the radio was transmitting then.

Use the independent mark command immediately for **misses**, not only successes.
This preserves a short wideband snapshot without requiring a detector candidate.
Retrospective notes remain valuable but cannot recreate overwritten RF samples.
Also retain quiet intervals and unexpected alerts; they are needed to assess
false alarms. Do not label every TETRA match as police based only on timing.

## 3. Compare antenna arrangements before buying more hardware

With the car parked, compare windscreen versus a suitable external/roof-mounted
antenna arrangement using comparable signals and fixed receiver settings. A
continuous downlink is useful for relative receive-chain checks but is not a
substitute for short-uplink detection testing. Interleave A/B/A measurements when
possible; different drive times have different transmitter activity.

Compare wanted-channel signal-to-noise, noise floor, adjacent-channel behavior
and overload symptoms. Separate tests for antenna position, cable routing, power
source and gain so an improvement has an identifiable cause. Inserting an LNA
or maximizing gain can worsen blocking. A lack of digital clipping does not rule
out analog compression or intermodulation before the ADC.

To claim range, use a lawful, coordinated known-source receive test with known
transmission times and positions. Do not transmit on public-safety frequencies
for testing. Sweep/attenuation tests on a suitable isolated bench can separately
measure receiver/recognition sensitivity without needing police vehicle labels.

## 4. Evaluate each session

Check these separately:

| Metric | Initial acceptance target |
|---|---|
| Stream/analysis reliability | Zero dropped samples (outside the first minute's `startup_dropped`, since 1.2.5), callback errors, overflow and window exceptions during the test |
| Evidence integrity | No metadata loss; all requested files have explicit write outcomes |
| Processing time | Each analysis window (started every 0.25 s since 1.2.0 of 15 November 2026; 0.5 s from 12 November; 3 s when this was written) finishes within its own length with useful headroom |
| Confirmation coverage | Report unchecked and partial separately; inspect interference-heavy windows |
| Notifications | Measure phone arrival versus RF/host event time; verify offline expiry and recovery |
| Visual observations | Record total sightings, matched alerts, misses and uncertainty, including no-transmission possibility |
| RF specificity | Inspect training evidence for unexpected alerts; do not infer vehicle class from channel pairing |

These are acceptance targets, not promises already demonstrated in the car.
If metadata or processing fails, resolve that before attributing a miss to antenna
range. Compare software decisions on the same saved IQ when changing algorithms.

## 5. Gates before unattended production

Complete a representative multi-hour drive/thermal/storage soak; confirm clean
stop and restart behavior on this specific Airspy; measure actual phone behavior
through hotspot loss/recovery; collect enough independently reviewed positive and
negative observations to estimate precision/recall. Establish the antenna/gain
configuration that wins the controlled comparison. Only then enable boot
autostart. (Boot autostart was in fact enabled on 12 September 2026 at the operator's
request, before these gates were met; they remain open.) The board's existing firmware/USB wedge can still require physical
power removal, so unattended recovery remains a hardware acceptance item.
