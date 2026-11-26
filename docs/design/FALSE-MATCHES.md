# How Slot17 could report a false match, and what stops it (18 November 2026)

Every way the detector could report a TETRA match that is not one, or report a real one wrongly, with
the evidence we have and what in the code prevents it. "False match" here means the recogniser says
*TETRA training word* where there is none. The much larger practical problem - real TETRA from a
radio that is not a nearby emergency vehicle - is section 2; no software can remove it.

## 1. Mechanisms, evidence and safeguards

| # | How it could happen | Evidence | What prevents it | Status |
|---|---|---|---|---|
| 1 | **Noise or interference resembles the training word by chance** | 12 November: 1 false match in 760,554 control examinations (standard rule; a reversed burst at correlation 0.84, coherence 0.358). 1.2.6: 0 in 284,438 field controls. Release candidate 1.2.7: see §3 | Training word (11-15 symbols) plus modulation coherence plus symbol errors; for normal bursts 3 tail symbols at fixed positions | Measured; rate below about 1 in 300,000 per examination |
| 2 | **A strong burst recognised again on the neighbouring channel** (the differential test ignores any constant frequency shift, so a burst 25 kHz away looks like a valid word rotated by 7 kHz) | 1,409 real matches: none within 3 ms of a match on another channel; none with an offset near ±7 kHz (largest 1.38 kHz) | The RRC matched filter attenuates a neighbour; the coherent rule only searches ±1.5 kHz, so a neighbour cannot pass it at all | Not seen |
| 3 | **Mirror image** (a burst reflected around the band centre by I/Q imbalance) | 0 pairs on mirror channels in 1,409 matches | The Airspy samples one real signal and converts digitally, which has no analogue I/Q imbalance | Not seen |
| 4 | **The same burst counted twice** by overlapping windows (since 1.2.0) | 0 of 356 matches since 1.2.0 in two windows | A burst belongs to the window it starts in | Not seen |
| 5 | **Aliasing at the band edge** (new channels 0-2 and 196-199 since 1.2.6) | No match there yet | Events carry `band_edge: true` so they can be counted apart | Watch |
| 6 | **A loud alert from a borrowed strength** (if a match's own strength cannot be measured, the window's strongest candidate, which may be interference, is kept for diagnosis) | Code: `display_publisher.Channels.add` sends no strength when `strength_reliable` is false; `notify.py` will not escalate on it | Unreliable strength never reaches the phone as a number | Safe by design |
| 7 | **Mast signals leaking into the uplink band** (downlink bursts use normal training words 1-3 too) | 4 hours parked at home within reach of masts: 0 matches | Separate bands 10 MHz apart; the receiver covers 5 MHz of the uplink only | Not seen |
| 8 | **A flawed test that hides false matches** | Found 18 November: normal training sequence 2 is the conjugate of sequence 1, so "conjugated" controls for normal bursts were real bursts. They tested the tail check (which refused all), not the word | Conjugated normal bursts are now reported apart; reversed bursts and conjugated control bursts remain valid controls | Corrected |
| 9 | **An off-raster or unsynchronised transmitter** | 2 of 1,409 matches (8 November, channel 20, 7.7 and 9.5 dB) both at +1.37 kHz with clean fits (noise 1.4-1.6 Hz): real training words, but not locked to a mast within ±100 Hz like a C2000 radio in network mode | Recorded `carrier_hz` shows it | Real TETRA, origin unknown (direct mode or another TETRA user possible) |

## 2. The false positive no software can remove

A match means *a TETRA radio transmitted*. Most transmissions are registrations and status messages from
radios in buildings, parked vehicles and on people ([TETRA-SIGNALS.md](TETRA-SIGNALS.md) §3-5). Power control sets a radio's
power by its distance to *its* mast, and fading moves strength by 10-30 dB within 39 cm, so neither
strength nor any measurement in this report separates a passing vehicle from a radio in a station. The
detector and app say so; this report does not change it.

## 3. The release candidate 1.2.7 (coherent rule) against false matches

The final validation, with the finished code and the coherent rule on:

| Test | Examinations | Accepted |
|---|---|---|
| Field controls: all 142,219 snippets of three drives time-reversed, and conjugated control bursts | 284,438 | **0** |
| Conjugated normal bursts (real bursts of the other sequence: a test of the tail check) | about 142,000 | **0** |
| Generated: TETRA modulation without the word | 12,000 | 0 |
| Generated: noise only | 6,000 | 0 |
| Generated: unmodulated tones | 6,000 | 0 |
| Generated: plain QPSK, 18 ksymbol/s | 9,000 | 0 |
| Generated: 4FSK 4.8 ksymbol/s (DMR-like) | 6,000 | 0 |
| Generated: 2FSK 9.6 kbit/s | 6,000 | 0 |
| Generated: wideband noise bursts (LTE-like) | 9,000 | 0 |
| **All** | **338,438** | **0** |
| Real field matches | | 910 by the existing rules (unchanged) + **151** by the coherent rule = 1,061 |

**What "sure" means here.** No finite test proves zero. With 0 false matches in 338,438 independent
examinations, the false-match rate is below about 1 in 113,000 per examination with 95 % confidence
(the "rule of three": 3 / n). On top of that, the coherent limits sit 0.03 above the highest control
seen when they were chosen, and the third drive, kept apart while choosing, gave 0 false matches
and 36 extra real-looking matches. The 151 added matches pass three checks a chance match would fail
as often as the existing weak-rule matches do (frequency within 300 Hz of the drive's typical value
89 % against 96 %, chance 20 %; right length 95 % against 98 %; another match on the channel within
two minutes 91 % against 91 %).

**Other checks:** all 1,409 earlier saved matches replay as matches with the same training word; the
coherent rule never changes or removes a match of the standard or weak rules (a test); a candidate
check takes 2.5 ms on a PC (10.5 ms in 1.2.5), so the extra test fits in the time 1.2.6 saved.
