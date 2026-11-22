# Testing

Offline tests show the software behaves as designed. They can't show live USB stability, antenna performance, phone delivery or detection range. Those need a parked check and a drive.

## 1. Offline checks

These never open the receiver or send a real message. Run them from the top folder. The commands and test counts are in the [README](../../README.md#tests).

The first one is the one that matters most:

```bash
python -B p0/preflight.py --offline --tests
```

**Expected:** `OK` and `"offline_preflight_pass": true`, with `"errors": []`. Stop here if it fails, and keep the output. It refuses to run while a detector is running on the same machine, which is not a fault.

The service runs this same check, with `--require-idle`, before every start. A failure prevents that start.

**Optional tools that need a recording:** `p0/validate_real.py` and one check in `p0/stress_test.py` need a real TETRA recording, `TETRA IQ.wav`, which isn't in this repository. Without it, `stress_test.py` reports that one check as failed and `validate_real.py` stops with a message saying where to put the file. Set `SLOT17_TETRA_WAV` to its path if you have one.

## 2. Parked check on the Pi

Do this once after installing or changing the detector.

1. Start one session. The command returns after the first completed analysis:
   ```bash
   sudo systemctl start tetra-detector.service
   ```
2. Check its status:
   ```bash
   python -B p0/status.py --heartbeat /run/tetra/watch.heartbeat
   ```
3. **Confirm these:**
   - `ready` is true, and the window count is rising.
   - The heartbeat is fresh.
   - `dropped` samples and the error counts stay at 0.
   - There's no power or heat warning.
4. **Read any warning.** "Receiver input very low" means the antenna connection is unverified. It can also mean a quiet location, and low input doesn't prove the antenna is disconnected.
5. **Check the display.** The app or web page should show the detector as live within about a minute.
6. Stop it:
   ```bash
   sudo systemctl stop tetra-detector.service
   ```

If it fails, read `journalctl -u tetra-detector.service -n 80 --no-pager`. Don't retry repeatedly. A wedged Airspy needs its power removed.

## 3. Drive check

Follow [FIELD-TEST.md](FIELD-TEST.md). A useful drive has:

- One session, started by power-up.
- A passenger marking sightings, including vehicles that gave no alert.
- A note of which antenna was used, and where.

Afterwards, read `summary.json` and `events.jsonl` in the session folder. Use `corrected_start_utc` from `session-time.json` for the time. See [RF-DEBUGGING.md](RF-DEBUGGING.md) for what each measurement means.

## 4. The iPhone app

The tests run in the cloud; see the [app README](../../ios/README.md#build-and-test). Then check these on a real phone, because a simulator can't:

- Sound with the screen locked, and through CarPlay.
- The Live Activity keeps up with the sounds.
- The widget's buttons reach the Pi.
- Closing the app fully stops the sounds.
- Bluetooth (1.4.0): Set up finds the car computer; with the hotspot and the stick off, a match still beeps;
  with both routes on, Settings shows which came first; after the engine starts, it reconnects by itself.

## 5. Installation

`scripts/test_setup.sh` tests the installer and both setups. It installs real services and permission rules, so only run it on a throwaway Linux machine with passwordless sudo:

```bash
bash scripts/test_setup.sh full            # release installer, server, car computer, update, uninstall
bash scripts/test_setup.sh distro debian:12   # packages and checks in a Docker image
```

The **Linux setup** workflow in GitHub Actions runs both, on Debian 12 and 13 and Ubuntu 22.04 and 24.04. It can't test the receiver, calibration or whether a server is reachable from the internet; those need real hardware.

## 6. Security and load

`display/stress_server.py` attacks a local copy of the server with slow connections, floods, malformed requests and wrong keys, while measuring whether real traffic still gets served. Only run it against a local copy.

## What a pass does not show

- That any match came from a particular vehicle.
- A detection distance.
- That a phone displayed a notification; a sent count only means the notification service accepted it.
