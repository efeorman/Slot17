## Slot17

| File | What it is |
|---|---|
| `install.sh` | The installer. On the car computer or server, run: `curl -fsSL https://github.com/efeorman/Slot17/releases/latest/download/install.sh \| bash` |
| `slot17.tar.gz` | Everything else. The installer downloads this by itself |
| `Slot17.ipa` | The iPhone app, unsigned. Sign and install it with Signulous or Sideloadly; see the README's step 4 |
| `Slot17.apk` | The Android app, signed. Open it on the phone to install; see the README's step 4 |
| `slot17-…-raspios-lite-arm64.img.xz` | The ready-made image for the Raspberry Pi: flash it with Raspberry Pi Imager and fill in `slot17.txt` on the card. See the README's step 1 |
| `slot17-update.json`, `slot17-update.tar.gz` | The signed update your server passes on to your car computer, so it updates itself |
| `SHA256SUMS` | Checksums, to check a download with `sha256sum -c SHA256SUMS` |

**New in 1.5.1:** Bluetooth connects by itself every time, also after restarting the app or the car computer, without the phone asking to pair (iPhone app 1.8.1, Android app 1.10.1); the recordings are written in batches, so an SD card keeps up with a busy band and a match reaches the phone without waiting for the storage; skipped recordings no longer mark the detector degraded, and `setup.sh doctor` says what the storage is and whether it kept up. Detection is unchanged. Signed updates bring the recorder and the app's Bluetooth fixes; for the pairing fix, run the installer again once (`bash setup.sh car --bluetooth`), then forget the car computer in the phone's Bluetooth settings.

**New in 1.5.0:** a **Support package** card on the apps' Pi screen (iPhone app 1.8.0, Android app 1.10.0): choose a quick check, one drive or everything from a drive, and the car computer makes one zip with its report, setup check and logs, without keys or passwords; add the phone's log, and places only if you choose; then share it from the phone, download it from the web page, or **Send to team** to the Slot17 team, who get a code from you and delete it after 30 days. Detection is unchanged. Signed updates bring it to the car computer; a server of your own needs the installer again.

**New in 1.4.0:** alerts also come straight from the car computer to the app over Bluetooth, besides the server: faster, and they keep coming without internet (iPhone app 1.7.0 on iOS 18, Android app 1.9.0); the setup asks about it and for the name phones show (**Slot17**), and the app's **Settings > Bluetooth to the car computer** sets it up, switches it and renames it; a match that comes both ways beeps once; the car computer sends to the server on its own thread, so a server that doesn't answer never holds anything up; `bash setup.sh logs` makes one zip for whoever helps you, without keys or passwords. Detection is unchanged. Signed updates bring the detector, the publisher and Bluetooth; for the setup question and `setup.sh logs`, run the installer again, on your own server too.

**New in 1.3.1:** a ready-made Raspberry Pi image (flash it, fill in one text file, start the Pi); the setup saves your phone's hotspot; the car computer updates itself with versions signed by the Slot17 team; `/server create` gives the car computer's whole setup as one line; a **Setup check** on the apps' Pi screen with the car computer's own check; the Android app updates itself.

**New in 1.3.0:** an Android app (1.7.0) with the same functions as the iPhone app, and hosted display servers: type `/server create` in the [Discord](https://discord.gg/rSFPWSWNfh) instead of renting a server. The car computer's setup accepts a hosted address (`name.slot17.cloud`, over HTTPS). Detection and the iPhone app are unchanged; a car computer that updates itself gets the new version number and nothing else.

**New in 1.2.9 (app 1.6.0):** switch the car computer's Pushover messages off and on from the app, and let the app show the same faults as its own notifications; `bash setup.sh doctor` gives a plain verdict per part; the server setup shows the private link as a QR code; the web page installs as a home-screen app; each match records which rule recognised it. The app adds Liquid Glass on iOS 26, Control Center controls and Lock Screen widgets for I SAW. Detection is unchanged. Signed updates bring the detector and publisher; for `doctor` and the QR code, run the installer again.

**New in 1.2.8:** the detector is ready about 30 s sooner after power-up: the full test suite runs before a start only when the code changed (and every 20th start), otherwise a few seconds of smoke tests. Needs the updated service file (run the installer again).

**New in 1.2.7:** a third recognition rule tests the training word coherently and finds markedly more weak bursts (field: 1,061 matches against 910), with 0 false matches in 338,438 control examinations. See FALSE-MATCHES.md.

**New in 1.2.6:** candidate checks are twice as fast with identical results, the recogniser finds more weak bursts (8 timing phases instead of 4; no false match in 284,438 field control examinations), and all 200 channels of 380-385 MHz are examined; the 7 outermost carry `band_edge: true`.

**New in 1.2.5:** samples lost in the receiver's first minute (while a 4G stick on the same USB controller starts) no longer mark the drive degraded or slow the checking down; they are counted separately. Each match also records its carrier frequency measured over the whole burst, far steadier than before, for research; it decides nothing.

**New in 1.2.4:** the interference catch-up of 1.2.2 and its counters of 1.2.3 are removed. It let interference candidates be checked up to 2 s late; on saved recordings it recognised nothing more, so it only added delay. `--impulse-catchup` and `SLOT17_IMPULSE_CATCHUP_S` no longer exist.

**New in 1.2.2:** a channel recognised in the last minute is checked first. App 1.5.1 has the sighting widget showing the last tap.

**New in 1.2.1:** app 1.4.9 shows on the Pi screen's Connection card how the phone and the car computer reach the server: round trips in milliseconds, which network the car computer uses and which it can fall back to, and the 4G stick's signal, quality, ping, roaming and cell. The detector is unchanged.

**New in 1.2.0:** a new detector window every 0.25 s with overlap, so no burst is cut and matches reach the phone sooner; a soft call tone in app 1.4.8 for a radio in a call; *Rising* on the meters; and a report on the server for every drive.

**New in 1.1.3:** `bash setup.sh modem` sets up a ZTE MF833N 4G stick so the phone's hotspot is only the fallback; after dropped samples the checking threads come back after five minutes instead of staying off for the whole drive; the stick keeps data roaming on and redials or restarts itself when it gets stuck; a push that stalls goes out over the other network at once; the logs say how the link is, in milliseconds and signal; and several small corrections. The app is unchanged (1.4.7).

**New in 1.1.2:** candidates are checked on several threads at once, as many as the computer's cores allow, so far fewer run out of time. One-core computers keep working as before, and the number can be set by hand.

**New in 1.1.0:** weaker bursts are recognised, the delay from burst to beep is cut from about 3 s to an expected 1 s, the car computer can update itself from signed releases, its log arrives by itself, and the app (1.4.7) shows the noise floor and interference live. Details, with what was tried and left out: [CHANGELOG](https://github.com/efeorman/Slot17/blob/main/docs/CHANGELOG.md).

Follow the [README](https://github.com/efeorman/Slot17#readme) from step 1. To update, run the installer again: it keeps your keys, settings, calibration and recordings.

**What a match means:** a TETRA radio transmitted. It does not identify a vehicle or measure distance.
