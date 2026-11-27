# What a TETRA signal is, and what it can and cannot tell us

*Update, 3 November 2026: the -2.1 kHz residual below led to a corrected calibration of about
+4.09 ppm.*

*Read with care (26 November 2026): this note is from September 2026. Some early inferences in it were withdrawn
later, in §11 and after: that a repeated or rising signal points to a vehicle approaching or to a crew, and that all
detections are random access bursts (the detector also recognises normal bursts, which give the call tone). Successive
bursts can come from different radios. None of the timing or strength patterns here established motion, direction, a
crew or a particular sender.*

Research note, 13 September 2026. **Amended the same day** after an independent review; §11 lists what changed.
Written before any code change, to answer one
operational question: **why does the detector report TETRA when no police, ambulance,
handhaving or KMar vehicle is anywhere in sight, and what would actually make it more
accurate?**

Primary source: ETSI EN 300 392-2 (TETRA Voice+Data air interface), read in
**V2.1.1 (2000-12)** as requested and cross-checked against **V3.2.1 (2007-09)**.
Clause numbers below are from V2.1.1 unless stated. Everything attributed to our own
equipment is measured from the 9 and 12 September recordings, not assumed.

---

## 1. Summary for the impatient

1. **No evidence of a recogniser fault.** All 90 confirmed matches replay as control
   uplink bursts carrying the ETSI training word with zero symbol errors, and 0 of 376
   controls were ever accepted. That is strong agreement with the recogniser, not
   independent proof that every waveform was genuine TETRA. The problem is that "a TETRA
   radio transmitted" and "an emergency vehicle is near me" are very different claims.
2. **There are roughly 85 000 to 90 000 C2000 radios in the Netherlands and about 620 masts**
   (620: a suppliers' note to parliament, March 2025; "almost 90 000" radios and vehicle sets
   to reprogram: an older parliamentary paper on the renewal; 85 000 and 615 were this note's
   September figures). An average cell covers ~67 km² (≈4.6 km equivalent radius) and
   therefore holds on the order of **140 radios** as a crude national average - not a measured count within our
   receiving range. Their random access converges on the cell's **main control carrier**
   uplink, the channel we watch, though a cell may run several carriers and a physical
   channel is carrier *plus* slot.
3. **Received strength is not distance, and for our burst type it is close to
   uninformative about distance.** A radio sets its power from its distance to *the
   mast*, not to us, over a 30 dB range (clause 23.4.4.2). 30 dB is a factor of 32 in
   distance. A handheld 200 m away under a mast can reach us weaker than a van 3 km away
   at the cell edge.
4. **Most of what we hear is a radio saying "I am here" to the network**, not a vehicle
   driving towards us. It happens when a radio is switched on, changes cell, sends a
   status or sets up a call - including radios in buildings, stations, parked vehicles
   and on foot.
5. **A better antenna increases range, so it will produce *more* detections of radios
   you cannot see.** Ferrites may reduce our own interference (unproven for this
   installation). Either way, sensitivity cannot buy selectivity; that has to come from
   alert policy, and no policy here is yet validated against labelled ground truth.
6. I tested the one idea that could have separated a moving vehicle from a static
   radio - Doppler - and **it is not shown with this receiver**: 100 km/h gives at most
   35 Hz, while our receiver's own frequency drifts by 37 to 124 Hz over a drive and we
   cannot attribute successive bursts to one radio. The measurement itself is no longer
   the limit (0.3 to 2 Hz since 1.2.5). Details and scope in §7.

---

## 2. The physical layer: what we actually detect

### 2.1 Structure

| Quantity | Value | Source |
|---|---|---|
| Carrier spacing | 25 kHz | EN 300 392-2 clause 6 |
| Modulation | π/4-DQPSK, 18 000 symbols/s | clause 5.2 |
| Slot | 255 symbols = **14.167 ms** | clause 9.3 |
| Frame | 4 slots = **56.67 ms** (17.65 frames/s) | clause 9.3 |
| Multiframe | 18 frames = **1.02 s** | clause 9.3 |
| Hyperframe | 60 multiframes = **61.2 s** | clause 9.3 |
| Duplex spacing (NL) | 10 MHz; uplink 380-385, downlink 390-395 MHz | CEPT/C2000 plan |

The uplink (what we listen to, 380-385 MHz) is **not continuous**: a radio transmits
only in its allocated slot, only when it has something to send. The downlink
(390-395 MHz) *is* continuous from the mast - which is why a mast is easy to find and
tells you nothing about vehicles.

### 2.2 Burst types, and why ours is always the same one

| Burst | Length | Contains | Meaning |
|---|---|---|---|
| **Control uplink burst (CUB)** | 103 symbols ≈ **5.7 ms** | 30-bit extended training sequence | random access: registration, call request, short data |
| Normal uplink burst (NUB) | 231 symbols ≈ 12.8 ms | 22-bit normal training sequence 1 or 2 | traffic or signalling in an assigned slot |

**Until November all confirmed matches were CUB** (90 of 90 in September), and that fact
drove most of §5: we were detecting *access attempts*, not conversations. Since the NUB
recogniser and the weak rules (12 November) NUBs are recognised too: of 1,409 matches from
8 to 17 November, 1,128 were CUB, 263 NUB with training sequence 1 and 18 with sequence 2.
Most matches are still access attempts; the NUBs come in runs, as a radio in a call or
assigned slot does.

Measured on those 1,409 bursts (17 November 2026), against the standard's Table 7: CUBs
103.9 symbols above half power (standard: 103 useful symbols), NUBs 231.1 (231); ramps of
about 4.5 symbols (allowed: 16 up, 15 down); 99 % bandwidth 24.3 kHz (18 ksymbol/s × 1.35).
A fifth burst type exists that we never look for: the *linearisation uplink burst* (119
symbols of ramp and amplifier tuning, no useful payload).

### 2.3 Confirmed against our own recordings

From the 12 September drive, 72 measured gaps between burst starts inside one 3-second
analysis window:

- **61 % sit exactly on the frame grid** (n × 56.67 ms, ±12 %; n=1 seventeen times,
  n=12 eleven times), two on the 1.02 s multiframe grid.
- Burst lengths 5.5-6.3 ms, i.e. the 103-symbol CUB, never the 14.17 ms traffic slot.

So the TDMA structure in the standard is directly visible in our data. What is *not*
visible is any 4-second heartbeat (§6).

---

## 3. When does a radio transmit on the uplink?

From clauses 16 (mobility management) and 23 (MAC):

| Trigger | Typical cause in the street |
|---|---|
| **ITSI attach / registration** | radio switched on, shift change, battery swap |
| **Cell reselection + location update** | the radio moved into another cell - *any* vehicle or person on the move |
| **Periodic registration** | network-timed; clause 16.4.2 ties it to location-area timers and BS state changes, **not** to a fixed short interval |
| **Random access for call setup** | someone presses PTT, control room calls a unit |
| **Short data (SDS) / status** | status buttons, automatic vehicle location, telemetry |
| **Energy economy wake-ups** | clause 16.7: a radio in a power-saving mode still registers its state |

Note what is missing from that list: "a vehicle is driving past you". The uplink tells
you a radio exists and is registered with a mast whose cell you happen to share.

**Direct Mode (DMO)** - radios talking to each other with no mast, used for exactly the
roadside-check scenario people want detected - is a different standard (EN 300 396) with
its own synchronisation, burst layout and tail positions, usually in a separate sub-band.
We have no DMO recogniser and should not expect to detect it; but because DMO shares the
normal training words, accidental acceptance is not mathematically excluded either.

---

## 4. Power control: the single most important fact

Clause 23.4.4.2, MS open loop power control:

```
PMS = MIN( MS_TXPWR_MAX_CELL , ACCESS_PARAMETER − RSSI )
```

The radio's transmit power is set by **how strongly it hears its own mast**. Power
classes run 1 W to 30 W nominal (clause 6.4, table 3) and the control range goes down to
15 dBm (32 mW), i.e. **30 dB of span** - a factor of 32 in distance for the same received
level. Consequences for us:

- A radio close to its mast turns *down*. A radio at the cell edge turns *up*. Neither
  has anything to do with its distance to our car.
- A 1 W handheld and a 10 W vehicle set differ by 10 dB before any of this.
- **Except for random access.** The same clause says power control is *optional* for
  random access bursts, and that the radio *may raise* power to
  `MS_TXPWR_MAX_CELL` "in order to increase the probability of the random access
  transmission reaching the BS". Our detections are **all** random access bursts - though the training word decodes no
   transaction, and a *permitted* power increase is not proof that the radios we heard
   used it.

So the bursts we detect are the ones *most likely* to be sent at the cell's maximum
allowed power regardless of who or where the radio is. That is the cleanest available
explanation for "strong signal, nothing in sight" - a mechanism the standard permits,
not something we have observed a radio doing.

A possible silver lining, **unproven**: *if* CUBs are mostly sent at a per-cell maximum,
then differences in received strength between CUBs on one channel over a short time would
be dominated by path loss. We have not established that, so §8 treats it as a hypothesis
to test rather than a basis for any proximity claim.

---

## 5. Why we hear TETRA with no emergency vehicle in sight

Ranked by how much each contributes, with numbers where we have them.

1. **Cell geometry.** ~615 masts over 41 543 km² → average cell ≈ 68 km², equivalent
   radius ≈ 4.6 km (published TETRA figures: 4-5 km urban, 10-25 km rural). Every radio
   in that whole area shares the uplink channel we watch.
2. **Population of radios.** ~85 000 C2000 radios → ~2 per km² nationally, ~140 per
   average cell, several times that in a city. Police is only part of it: fire,
   ambulance, KMar, Defence, DJI, Rijkswaterstaat, ProRail, customs, KNRM and regional
   safety services all use C2000.
3. **Power control (§4)** removes most of the link between strength and distance.
4. **Radios that are not in vehicles at all**: handhelds on belts, in stations, in
   hospitals, at fire posts, in parked vehicles, in offices. A registration burst from a
   radio sitting in a building three streets away looks exactly like one from a car.
5. **Height and propagation.** 380 MHz over a clear path reaches kilometres from a few
   watts. Our windscreen antenna sees a large area, not the road ahead.
6. **Our own noise floor.** Car electronics raised the floor across dozens of channels
   on 9 September (channels 3-39 sat 23-32 dB above the floor). This does not create
   false TETRA matches - the recogniser rejects it - but it consumes the confirmation
   budget, which is why ~19 % of candidates went unchecked or partly checked.

**What this is not:** it is not a detector bug, not a wrong training word, and not
(on the evidence) a false-positive problem. 90/90 confirmed matches replay as genuine
CUBs, and 0 of 376 control snippets were ever accepted.

---

## 6. The "every 4 seconds" claim

Widely repeated, and **not supported by the standard**.

- What is true: in a call, a radio transmits one 14.17 ms burst per 56.67 ms frame -
  17.65 bursts/s. Our data confirms the frame grid (§2.3).
- What is not: EN 300 392-2 has no 4-second uplink keep-alive. Registration is
  event-driven, plus *periodic* registration whose interval the network sets through
  location-area timers (clause 16.4.2) - minutes, not seconds. one maker's own FAQ
  concedes that "emergency services do not always transmit (periodical) signals".
- What we measure: the gap between confirmed matches on one channel ran **median 21 s,
  minimum 0.5 s, maximum 4.5 minutes**. No metronome. (Our confirmation coverage is
  partial, so the true spacing is somewhat shorter than that.)
- The "×/min" on the phone page is our own 60-second counting window, not a property of
  the radio.

---

## 7. Tested and rejected: Doppler as a movement test

The idea: a vehicle closing at speed shifts the carrier; a radio in a building does not.
If we could measure that, we could alert only on *moving* sources.

What I did: re-derived each burst's carrier offset from the saved snippets of the
12 September drive, using the fourth-power estimator (`mean(d⁴)`, whose magnitude the
detector already computes as "coherence" and whose **angle it throws away**),
disambiguated with the training-sequence estimate. One real trap: for π/4-DQPSK every
transition is an odd multiple of π/4, so `d⁴` carries a constant π - forget it and every
estimate lands exactly half an ambiguity step (2 250 Hz) off.

Results over 36 bursts:

| Measurement | Value |
|---|---|
| Median residual offset | **−2 197 Hz** (9 September: −2 124 Hz - the same receiver calibration error) |
| Fine vs coarse estimator agreement | mean +5 Hz, sd 54 Hz |
| Spread across strong bursts (≥20 dB) | **39 Hz** |
| Doppler at 380 MHz | **1.27 Hz per m/s** → 18 Hz at 50 km/h, 35 Hz at 100 km/h, 70 Hz for a 200 km/h head-on closing |
| **Radio's permitted frequency error** | **±100 Hz** (clause 7.6a - which explicitly names Doppler as one contributor) |

**Verdict: not viable.** The transmitter's own legal tolerance is three times the
Doppler shift we would be looking for, and our per-burst precision (~±40 Hz) is itself
comparable to the effect. Worse, successive bursts on one channel need not come from the
same radio, so we cannot even track one emitter reliably. Recording this so nobody
spends a week building it.

*Useful by-product:* the same measurement is a free, precise calibration monitor. Our
receiver is mistuned by about −2.2 kHz, consistent across two sessions four days apart.

### 7.1 Revisited on 17 November 2026: the estimator was the limit, the receiver is now

Prompted by a technique that places a video by the Doppler curves of aircraft heard in it
(matched against public flight tracks), the question was asked again: could the *shape* of a
frequency curve over time, in which a radio's constant offset cancels, say where a transmitter
is or whether it moves?

| Measurement (1,409 matched bursts, 8 to 17 November) | Value |
|---|---|
| Training-word estimate (`residual_offset_hz`), neighbouring bursts on one channel | 60 to 92 Hz spread |
| Whole-burst estimate (`carrier_hz`, 1.2.5), same pairs | 21 to 24 Hz; inside one burst 1 to 2 Hz from 15 dB |
| Generated bursts, known offset, 15 dB and up | error 0.3 Hz (NUB), 1.2 to 1.7 Hz (CUB); training word 16 to 49 Hz |
| One call on one channel, 37 bursts in 9 s | old −158 to +23 Hz; whole burst mostly −76 to −93 Hz |
| Inside an exchange vs between exchanges, one drive | 6 Hz vs 17 Hz |
| **Our receiver's drift during a drive** (all channels together) | **37 to 124 Hz**, smooth, e.g. −83 to −17 Hz in 24 minutes |
| Fixed-transmitter fit to the car's own motion, 13 passes | beat shuffled frequencies 6 times, but beat a reversed or shifted route and a straight drift once; after removing the drift seen on other channels, 0 of 9 |

**Verdict now:** the per-burst precision is fine (§7's ±40 Hz came from using the training word
only). What blocks Doppler is the receiver's clock, which wanders more than the effect, plus
several radios sharing a carrier and few bursts per pass. A test would need a receiver locked to
a stable reference (GPS-disciplined), a position every second, and passes with many bursts. The
records: `audit/doppler-check-20261117/` in the private archive and the Engineering Log.

---

## 8. Candidate improvements

**These are alert-rate policies and hypotheses, not demonstrated proximity classifiers.**
A distant radio can repeat, a nearby one can transmit once, and several radios share a
carrier - so an apparent trend need not be one transmitter approaching. Each needs
independently labelled evidence before becoming a hard gate. None require decoding.

### 8.1 Software (where the real gain is)

| # | Change | Why it helps | Evidence |
|---|---|---|---|
| 1 | **Require persistence**: alert only after N matches on one channel inside T seconds (e.g. 2 in 30 s) | A radio that is near you and active repeats; a one-off registration from 3 km away does not | Nearby activity produced 8 matches in 6 min (ch 171) and 7 in 5 min (ch 170); isolated single matches were common elsewhere |
| 2 | **Alert on rising strength, not on absolute level** | Within one channel over a short window, CUB strength is dominated by path loss (§4), so a rise is the closest thing to "approaching" we can honestly measure | 36 matched strengths spanned 10.8-48.8 dB, median 20.5 |
| 3 | **Tune the red threshold from matched strength** | Sets how many alerts you get: 22 dB → 15 of 36; 26 dB → 9; 30 dB → ~5 | measured, 12 September |
| 4 | **Coincidence across channels** | Two radios moving together (crew of two) show up as near-simultaneous matches on different channels | ch 168/176/170 within 6 s on 12 September |
| 5 | **Cell context**: remember which channels are normal for home/commute | A channel you always see is your local cell, not news; a new channel means you moved | `downlink_channels.json` already holds the home cell list |
| 6 | ~~Suppress familiar channels~~ **withdrawn** | The home cell's carrier is exactly the one a passing ambulance would use; carrier-wide suppression hides what you want | reasoning, not measurement |

### 8.2 Hardware, and an important warning

| Item | What it actually fixes | Effect on this problem |
|---|---|---|
| **Ferrite clamp chokes** (the "noise caps") on coax, USB and power leads | Common-mode noise from the Pi and the car conducted onto the cable screen; this is what raised our floor across channels 3-39 | **Helps**: fewer junk candidates → more confirmation budget for real bursts → less of the 19 % unchecked. Does **not** reduce distant detections |
| **Band-pass filter for 380-395 MHz** at the receiver input | Strong out-of-band transmitters compressing the front end and creating spurs | Helps stability and reduces phantom candidates |
| **Better / external antenna** | Windscreen and cabin losses | **Increases range → more detections of radios you cannot see.** Makes the symptom worse unless §8.1 rules are added first |
| **Directional (forward-facing) antenna** | One maker's own admission is that its detector "looks around rather than ahead" | The only hardware route to real selectivity, but awkward to mount and it would miss vehicles coming from behind |

So: fit the ferrites and the filter for signal quality, but do not expect them to remove
"TETRA with nothing in sight". That is a software-rules problem.

---

## 9. Honest limits: what this project can never tell you

- **Who.** No identity is decoded, so police, fire, ambulance, KMar, DJI, Rijkswaterstaat
  and a hospital radio are indistinguishable. C2000 traffic is encrypted anyway, and
  decoding it is both illegal and outside this project's design.
- **Where.** No distance, no direction. One receiver, one antenna, no timing reference.
- **Whether it is a vehicle.** A radio is a radio. Bars are not vehicles - one maker says
  the same in its manual. The one exception is an aircraft in the air: since 1.8.0 a match on
  the air-ground-air channels is marked as one (§12).
- **Direct mode.** Radios talking directly to each other at a traffic check are invisible
  to us, which is precisely the scenario people most want covered.
- **Silence.** A vehicle whose radio is idle and not registering transmits nothing. No
  detector of this class can see it.

---

## 10. Next experiments worth running

1. **Log strength trends per channel across a whole drive** (no alerting change) and see
   whether passing vehicles produce a recognisable rise-and-fall against static sources.
   This is the empirical basis for rule 8.1.2.
2. **Record one drive past a known stationary post** (a fire station, a police bureau)
   and compare its signature with a vehicle that visibly overtakes you.
3. **Measure the floor before and after the ferrites**, with the same route, to quantify
   what they buy in candidate counts and confirmation coverage.
4. **Correction:** `residual_offset_hz` is **already computed and logged** (`p0/phy.py`
   line 132; last night's events carry −2063.8, −2088.5, −2102.5 Hz). My fourth-power
   estimator is a different, finer diagnostic. Neither is an independent frequency
   reference: recalibration needs a known source.

## Sources

- [ETSI EN 300 392-2 V2.1.1 (2000-12)](https://www.etsi.org/deliver/etsi_en/300300_300399/30039202/02.01.01_60/en_30039202v020101p.pdf) - clauses 5, 6.4, 7.6, 9.3, 16.4, 16.7, 23.4.4, 23.5
- [ETSI EN 300 392-2 V3.2.1 (2007-09)](https://www.etsi.org/deliver/etsi_en/300300_300399/30039202/03.02.01_60/en_30039202v030201p.pdf) - tables 9.2/9.4, equations 9.11/9.12 (burst layout and training words)
- [ETSI EN 300 396-2 (TETRA Direct Mode: radios talking to each other without a mast)](https://www.etsi.org/deliver/etsi_en/300300_300399/30039602/01.04.00_40/en_30039602v010400o.pdf)
- [C2000 - Nederlands Instituut Publieke Veiligheid](https://nipv.nl/onderzoek/c2000/) and [C2000 (Wikipedia NL)](https://nl.wikipedia.org/wiki/C2000) - users and organisations
- [Techniek en toekomstbestendigheid C2000, Tweede Kamer 2025](https://www.tweedekamer.nl/downloads/document?id=2025D12295) and [c2000masten.nl](https://c2000masten.nl/) - number of masts
- Our own recordings from September 2026.

*Checked 13 November 2026:* c2000masten.nl lists 609 masts and Dutch Wikipedia about 600, so the 615 used above holds. The 85 000 radios figure could not be confirmed again from these sources that day; the last official count found was about 65 000 programmed radios in 2006. The estimate of about 140 radios per average cell scales with it (about 107 at 65 000).

---

## 12. Aircraft channels (27 November 2026)

**The rule.** ECC/DEC/(06)05 (CEPT, 2006) designates 384.800–385.000 / 394.800–395.000 MHz as
the core bands for air-ground-air (AGA) use by emergency services, with 384.750–384.800 /
394.750–394.800 MHz as extension bands: channels 192–199 and 190–191 on our grid. ETSI
TR 102 300-6 (the TETRA AGA designers' guide) has an aircraft's radio hand over to an air cell
before 500 ft (150 m), at no more than 30 dBm (1 W), and back to a ground cell well below
500 ft. Germany's BDBOS says the same of its Luftfahrzeugfunkzellen.

**Our data.** All 133 sessions on the car computer (6 September to 27 November): 13,171
matches, 3,113 (24 %) on channels 190–199; channel 190 is the busiest channel of all.
Air-channel episodes stayed audible over up to 26 km of driving, ground channels never past
10 km, and their strength varied less. Against the ADS-B Exchange tracks of 18 Dutch police
and trauma helicopters, minute by minute along the drives:

| | minutes | a tracked helicopter flying within 20 km | within 30 km |
|---|---|---|---|
| With an air-channel match | 189 | 50 % | 74 % |
| Without one | 774 | 4 % | 12 % |

**What follows (1.8.0).** A match on channels 190–199 carries `air_channel: true` in the
detector's events, and every display marks it with a helicopter and the word Aircraft. It
changes nothing in detection: the label is a lookup on a match already recognised.

**Limits.** A helicopter below about 150 m uses the ground channels and is not told apart. A
quarter of the air-channel minutes had no tracked Dutch helicopter within 30 km (German or
Belgian aircraft, fixed-wing aircraft, flights without a public track). The channel says
"aircraft", not police or ambulance. Records: `audit/air-channels-20261127/` in the private
archive.

## 11. Amendments after independent review (13 September 2026)

An independent review was right on several points. They are corrected above rather than
argued with.

| Claim here originally | Correction |
|---|---|
| "Log `residual_offset_hz` - already computed and discarded" | **Wrong.** `p0/phy.py:132` already returns it and the field is in last night's events. Verified. |
| "Every radio in a cell uses the same uplink channel" | A cell can run several carriers, and a channel is carrier *plus* slot. Random access converges on the main control carrier; the density argument survives, the wording did not. |
| CUBs are "mostly registration", sent at "fixed maximum power" | The training word decodes no transaction, and permitted power is not observed power. Both are now hypotheses. |
| Persistence / rising strength "improve accuracy" | They are alert-rate policies, not proximity classifiers. |
| Coincident channels = a crew; a new channel = you moved | No identity or location data exists in the recordings. |
| Suppress familiar channels | Withdrawn: that carrier is the one a passing ambulance also uses. |
| Car noise "caused" the broadband activity; ferrites "will fix" it | Source and coupling path were never isolated; a ferrite may help a common-mode path, and preselection needs insertion-loss and blocking measurements first. |
| Doppler is impossible | Scoped: not shown *with this receiver and bursts we cannot attribute to one radio*. Our own motion also Dopplers stationary sources, and the +/-100 Hz figure is a tolerance, not a measured per-radio offset. Since 17 November 2026: radios measured within about ±20 Hz of each other, the burst length is not the limit, the receiver's drift is (§7). |
| "The tail check breaks the 90 degree degeneracy"  | It does not. The fitted rotation still absorbs any of four phases; the tails add three constrained symbols (~64x fewer accidental matches), which is a weaker and different claim. |

**A finding of theirs that I verified and had missed:** at 6 MSPS centred on 382.5 MHz the
channeliser yields **193 channel centres, indices 3-195 (380.0875-384.8875 MHz)**. Uplink
channels **0-2 and 196-199 are never examined** - a 3.5 % blind spot at both band edges,
reproduced locally by calling `channelise()`. *(Fixed in 1.2.6, 17 November 2026: all 200 channels are examined now, and 0-2 and 196-199 are flagged `band_edge`.)*
