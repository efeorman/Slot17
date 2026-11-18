# Slot17 iPhone app

The app plays Slot17's sounds with the screen locked and while connected to CarPlay, shows the same information as the web page, and controls the Pi. It has no CarPlay icon, because that needs an Apple entitlement. The sound follows the phone's audio to the car, and a Live Activity shows the status.

**It reports that a TETRA radio transmitted. It can't identify a radio or vehicle or measure distance, and a silent vehicle is invisible.**

## Install

1. Download **Slot17.ipa** from the [latest release](https://github.com/efeorman/Slot17/releases/latest), or build it yourself; see [Build and test](#build-and-test).
2. Sign and install it:
   - **[Signulous](https://www.signulous.com):** $19.99 a year per iPhone, and works for a year. Register the iPhone on [signulous.com/register](https://www.signulous.com/register), ideally from Safari on the iPhone with **Link this device**, install their profile, then upload **Slot17.ipa** with their own-app signing. This is what we use, since app 1.5.1; the sighting widget's App Group then gets a name of Signulous's own, which the app finds by itself.
   - **[Sideloadly](https://sideloadly.io):** free with a normal Apple ID, from Windows or a Mac, but it stops working after 7 days. We used it at first.
3. On the iPhone, allow your profile under **Settings > General > VPN & Device Management**. You may also need **Developer Mode** under **Settings > Privacy & Security**.
4. Open the app and paste your private link, the one that opens the web page. It's stored in the phone's Keychain and is never built into the app.

Signing services depend on Apple's certificates, which Apple can revoke at any time.

## Daily use

Open the app once before driving, then lock the phone. iOS won't let an app start its audio from the background, so opening it is what arms the sound. It sleeps by itself after 30 minutes closed with no car data, so open it again to re-arm.

| Screen | What it does |
|---|---|
| Live | Status, the link from server to receiver, three signal bars, last alert and I SAW buttons |
| Dashboard | Turn the phone sideways, or tap the rotate button if rotation lock is on |
| Sightings | Everything you marked and whether the Pi saved it, plus adding one with a note |
| Pi | Health, Save a recording, Collect the Pi's logs, Stop the detector and Power off |
| Logs | Every alert with why it sounded or not and which recognition rule matched, diagnostics, and logs kept on the server |
| Settings | Private link, sound, levels, warnings, fault messages, the car computer's Pushover switch and demo mode |

**Fault messages (1.6.0).** Settings, Fault messages: the app can show the faults Pushover sends (detector degraded, receiver hears nothing, detector stopped, and once when all is well again) as its own notifications, and can switch the car computer's Pushover messages off and on. The app's notifications work only while the app runs, as it does with sound armed; Pushover has no such limit. Switching Pushover needs car computer software 1.2.9.

**Without opening the app (1.6.0, iOS 18).** Besides the Home Screen and CarPlay widget, Slot17 offers two controls, *Slot17: saw police* and *Slot17: saw nothing*, for Control Center, the Lock Screen's buttons or the Action button, and Lock Screen widgets with the same taps.

## Limits

- **Reaching the Pi:** the Pi only talks to the app while the detector runs, because the publisher exits with it. After Stop, Power off is no longer available from the app.
- **Power off:** coming back needs the power to go off and on. There's no start or reboot button, because cycling this Airspy can wedge it.
- **Recordings:** a sighting saves a recording only if it reaches the detector within about 5 seconds, and at most one per 10 seconds. Recordings stay on the Pi.
- **Security:** the display server uses plain HTTP, so the private link and everything sent with it, including Stop and Power off, isn't encrypted.
- **App Store:** the near-silent audio loop that keeps the app running breaks App Store rules, so this is for personal install only.

## Build and test

You need no Mac of your own. Builds start only by hand, so they don't use cloud minutes unexpectedly.

The release's **Slot17.ipa** comes from the `full` workflow below, so it has passed every test listed here.

| Where | How | Workflows |
|---|---|---|
| Codemagic | Add this repository, then **Start new build**. It reads [codemagic.yaml](../codemagic.yaml) from the top folder | `app-only`, `quick`, `full` |
| GitHub Actions | Run **Build** from the Actions tab, and choose how much testing | `none`, `quick`, `full` |

On a Mac with Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen):

```bash
cd ios
xcodegen generate
bash scripts/build-ipa.sh          # build/Slot17-unsigned.ipa
bash scripts/run-tests.sh          # everything; add NO_AUDIO=1 on a Mac with no audio system
```

**What `full` covers:**

| Test | What it covers |
|---|---|
| Unit tests | The sound rule, including sound counts from saved drives; links, timing and status |
| Closing checks | What happens when the app is closed, slept or never connected |
| Simulated drive | The app against the real `display/` server and publisher, with a stand-in detector and recorded stop and power-off commands (`scripts/e2e_harness.py`, checked by `scripts/check_e2e.py`) |
| Screens | Every screen upright and sideways, saved as pictures |

Simulators can't test CarPlay, a locked phone, real speakers or whether a real iPhone keeps the app running, and they don't enforce iOS background rules. Those need a real phone in a car.

On a test Mac with no audio system, the sound checks are skipped and one short iOS pause is allowed. Any other logged problem fails the run.
