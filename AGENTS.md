# Instructions for AI coding assistants

Slot17 is a receive-only TETRA burst detector: an Airspy receiver on a Raspberry Pi in a car, a small
display server, and an iPhone app. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the full rules and the module
map; this file is the short version, for any coding assistant working in this repository.

## Hard rules

1. **Receive-only.** Never add a transmit path, decryption, decoding of content, or interception. This is
   about the radio band Slot17 listens to: the car computer's own data links to the app and the server (Wi-Fi,
   4G, Bluetooth since 1.4.0) never transmit there.
2. **Honest claims.** A match means a public TETRA training pattern was recognised. Code, text and UI must
   never claim police, identity, distance or direction from a signal.
3. **One receiver session.** Never write code or instructions that start, stop or reopen the Airspy
   repeatedly, restart it automatically, or reboot the computer with it attached. The phone can stop the
   detector and can never start it.
4. **No heavy work during a capture** on the car computer, and never a second program on the receiver.
5. **No clock at boot.** The car computer has no real-time clock. Never measure a duration with
   `time.time()`; use `time.monotonic()`. Never trust a capture folder's name; use `corrected_start_utc`.
6. **Nothing private in Git:** no captures, logs, GPS tracks, keys, tokens or passwords.
7. **Physical actions belong to the person:** plugging, unplugging, power and antennas. Never claim to have
   done one.

## Commands

```bash
pip install -r requirements-dev.txt
python -B p0/preflight.py --offline --tests      # detector tests; must end with "offline_preflight_pass": true
python -m unittest display/test_hardening.py display/test_app_backend.py display/test_display.py display/test_drive_report.py
python -m unittest scripts/test_doctor.py scripts/test_modem_link.py scripts/test_scorecard.py scripts/test_bluetooth_parity.py
ruff check p0 display deploy tests scripts       # must report no findings
```

The tests never open a receiver or send a real message. The iPhone app (`ios/`) needs a Mac; it builds and
tests in GitHub Actions (`.github/workflows/build.yml`), which costs paid minutes: change Swift carefully and
bundle app changes into one run.

## How to change things

- **Detection** (`p0/phy.py`, `p0/detect.py`, `p0/confirmation.py`): only after testing on saved
  recordings, with false-match controls (time-reversed snippets, generated signals without the training
  word). Record what failed as well as what worked in `docs/CHANGELOG.md`.
- **The API** between car computer, server and app: change `display/display_publisher.py`,
  `display/server.py` and `ios/Slot17/Core/Protocol.swift` together. The Bluetooth frames
  (`p0/bluetooth_frames.py`, docs/design/BLUETOOTH.md) change with both apps' `Bluetooth` decoders and a
  regenerated `shared/parity/ble-frames.json`. The server accepts only whitelisted
  fields; the app ignores unknown ones; a new operation is sent only to a car computer that lists it in
  its features, so older ones keep working.
- **Add a test that fails without your change.**
- **Line endings:** Python, Markdown, JSON and shell files are LF. `p0/*.py` is stored byte for byte
  (`-text` in `.gitattributes`) because source hashes are recorded with each session: keep each file's
  existing line endings.
- **Releases:** the car computer installs signed releases by itself. Never put a signing key in the
  repository.

## Where things are

| Path | What |
|---|---|
| `p0/` | The detector |
| `display/` | Server, web page, the car computer's publisher, and their tests |
| `deploy/` | Setup scripts, service files, `doctor.py` (starts `p0/doctor.py`, the setup check) |
| `ios/` | The iPhone app, Live Activity and widgets |
| `tests/` | Detector tests and small saved signals |
| `docs/` | Changelog, design notes, operations guides |

## Helping someone set Slot17 up

Follow [README.md](README.md) step by step, and [docs/SETUP-WITH-AI.md](docs/SETUP-WITH-AI.md). Ask for the
output of `bash setup.sh doctor` before guessing.
