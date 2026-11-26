# Slot17: what it does, how it is built and why (8 November 2026)

Covers the iPhone app and the parts it depends on.

> **Since this was written (to app 1.5.1 and detector 1.2.5, 17 November 2026):** the detector starts a window every 0.25 s (1.2.0), not every 1.5 s; the app is installed with Signulous now, not Sideloadly; the position log is taken every 2 s, not 10 s (a server setting); the app has a call tone, *Rising* and a sighting widget that shows the last tap (1.4.8 to 1.5.1); ordinary tile changes wait at most 3 s, not 10 s; the lead-in before a sound is 0.05 s for the urgent beep and 0.15 s for others; a double tap within 3 s is one sighting; the widget has worked on CarPlay since 11 November; the Pi screen also shows the software version, the noise floor and whole-band interference. The test count and the decisions table below describe 8 November.

## The one thing to understand first

Slot17 recognises that **a C2000 TETRA radio transmitted**. It does not know who transmitted, what kind of vehicle it was or how far away it was. Beeps therefore mean "a radio was heard strongly", never "police nearby". On 8 November the operator marked 4 Police and 29 Saw nothing. Whether alerts give useful warning has not been shown.

The system is receive-only and does not decrypt or listen to any content.

## How the parts fit together

```
Airspy Mini -> Pi 5 detector -> publisher -> display server (VPS) -> iPhone app -> sound, tile, widget
                  |                                  ^                    |
           saves events and IQ                       |                    |
                  ^----- sightings, stop, power off -+--------------------+
```

| Part | Where | What it does |
|---|---|---|
| Detector | `p0/` on the Pi | Finds energy on the 25 kHz channels every 1.5 s, confirms the TETRA training pattern, records events |
| Publisher | `display/display_publisher.py` on the Pi | Sends the live state to the server, and picks up commands |
| Display server | `display/server.py` on the VPS | Streams the state to the phone, stores uploads, serves settings and sounds |
| Settings file | `display/app-config.json` on the VPS | Levels, spacing, colours, words and sounds, changeable without a new app |
| iPhone app | `ios/` | Decides on sounds, shows status, records sightings and location |
| Tile and widget | `ios/Slot17Live/`, `ios/Shared/` | Live Activity and CarPlay widget, drawn by iOS |

## What the app does

### Live screen and dashboard
- **Meters:** the strongest channels as 10-segment bars, with channel, dB and age.
- **Meter colours:** grey-blue is weak, yellow is silent, red is chime level, red with a white outline and STRONG is urgent level. Never green, because green reads as safe.
- **Connection chain:** phone, server, Pi and receiver each show their own state, so a fault can be placed.
- **Headline:** one line of overall status, such as "Detector running" or "No data from the car".
- **Sighting buttons:** Police, Ambulance, Fire, Other, and Saw nothing.
- **Dashboard:** a sideways layout for a phone holder, also available with the rotation lock on.
- **Banners:** server messages, Quiet mode, and receiver warnings.

### Sounds
| Sound | When |
|---|---|
| Chime | 35 dB and up since 8 November (was 22), at most every 30 s per channel and every 6 s overall |
| Urgent beep | 50 dB and up, at most one per second; in 1.4.1 a chime no longer holds it back |
| Link warning | No data for 30 s, once per outage |
| Receiver warning | The receiver input very low for 60 s |
| Blip | A widget tap was recorded (1.4.0) |

Nothing sounds below the chime level, or if the signal reaches the phone over 10 s late. A near-silent audio loop keeps iOS running the app. The app measures that sound really left its mixer, but cannot know that the car played it. The modes are over the music, music dips, or like a navigation voice.

### Sightings
- One tap records label, time and place, and sends it to the Pi, which saves a recording of the moments around it.
- A form allows a note and an earlier time.
- A summary card counts how often sounds were followed by a mark.
- Unsent sightings are retried until the Pi answers.

### Tile (Live Activity, Lock Screen, Dynamic Island, CarPlay Dashboard)
- Shows channel, dB, age, last beep, a meter and a large icon for the signal's level. The icon follows the level only: a bell can show during mute or a cooldown, when no sound played.
- Clocks count up on the device by themselves.
- Is set to say "Not updating" if no update arrives for 45 s. That is a setting, not a promise that CarPlay redraws on time.
- New beeps, problems, STRONG and sightings are sent immediately; other changes wait up to 10 s.
- A tiny `R` number counts updates, for comparing a photo with the log.
- It cannot have buttons: Apple does not allow that on CarPlay.

### Widget (Police and Saw nothing)
- Runs in the app without opening it. With the app already listening, it answers with a blip and a tick on the tile.
- A tap while the app is not listening is kept with no blip or tick, and goes to the Pi after the next open.
- **Never yet seen working on the phone:** it did not appear in the phone's widget list on 8 November.

### Pi screen
Pi health, session details, save a recording, collect the Pi's logs, stop the detector and power off, each with confirmation. It deliberately cannot start the detector, because repeated starts can wedge this Airspy.

### Logs and Settings
- **Logs:** alert history with reasons for silence, diagnostics, the app's own log, and what is stored on the server.
- **Uploads:** the app log and drive data can be sent by hand, and go by themselves 3 minutes after the car disappears or at the next open.
- **Settings:** private link, volume, sound mode, sound tests, levels, Quiet mode, warnings, sleep time, location recording, keep screen on, and the tile.
- **Rule of precedence:** server settings apply unless the operator changed that value in the app.

### When the app runs (changed in 1.4.1)
Sound, link and location start only when the person opens the app. If iOS starts it in the background, it stays silent and removes a tile left behind. When iOS announces that the app is closing, the tile is ended.

## How it is coded

- **Language and structure:** Swift and SwiftUI, with `Core/` free of the system so it can be unit tested, `Services/` for sound, network, location and storage, and `UI/` for screens. `AppModel` joins them on the main thread.
- **Alert decisions:** `AlertEngine` is pure logic: matches in, decisions with a written reason out. It is tested by replaying four saved drives against known counts.
- **Network:** a long-lived server stream, with a separate timing sample so an old message cannot look fresh. A monotonic clock is used for durations.
- **Sound:** `AVAudioEngine` with sounds generated from descriptions or WAV files from the server.
- **Tile:** ActivityKit, local updates only, one update in flight at a time, newest wins. The state is under the 4 KB limit.
- **Storage:** JSON files written off the main thread, the log written line by line, the private link in the Keychain.
- **Builds:** no Mac here, so an unsigned IPA is built in the cloud and sideloaded with Sideloadly.
- **Tests:** about 90 unit tests, UI tests of every screen, a pretend drive running the real server and publisher code, and checks of what the logs say really happened.

## How the decisions were made

| Decision | Why | Evidence, or what is still open |
|---|---|---|
| Sell nothing as "police detection" | The signal cannot show it | TETRA-SIGNALS.md; 29 Saw nothing today |
| Strength is never shown as distance | Radios set power by distance to the mast, over a 30 dB range | ETSI clause 23.4.4.2 |
| 1.5 s detector window (was 3 s) | Faster alerts | Burst to finished check: median 1.1-1.6 s, was about 3 s; 0 dropped samples |
| Chime level 35 dB | Fewer beeps, at the operator's choice | Replay: 44 to 15 an hour, all 4 Police marks still had a beep. **Not more accurate**, and marks follow beeps |
| Urgent at 50 dB | Rare, strong signals | Not validated as more meaningful |
| Settings on the server | Change levels without a new app | Worked for 35 dB on 8 November |
| Live Activity instead of a CarPlay app | CarPlay apps need an Apple entitlement; Live Activities do not | Works in the operator's car |
| Fewer tile updates | Apple asks for "only the most significant states" | Busiest minutes barely changed. **Not shown to matter** |
| `R` number on the tile | One photo showed the tile about 2 minutes behind | The operator saw it keep up on later drives. Cause unknown |
| Widget through `LiveActivityIntent` | Runs in the app, needs no App Group | Unproven on a real phone |
| Sound only after a real open | The log shows iOS restarting the app unprompted, and the operator heard sound after closing | **Trade-off:** if iOS kills the app mid-drive, it stays silent until opened, and the tile disappears or says "Not updating" |
| Boot autostart of the detector | The car cuts power; no manual steps on a drive | Requested by the operator on 12 September |
| No start button on the phone | Repeated starts wedge this Airspy | Documented wedges |

## Known mistakes and weak points

- **Wrong claims made along the way:** that the meters used only yellow and red; that Codemagic has no audio (never proven, and the docs do not say); a first explanation of overload that blamed the display.
- **Cloud minutes:** GitHub Actions allowance used up by long and repeated runs.
- **Test limits:** Codemagic's tests skip real sound and WAV decoding. Debug switches stand in for background launches and swipes, so they test the app's reaction and not iOS's behaviour. The widget test runs its button inside the app, not through the widget extension.
- **Evidence limits:** few independent sightings, and marks are prompted by beeps, so usefulness is unmeasured.
- **Unexplained:** the mid-drive restart at 14:25 on 8 November, and the one tile photo that was behind.
- **Receive-chain question:** possible receiver overload on strong signals.
