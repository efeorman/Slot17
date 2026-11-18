# Range and sensitivity — corrected working assessment

Updated 8 September 2026 for `p0-2.0-rc1`. The owner's observation of late alerts
around 100 m is a real operational concern. The available logs do not establish
a calibrated maximum range, and software simulations cannot prove 500 m–1 km
detection. An earlier review contained the original
evidence and source-by-source fact checks. The earlier version of this document
is preserved in baseline Git commit `a45811b` and deployment backups.

## Software limitations repaired

The old recogniser failed isolated control bursts even at substantial SNR:
its active-sample threshold discarded short useful bursts, a symbol-rate statistic
needed more samples than a CUB contains, and a timing rule excluded lone CUBs.
The replacement checks public CUB/NUB training patterns after matched filtering
and differential demodulation. A single short burst can now produce an RF alert.
Synthetic isolated CUB/NUB tests and compact field replay validate this change,
not any particular physical distance. Training thresholds remain empirical and
need independent field precision/recall assessment.

Per-hit long wideband confirmation and blocking disk writes are replaced with
short regions and an asynchronous recorder. A round-robin scheduler gives channels
an initial region check before additional regions. Coincident activity is advisory,
since legitimate TETRA channels can be synchronised. Calibration is applied to
channel-power integration as well as raw-IQ tuning. Strong/moderate/weak and
rising/falling describe received signal, without pretending to infer metres or motion.

## Corrected RF calculations

At 383 MHz and 25 kHz bandwidth, thermal noise at approximately room temperature
is about -130 dBm. An assumed **whole-system** noise figure of 8 dB makes the model
floor about -122 dBm; this is not a calibrated conversion from our logged units.
The Airspy manufacturer's own noise-figure specification is a different quantity
from unmeasured cable/installation/interference losses in this car.

The implemented piecewise two-ray scenario must cross from free-space to its
asymptotic law at `4*pi*h_tx*h_rx/lambda`, about 36.1 m for 1.5 m heights at
383 MHz. The prior code joined at `4*h_tx*h_rx/lambda` and added about 9.94 dB
of unintended far-distance loss. Correcting that gives internally consistent
arithmetic; it does not establish that two-ray propagation describes this route.

The research model uses transmitter scenarios of 15, 34.8 and 40 dBm and assumed
antenna/loss terms. They are not confidence bounds. TETRA power control, portable
versus vehicle antennas, clutter, screening, polarization and analog blocking
remain unknown. See the [ETSI physical-layer specification](https://www.etsi.org/deliver/etsi_en/300300_300399/30039202/03.02.01_60/en_30039202v030201p.pdf).

Using the audit's separate log-distance example (3 W, +2 dBi transmit gain,
-10 dB receive loss, NF 8 dB, exponent 3.5 referenced to free-space at 1 m), the
modelled SNR is about 54.7 dB at 100 m, 30.2 dB at 500 m and 19.7 dB at 1 km.
The previous 85/60/50 dB table was incorrect. These remain conditional model
outputs, not observed receiver sensitivity or guaranteed margin.

## What to test next in this car

- Establish the dipole's total effective length and vertical orientation; compare
  the windscreen arrangement to an appropriate external antenna under controlled
  conditions. A balanced dipole does not need a vehicle ground plane.
- Identify coax type, length and connector losses. Representative RG-174 loss
  around this frequency is roughly 0.6 dB/m, so several metres matter, but cannot
  alone establish the whole range deficit.
- Measure the wanted signal and interference with different cable routing, power
  sources and gains. No digital clipping does not exclude earlier analog overload.
- Keep the present 193-channel usable mask until a passband measurement justifies
  widening it. At 382.5 MHz / 6 MSPS the nominal edge channels outside that mask
  remain uncovered; extra nominal bandwidth is not automatically usable bandwidth.
- Capture missed sightings independently with `mark.py`. No candidate can mean
  silence, screening, coverage loss, RF impairment or software failure; the data
  must distinguish them before spending on a new receiver, LNA or SBC.

Use [FIELD-TEST.md](../operations/FIELD-TEST.md) for the next session. The production acceptance
target remains reliable alerts with measured coverage and latency; no numerical
range target is claimed achieved by this software release.
