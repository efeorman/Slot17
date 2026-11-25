# Working on Slot17

## Rules that do not change

1. **Receive-only.** There's no transmit path, and no decryption or interception. This is about the band Slot17 listens to; the car computer's own links to the app and the server (Wi-Fi, 4G, Bluetooth since 1.4.0) never transmit there.
2. **Honest claims.** A match means a public TETRA training pattern was recognised. Never claim distance, identity or "police" from a signal.
3. **One Airspy session at a time.** This board has hung after stop/start cycles and warm reboots, and only removing power clears that. Don't cycle it for diagnostics, don't reboot the Pi with it attached, and never reopen it automatically. The display can stop the detector and can never start it.
4. **Keep the evidence.** Don't overwrite captures or logs. Keep source and configuration hashes with each session.
5. **No heavy work during a capture.** Don't run replay or analysis on the Pi while it's capturing, and don't open a second receiver program. Judge health from a fresh heartbeat and completed windows.
6. **Nothing private in Git.** No captures, logs, GPS tracks, keys or passwords.

**This Pi has no real-time clock.** Never measure a duration with `time.time()`, and never trust a capture folder's name. Use `corrected_start_utc`.

## Evidence rule

| `confirmed` | Meaning |
|---|---|
| `true` | A public training pattern was recognised, not police or an ambulance |
| `false` | No match in the sampled regions; read the coverage too |
| `null` | Not checked, or an error |

A channel can carry several radios. Timing and signal trend can't establish identity, call type or motion.

## Where things are

| Module | What it does |
|---|---|
| `p0/watch.py` | Owns the process: USB setup, callback ring, worker, watchdog and session lifecycle |
| `p0/pipeline.py` | Energy candidates, fair confirmation scheduling, telemetry and recording requests |
| `p0/channelize.py` | 25 kHz channel power, with frequency calibration |
| `p0/detect.py` | Sensitive energy candidates and advisory timing; not protocol confirmation |
| `p0/phy.py`, `p0/confirmation.py` | Training-pattern recognition and its exact replay |
| `p0/recording.py` | Bounded background recorder with explicit write results |
| `p0/mark.py`, `p0/marking.py` | Human sightings and their short recordings |
| `p0/notify.py`, `p0/outbox.py` | Notifications that expire and retry; credentials are never stored in the queue |
| `p0/status.py`, `p0/preflight.py`, `p0/version.py` | Readiness, offline checks and source hashes |
| `p0/supervise.py` | Runs one detector and reports its failures |
| `p0/support.py` | `bash setup.sh logs`: the support package, one zip for someone who helps ([SUPPORT.md](docs/operations/SUPPORT.md)) |
| `p0/bluetooth_*.py` | The Bluetooth link to the apps: its frames, settings, BlueZ peripheral and own process ([BLUETOOTH.md](docs/design/BLUETOOTH.md)) |
| `display/server.py` | Display server and the app's API |
| `display/display_publisher.py` | Sends the Pi's state out, and carries commands back |
| `ios/Slot17/` | The app |
| `ios/Slot17Live/` | Live Activity and widget |

## Making a change

1. Change detection only after testing on saved recordings. Record what failed as well as what worked.
2. Run the tests in the [README](README.md#tests). Add a test that fails without your fix.
3. Change the app, server and publisher together when the API changes. The app's simulated drive runs against the real `display/` code.
4. Run `ruff check p0 display deploy tests scripts`. It must report no findings. If an exception is deliberate, mark that line with `# noqa` and the reason.

## Making a release

1. Set the new version in `VERSION`, `versions.json` and `p0/version.py`, add its paragraph to `docs/RELEASE-NOTES.md`, commit, and push: the **Tests** workflow runs by itself in the public repository.
2. Build the app with the `full` workflow on Codemagic or GitHub Actions.
3. Tag the commit, for example `git tag v1.0.1 && git push origin v1.0.1`. The **Release** workflow tests the setup on clean machines and runs every test again, and only then publishes `slot17.tar.gz`, `install.sh` and `SHA256SUMS`.
4. Add the apps to the release as **Slot17.ipa** and **Slot17.apk**, and add their checksums to `SHA256SUMS`.

## Deploying to a running system

1. Compare hashes on the target first. If it differs from what you expect, find out why before replacing anything.
2. Keep a copy of each file you replace.
3. Test the new files beside the old ones, then install only the files you named.
4. Stop the detector before replacing its code. Leave it stopped until the next power-up, rather than restarting the Airspy.
5. After changing `p0/`, record the new source hash from `python -B p0/preflight.py --offline`, with no detector running on that machine.

The publisher can be restarted while the detector runs, and so can the display server. Neither touches the receiver.
