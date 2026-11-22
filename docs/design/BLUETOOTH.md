# The Bluetooth link to the apps (1.4.0)

Besides the display server, the car computer can send alerts and the detector's state straight to the iPhone and
Android apps over Bluetooth Low Energy. It is faster, and it keeps working where there is no internet. The
server stays for everything else: logs, controls, updates and more than one viewer.

## Why, in numbers

From the car computer's own push times on the drives of 20 November 2026 (266 minutes):

| Route | Typical | Bad minute | Worst |
|---|---|---|---|
| Car computer to server, one push | 0.18 to 0.28 s | 2.8 s or more, 1 minute in 10 | 6.7 s |
| Phone picks it up | within 0.25 s, plus its own trip to the server | | |
| 4G stick without service | nothing, for minutes (3.5 min at the Elten border, 8 min on the first drive with the stick) | | |

Measured over Bluetooth from a Windows PC to the car computer, before anything was built (22 November 2026,
−54 to −65 dBm):

| Test | Result |
|---|---|
| Found by service UUID | 0.20 s |
| Connected | 0.92 s, MTU 517 |
| 10 frames a second for 16 s | 161 of 161 |
| 180-byte frames | all whole |
| Round trip, PC to car computer and back | median 79 ms, slowest 207 ms |
| Advertising again after a disconnect | 0.05 s |

The real code, before release, on the same car computer:
- **82 frames in 35 s:** 35 status, 35 channels, 12 matches.
- **Checks:** none refused for its signature, none as a replay, every match key right.
- **Rename and switch-off from the settings:** both worked.
- **Detector:** the drop counter stayed at its start-up value throughout.

A phone has not been tested yet; that is the next step.

## The parts

| Where | File | What it does |
|---|---|---|
| Car computer | `p0/bluetooth_frames.py` | The wire format: frames, their signature, the match key |
| | `p0/bluetooth_config.py` | On or off, the name phones show, and the key (`~/.config/slot17-bluetooth.json`, only its owner can read it) |
| | `p0/bluetooth_bluez.py` | A BLE peripheral through BlueZ's D-Bus API (Linux only; `python3-dbus`, `python3-gi`) |
| | `p0/bluetooth_link.py` | The link's own process: commands from the publisher in, frames out, its state back |
| | `display/display_publisher.py` | `BluetoothLink` runs that process while Bluetooth is on and feeds it; `run_bluetooth` carries out the app's switch, rename and pairing request |
| Server | `display/server.py` | Lets the `bluetooth` operation through and keeps the car computer's `bluetooth` block, both checked field by field |
| Both apps | `Bluetooth.swift`, `Bluetooth.kt` (core) | The same decoder, replay check, feed and route figures, tested against `shared/parity/ble-frames.json` |
| iPhone | `BluetoothLink.swift`, `BluetoothRoute.swift`, `BluetoothCard.swift` | AccessorySetupKit and Core Bluetooth; the route; the Settings card |
| Android | `BluetoothLink.kt`, `BluetoothRoute.kt`, `SettingsScreen.kt` | Companion device pairing and GATT; the route; the Settings card |

**The link's own process.** The publisher starts `bluetooth_link.py serve` as a child, because:
- signed updates bring `p0/` and the publisher, so no service file has to change;
- a fault in Bluetooth can never hold up a push to the server, because commands go into a non-blocking pipe
  and are dropped when it is full;
- it runs at the lowest priority (nice 19). Rule 5 applies: the session's drop counter must not move.

A link that keeps ending is started again after 5, 30 and 120 s, then every 10 minutes.

**Pushes on their own thread.** A server that doesn't answer can take one push several seconds. So pushes run on a thread of their own (`Pusher`), and the main loop keeps reading new matches and handing them to Bluetooth meanwhile. Without this, Bluetooth would have waited in exactly the outages it is for.

**The detector isn't slowed** by the publisher's retries: it is a separate process at Nice −5, the publisher runs at Nice 10 and the link at 19, and a failing push waits on the network rather than using the processor.

## On the air

| | |
|---|---|
| Service | `f9cb0001-aa93-4349-a576-0e70a736266f` |
| `frames` | `f9cb0002-…`, notify (and read: the last frame) |
| `info` | `f9cb0003-…`, read: `{"v":1,"name":…,"key_id":…,"software":…,"boot":…}` |
| `control` | `f9cb0004-…`, write: a signed control frame from a phone (switch off, rename) |
| Advertising | The service UUID and the name, which ends up in the scan response. No TX power: with a 128-bit UUID it would leave no room for the name in 31 bytes |
| Intervals | Apple's Accessory Design Guidelines (R32, chapter 57): 20 ms for 30 s after a start or a disconnect, then 152.5 ms. While a phone is connected, 1022.5 ms, so a second phone can still join |
| Name | The adapter's alias, `Slot17` unless changed: up to 20 letters, digits, spaces, `.`, `-` or `_` (Apple advises against `:` and `;`). BlueZ keeps it across restarts |
| Pairing | None. Apple advises not to require it to discover services; the signature below proves where a frame comes from, and the contents are no secret: anyone with an SDR hears the same band |

## Frames (version 1)

Little-endian. Each frame fits in 180 bytes, so one notification carries it once the phone has asked for a
larger MTU, which iPhones and Android phones do by themselves.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | version, 1 |
| 1 | 1 | kind: 1 status, 2 matches, 3 channels, 4 reply; 16 control (from a phone) |
| 2 | 4 | boot: random for each start of the link |
| 6 | 4 | counter: one higher for every frame in a boot |
| 10 | n | body |
| 10 + n | 8 | tag: the first 8 bytes of HMAC-SHA256(key, everything before it) |

| Kind | Body |
|---|---|
| Status, every 2 s | state (stopped, starting, running, degraded, stale), windows, hits, quiet windows, quiet-alarm windows, whole-band windows, heartbeat unchanged (tenths of a second), noise floor (tenths of a dB) |
| Matches, at once | a count, then per match: key (8 bytes), channel, strength (tenths of a dB), age (ms), rule |
| Channels, every 2 s | a count, then up to 8 rows: channel, frequency (100 Hz), strength, age (tenths of a second), hits in 60 s, flags (rising, call, approximate time, activity), the newest burst (4 bytes) |
| Reply | the action answered, done or not, and a few words (at most 80 bytes) |

**Control frames go the other way:** a phone writes them to `control`, to switch the link off or rename it
without the server.
- **Layout:** the same header with kind 16. Then the action (1 off, 2 rename), the name's length and the
  name, then the tag.
- **Boot and counter** are those of the newest frame the phone has from the car computer. The car computer
  takes the command only if:
  1. the tag checks out;
  2. the boot is its own;
  3. the counter names one of its last 300 frames (a few minutes);
  4. no command named that counter before.
- **A recorded command played back later** is therefore refused.
- **The answer** is a reply frame.
- **Switching on can't go this way:** a link that is off hears nothing. It goes through the server or setup.

**Matches carry exactly what the server passes on for a match**, so a sound never depends on which route
brought the match first.

**One match, one key.** The key is the first 8 bytes of SHA-256 of the match id. The apps compute it for matches
from the server too, and remember matches by key, so a match that comes both ways plays once. When the server's
copy arrives, an alert first heard over Bluetooth takes the server's id, so the drive report can still find it.

**Ages.** A match over Bluetooth is the car computer's age plus the time since the frame arrived: an upper bound,
like `LinkTracker`'s rule that ages may overstate but never freshen.

**Live.** The car counts as live when a fresh status (under 8 s, as for the server) says the detector runs.
Without the server, the app shows the car computer's own state and channels from Bluetooth on top of the
server's last report.

## Keys and replays

The key is 32 random bytes, made on the car computer the first time Bluetooth is switched on.
- **How it reaches the app:** over the display server, only in answer to the app's pairing request (the
  operation `bluetooth` with `pair`). It is never sent over Bluetooth, and never in a state.
- **What a state carries instead:** `key_id`, the first 8 hex digits of the key's SHA-256. An app checks it
  against `info` after connecting, so it notices a different car computer.

That server link is plain HTTP, the operator's choice; anyone who can read it already has the link key itself, so
the Bluetooth key adds no new weakness there.

An app takes a frame only if:
1. its tag checks out with the key;
2. within one boot, its counter is higher than the last;
3. its boot isn't an earlier one it has seen before (it remembers the last 32).

So a recording of the link, replayed later, cannot raise an alert.

## Switching it on and off

| Where | How |
|---|---|
| Setup | `bash setup.sh car` asks "Send alerts over Bluetooth too?", yes by default, and for the name (Enter keeps `Slot17`). `--bluetooth`, `--no-bluetooth` and `--bluetooth-name NAME` skip the questions. Skipped where there is no adapter, or no display server, which the app needs for the key |
| Ready-made image | `bluetooth=yes` or `no`, and `bluetooth_name=`, in `slot17.txt` |
| App, the car computer | Settings, Bluetooth to the car computer: the switch and Rename. With this phone connected, off and Rename go over Bluetooth at once; otherwise, and for on, through the server like the Pushover switch, where a change made while the car is off waits up to a day |
| App, this phone | Set up (the key through the server, then the system's chooser), Use Bluetooth on this phone, Remove |
| By hand | `python3 -B p0/bluetooth_config.py --show`, `--on [--name N]`, `--off`; `python3 -B p0/bluetooth_link.py adapter [--name N]` |
| Doctor | A Bluetooth line: off (a note), on with a working adapter, or what is missing |

**What the app shows:**
- **From the car computer:** on or off, waiting for a phone or connected, frames and matches sent, the MTU, and
  the last error.
- **From the phone:** connected or not, and the last frame so many seconds ago.
- **Which route was faster:** matches first over Bluetooth and first over the server, how far Bluetooth was
  ahead (median), and matches that came only over Bluetooth.

## The platforms

**iPhone, iOS 18 and later** (the app itself still runs from 16.2):
- **Set-up:** AccessorySetupKit's picker, with no Bluetooth permission prompt. From iOS 26, Bluetooth state
  restoration relaunches only apps that set their accessory up this way (Apple TN3115, 15 September 2025).
- **Connecting:** a Core Bluetooth connection request, which never times out, with a restore identifier.
- **Background:** `bluetooth-central` in `UIBackgroundModes`. AccessorySetupKit is weak-linked, so iOS 16 and
  17 still start the app.
- **Limits:** iOS starts sound only from an opened app. So after iOS ended the app, it reconnects, but beeps
  again only once it is opened. Bluetooth switched off in Settings, or a force quit, stop it.

**Android:**
- **Set-up:** the companion device chooser with the service UUID (Location must be on while it looks). Android 12
  and later ask once for Nearby devices (`BLUETOOTH_CONNECT`); the chooser does the scanning.
- **Connecting:** `connectGatt(autoConnect = true, TRANSPORT_LE)`, one GATT operation at a time. Android
  connects again by itself whenever the car computer is in range.
- **Background:** the foreground service adds the `connectedDevice` type once the permission is granted.
- **Not yet:** starting the app from scratch when the car computer appears (companion presence).

## Not in this version

- Bluetooth without any server: it needs another way to share the key.
- Other controls over Bluetooth (stop, power off, sightings). Switching the link off and renaming it work.
- The web page: Safari has no Web Bluetooth.

## Sources

- **Apple:**
  - TN3115, Bluetooth State Restoration app relaunch rules.
  - Core Bluetooth background processing (archive).
  - AccessorySetupKit, with `NSAccessorySetupKitSupports` and `NSAccessorySetupBluetoothServices`.
  - Accessory Design Guidelines R32 Update 1, chapter 57.
- **Android:** background BLE, Bluetooth permissions, foreground service types and their background-start
  rules, companion device pairing, `BluetoothGatt`.
- **BlueZ:** the D-Bus API documents for GattManager, GattCharacteristic, LEAdvertisingManager and
  LEAdvertisement.
