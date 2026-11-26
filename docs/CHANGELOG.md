# Changelog

What changed, why, and what was tried and left out. Newest first. Figures come from our own car, receiver and drives; yours will differ.

## Unreleased: the guides read as a newcomer, and three fixes they found (26 November 2026)

Every guide was read as someone setting Slot17 up for the first time, and checked against the code, by three
reviewers independently, then each finding was checked again before anything changed.

- **The setup no longer opens the receiver twice in one power-up.** It runs only with the detector stopped, so a
  re-run (an update, Bluetooth or the screen switched later) came after the detector had opened the receiver, and
  its default answers then calibrated or started it again: the second opening this project avoids, because an
  Airspy can stop responding. The setup now asks systemd whether the detector ran in this power-up (its last start
  time, or this boot's journal for a detector without autostart). If so it neither calibrates nor starts by default;
  an explicit `--calibrate` still works, with a warning. 4 new setup tests; each fails on the old script.
- **The web page takes the server's sound levels**, as the apps do: the chime from 35 dB and the urgent beep and
  call tone where `app-config.json` puts them, unless you set a level by hand in that browser. It chimed from its
  own 22 dB. The car computer's own screen has no server settings, so the page's own levels are now the ones
  `app-config.json` ships with (35 and 50 dB); a saved 22 that wasn't set by hand moves to 35 once.
- **The phone's status row** says "Car", on one line like its neighbours; it took two lines for "Car computer".
- **The guides:** the server key before the image; Imager's current screens and what to do when it offers no
  Customisation for the image; writing an NVMe SSD; `slot17.txt` is read only once (the first start's own note said
  otherwise); `--no-calibrate --no-start` when changing a setting later; SSH for a beginner; Sideloadly on Windows;
  the receivers of 1.7.0 in the FAQ; new keys on your own server; `sudo` for the server's own check; the car screen
  needs the server once; where the app sends your route; data roaming with the 4G stick; other bands aren't
  supported. New pictures of the web page and drive mode, and the build drawings with our 3 A charger.
- **A third read:** full paths (`~/Slot17/setup.sh`) for every setup command in the guides, and the detector stopped
  first where a setting changes later; `slot17.txt` says the first start stops without the SDRplay licence line,
  as it does; checking a session after a power cut starts from the drive list and doesn't claim that `final:
  false` proves a power cut; the 4G stick goes in the other blue port, on its own USB controller (`lsusb -t`);
  BUILD.md in the order you do it, software first; the RTL-SDR pair needs a splitter or two antennas; short
  explanations of ufw, ppm, SMA and dB; HTTPS on hosted servers in the iPhone guide; the FAQ's range answer.
- **A screen without a server:** the setup now says, when it sets up the car computer's screen without a display
  server connection, that the screen's data comes with that connection. 2 new setup tests.
- **Not changed, written down:** the image's first start waits for the user Imager makes, so an Imager that offers
  no Customisation for the file leaves the card waiting; the guides now say to use the manual steps then. A screen
  on a car computer with no display server at all still shows no data, because the part that feeds it comes with
  the server connection; the setup now says so.

## 1.7.1: the ready-made Raspberry Pi image again (26 November 2026)

1.7.0's software, with its Raspberry Pi image. 1.7.0's image was not built: inside the image, under ARM emulation
(about ten times slower than a Pi 5), three of 1.7.0's new checks failed. They send samples through the RSP1B and
RTL-SDR stand-ins at the real sample rate. Like five earlier checks, they are now left out there by name, and the Pi
runs them at its first start, before it opens the receiver. Detection and the apps are unchanged.

## 1.7.0: the cockpit web page, drive mode and the car computer's own screen; the SDRplay RSP1B and RSP1A, and a pair of RTL-SDRs (26 November 2026)

Detection is unchanged for the Airspy Mini, and so are the apps (iPhone app 1.9.0, Android app 1.11.0). Signed updates
bring the detector, the receiver code and the page for the car computer's own screen. The screen itself
(`setup.sh car --screen`) and a new receiver need the installer again, and so does a server of your own for the new web
page.

### The cockpit web page, drive mode and the car computer's own screen (25 and 26 November 2026)

- **The web page is now a cockpit with the app's five sections:** Live, Sightings, Car computer, Logs and Settings. Until now it was three meters and a settings sheet.
  - **Live:** the four status lights (server, car computer, detector, receiver), the meters with a STRONG mark, the last sound played, the I saw buttons, the matches recognised since the page opened, and the car computer at a glance.
  - **Car computer:** health, connection and Bluetooth, the setup check with where to start, software, and the actions the app has: save a recording, collect the log, a support package, the Pushover and Bluetooth switches, stop, and power off (two taps).
  - **Sightings and Logs:** the sightings made on this device with the car computer's answer, the car computer's logs and the support packages on the server.
  - The page's sound, timing and safety rules are unchanged, line for line; its 36 browser tests pass unchanged except where a control moved (switches instead of labelled buttons, stop under Car computer).
- **Drive mode:** one glance on a car mount. The strongest channel in very large figures, its bar, the other channels, and four large I saw buttons; red, with the urgent beep's line, during a strong burst. Landscape and portrait. Remembered on the device. **One bar or three:** the strongest channel's bar (the default), or the three strongest channels, each with its figure, its line and its own bar in its own colour; the button is at the foot of the left side.
- **Every screen size:** a sidebar on a computer, a tab bar on a phone, an icon rail on a short landscape screen (800 x 480, 1024 x 600), drive mode on any of them. Checked from 320 px wide to 1440 px.
- **The car computer's own screen** (`p0/screen.py`, `deploy/install-screen.sh`, `setup.sh car --screen`): the same page, full screen at every power-up, served by the car computer itself on 127.0.0.1. The publisher writes its whole state to a file in memory, so a match is on the screen within its 50 ms loop, with no server or internet. Requests from the screen are carried out like the app's, by id. It opens in drive mode at every power-up, whatever it was left on; one bar or three is kept on the car computer through power cuts. Chromium in kiosk mode, under cage on Raspberry Pi OS Lite or started with the desktop where there is one, at a low priority. [CAR-SCREEN.md](design/CAR-SCREEN.md).
- **Second pass, after the first look on the car computer (26 November 2026).** On its own screen the page looked like another app, and drive mode was out of proportion. Found by comparing the screen with the design files, not by the tests:
  - **The type.** The design uses Inter and Bricolage Grotesque; the car computer has neither and no internet, so Chromium fell back to DejaVu Sans. Both fonts are now inside the page (Latin, the weights it uses, 92 KB; SIL Open Font License, `display/fonts/`). Both pages' security policy allows `font-src data:` and nothing else new.
  - **Drawn to the design files' measurements:** the channel cards (name and STRONG on top, ten full-width bars, the strength, frequency, then chips for rising, a call and the rule), the header (the state as the title, the mute button with its words), the car computer's cards in columns that fill from the top (they left large holes with little data), and on a phone the newest channel large and the others as one line each.
  - **Drive mode is drawn at 844 x 390 and scaled as a whole**, so it keeps its proportions from a 7-inch screen to 1920 x 1080; standing up it stacks. With nothing to show it says why in words where the number goes; it showed a grey bar.
  - **Play again** on the last sound. **This drive** in the summary.
  - **The car computer's screen opens on Live** (or drive mode), not on whichever section a browser was left on.
  - **The screen's own message when the detector runs but no data reaches it** said the publisher was not running; an older publisher was running that did not feed the screen yet. It now says either may be the case, and the state is "no data" instead of "stopped".
  - Checked at 1920 x 1080, 1440 x 900, 1280 x 800, 1024 x 600, 800 x 480, 844 x 390 and 390 x 844, with a strong burst, a quiet channel and no data: no sideways scrolling, nothing of drive mode off screen, both fonts loaded, no script errors. 4 new browser tests (46).
- **Third pass, with the car computer's own data (26 November 2026):** with no channel heard, Live showed three dashed placeholder cards that read as "still loading"; one panel now says "No TETRA heard", or the detector's state and its reason. The icon rail and the tab bar cut "Car computer" to "Car compu…"; they say "Car". Drive mode showed an empty bar under its words; it is hidden.
- **Fourth pass, at the operator's request (26 November 2026):** the car computer's screen opens in drive mode at every start (it opened in the last mode); drive mode shows one bar or three. Drawing them found the alert line cut at its longest: "Urgent beep · 10 s ago" ended in "ag" in landscape, and standing up "Urgent beep · 2 s ag". The design's top line has only Mute; this one also has Cockpit. Where it must, the line now breaks after the dot, within the buttons' height. 4 new browser tests and one rewritten (53); each fails on the earlier page.
- **Tried and left out:** a separate Python program for the screen. It would be a fourth interface to keep in step with the apps, for a saving in memory (about 300 MB) the Pi 5 doesn't need.

### The SDRplay RSP1B and RSP1A, and a pair of RTL-SDRs (24 and 25 November 2026)

Nothing changes for an Airspy Mini: its code moved into `p0/receiver_airspy.py` without changing a call, and a test
replays 1.6.0's exact sequence of library calls. On our own car the new receiver layer has run its Airspy Mini since 26
November: ready within a minute of power-up, and no samples lost in four hours.
[RECEIVERS.md](design/RECEIVERS.md) describes all three receivers and the checks to run with each.

- **The receiver is chosen once**, by `setup.sh car` from the USB IDs (or `--receiver airspy|rsp1b|rsp1a|rtlsdr2`;
  `receiver=` in `slot17.txt` for a prepared card), and kept in `~/.config/slot17-receiver.json`. It is never
  guessed at a start. Only that receiver's code is loaded. The doctor, the status, the supervisor's messages and
  the boot message name the receiver; for the Airspy their words are unchanged.
- **SDRplay RSP1B and RSP1A**, through SDRplay's API 3.15 at 6 MSPS: zero IF, 5 MHz, fixed gain, AGC and bias-T
  off. `deploy/install-sdrplay.sh` downloads SDRplay's installer, checks its SHA-256 and runs it. It shows
  SDRplay's licence, which forbids passing the API on and allows it to send receiver details to SDRplay; the API
  runs a root service. Lost samples are found from the API's sample numbers, and overloads are counted.
- **A pair of RTL-SDRs**, at 381.25 and 383.75 MHz, joined into the 6 MSPS band by FFT (`p0/rtl_combine.py`),
  handing over at 382.5 MHz, between two channels. A realignment of the two sticks' clocks is counted, not a
  fault. Lost samples are found from the timing of the USB transfers, from 20 ms up. `calibrate.py` gives each
  stick its own ppm. `deploy/install-rtlsdr.sh` installs Debian's librtlsdr and keeps the DVB-T driver off the
  sticks.
- **Tests: 311** (235 before), plus checks against each receiver's own header files, its real library where it
  could be loaded without installing, and a stand-in library written in C. On the Pi 5, the RTL pair's joining
  costs 38 % of a core, and the whole detector on the pair 95 % with six busy channels.
- **Tried and left out:**
  - 2.4 MS/s per stick: six channels out of reach, and bursts beyond a stick's reach recognised on the wrong
    channels.
  - Longer FFT blocks: no cheaper (28 to 38 % of a core for every shape tried).
  - A table look-up for the sticks' bytes: five times the cost of arithmetic on the Pi, for the same values.
  - Reporting realignments as lost samples: that would have marked every long drive degraded.
- **Fixed after a second engineer's review** (24 November 2026). Each finding was reproduced or traced
  before fixing, and each fix has a test that fails without it. 322 detector tests (311 before them).
  - **The RTL join's block edges.**
    - **The fault:** the overlap was discarded at the start of each block only. The zero-phase weighting wraps
      round both ends, so the last 16 samples of every 2 ms block had an error larger than the signal.
    - **How it was measured:** against many tones off the FFT's bins, evaluated exactly.
    - **Now:** 0.5 ms is discarded at each end. The error is the same everywhere (−47 dB), at the same cost,
      and the stream is 0.5 ms behind the sticks.
    - **Why it was missed:** the earlier tests compared the stitcher with itself.
  - **The RTL pair's overflow:** when both sticks' waiting rooms overflowed, the two losses were added, twice
    the time. The larger total was reported next. That was wrong too (second round, below).
    `backlog_max_ms` shows one stick's reader running behind.
  - **Calibration's choice of captures:**
    - **The fault:** any pair capture among the files made the newest pair capture win, so an older pair
      capture overrode the Airspy just captured. A pair's captures also replaced another receiver's calibration.
    - **Now:** only captures made like this receiver is set up (kind; for the pair also its sticks' serials and
      ppm settings) are measured together. Setup measures exactly the capture it made.
    - **Writes:** calibration files are written whole and synced.
  - **Setup:**
    - The receiver choice is saved only after its driver is in.
    - A start asked for at first boot waits until the whole receiver chosen is plugged in.
  - **The installers:** the DVB-T blacklist check reads only the `*.conf` files modprobe reads. SDRplay's
    service must be enabled for power-up, not merely running.
  - **The doctor:** an unreadable receiver settings file is a problem, as it is to the detector, not a working
    Airspy.
  - **SDRplay:**
    - A batch the ring refused is counted whole (8 lost had been reported as 4).
    - Failed API calls are kept in the status.
    - After a failed Uninit, nothing is handed on from the closing thread.
    - A free receiver is preferred over one in use.
  - **The verifiers:**
    - A compiler other than zig checks only its own target.
    - A missing pycparser makes the check incomplete rather than passing.
    - Results are compared for signedness.
  - **Left as a limit, written down in RECEIVERS.md:** on the pair, a loss is marked where it is found, up to
    5 s after it happened. One stick queueing for over 5 s can't be told from a loss.
- **The review's second round** (the same session, 24 November 2026, evening). 344 detector tests, plus 94 in
  `scripts/`.
  - **A test the updater's copy couldn't load (F13):**
    - **The fault:** setup's order was tested in `tests/`, by reading `deploy/setup-car.sh` when the module
      loaded. The updater tests a release in a copy of `p0/` and `tests/` only, so that module failed to load
      there, and every release of this branch would have been rejected. The full checks on the PC and on the
      Pi both have `deploy/`, so neither saw it.
    - **Now:** those tests run setup's own steps in bash (`scripts/test_setup_steps.py`). The released code
      gains a test that every test module loads in the updater's copy.
  - **The updater's time:**
    - With that fixed, the copy took 138 s on the development PC, against the 70 s it has on the car computer.
    - The tests that run the whole detector on stand-in receivers, and the pair's calibration on synthetic
      captures, now run in the full checks only. The branch then costs about 1.23 times the released code.
    - It still needs measuring on the Pi before a release.
  - **One loss rule for the pair (F17):** the joined stream's loss is the smaller of what each stick has
    passed over: overwritten, skipped to realign, owed, and lost on USB. The larger of the two overflow
    totals had lost a later overflow of the other stick.
    - The same rule now reports the stretch a realignment there and back passes over. A reader late by
      15 ms for 7 s used to shift every later timestamp by 15 ms, unreported.
    - Checked against the test's own count of what was delivered and joined.
  - **The join's 0.5 ms delay** now dates the session's start (F15).
  - **SDRplay's end of session (F16):** a closing gate in both callbacks; a last batch that can't be handed
    on is counted; after a failed Uninit the receiver is kept alive to the end of the process; a second
    close does nothing.
  - **Calibration:** pair captures are grouped by their handover frequency too (F14).
  - **Smaller ones:**
    - The pair needs exactly two sticks for setup's check (three passed).
    - Receiver settings writes sync their folder.
    - A same-named file in an earlier modprobe folder hides a later one.
    - SDRplay's already-installed path needs the service enabled.
    - The verifiers compare a compiler's system as well as its processor.
    - The receiver capture's levels count the negative rail (as F27 on released code).

## 1.6.1: the Bluetooth key stays off the Bluetooth link, the second review's fixes, and tests on every change (25 November 2026)

Detection is unchanged, and so are the apps (iPhone app 1.9.0, Android app 1.11.0). Signed updates bring the
detector, the publisher and the updater's own fixes; the setup's and the image's fixes come with the installer or a
new image.

### Tests on every change

- **A Tests workflow** (`.github/workflows/tests.yml`) runs the detector's tests on the car computer's Python and
  libraries, the server's and publisher's on Python 3.10 (the display host's) and 3.13, the scripts' tests and the
  code checks. It runs on every push to `main` and every pull request in the public repository, where it costs
  nothing. Until now no workflow ran them.
- **A release waits for it.** The release's installer test passes whether the detector's own checks pass or not,
  since a detector without a receiver fails either way; now the release is published only after both.
- **A skipped test is no longer silent.** The offline checks list skipped tests by reason (`tests_skipped`), and
  note when the `cryptography` package is missing. Without it, all 27 of the updater's tests used to skip and the
  checks still said "pass". With `SLOT17_REQUIRE_CRYPTOGRAPHY=1`, as the Tests workflow and the release signing
  set it, a missing package fails instead. A car computer never sets it: without the package its updates are off,
  and its detector still starts.
- **Tools that act on a machine refuse arguments they don't know.** `deploy/modem_link.py`, which runs as root on
  the car computer, used to act whatever it was given, `--help` included. Now `--help` only prints its description
  and anything else is refused.
- **`SHA256SUMS` lists the apps too**, from this release on, as CONTRIBUTING.md says.

### Fixes from the second review of the released code (24 November 2026)

A second engineer reviewed the whole repository on 24 November 2026. These are its findings about code
that is already released (F18 to F30), each reproduced first and each fixed with a test that fails on 1.6.0.
The record of the review and of every check is kept with the project's own records.

- **Signed updates (`p0/update.py`):**
  - The signed checksums now stay with a waiting release. The files are checked against them again before they
    are tested, and as each one is installed. A file damaged on the card is fetched again; it isn't installed.
    Until now only the file names were kept after the download.
  - An install that can't be finished puts the old files back, and removes the files the release added. The
    old release is then whole again, and the new one is fetched again.
    - **After a power cut too** (the review's third round): a file the release had already added is no longer
      backed up on resume as if it were the old release's, so putting back removes it.
    - **A cut after the new install record** only finishes the cleanup at the next start. Before, it replaced
      everything again, and a failure there put the old files back under the new release's record.
    - **A backup folder lost in a power cut** (fourth round): new folders are now made durable in their parents.
      The journal keeps each original's checksum, and only a file that still holds it is backed up or put
      back. When an original can't be restored, the updater says so and keeps the journal for the next start,
      instead of reporting "put back" with new contents under the old release's number.
  - Backups are written through a temporary name. A power cut during a backup used to leave a short file that
    counted as the backup.
  - Every rename is made durable, the folder too, before the next step relies on it.
  - A release older than the installed software, going by `p0/version.py`, is refused. This also holds on a
    car computer with no record of a release yet (a fresh install from the tarball or the image). The number
    alone let an older signed release in there.
  - Checks that ran out of time, or couldn't run at all, are tried again at the next start, up to three starts.
    Until now a slow card rejected a good release for good.
  - A new test loads every test module in the copy the updater actually tests (`p0/`, `tests/` and the
    publisher only). The test of a broken release now also checks why it was rejected: a release can be
    rejected because a test module couldn't load at all, and that test would still have passed.
- **One opening of the receiver per power-up (`deploy/setup-car.sh`):** accepting both default answers used to
  calibrate (one start of the Airspy) and then start the detector (a second one), which the project's rules
  forbid for this board. Now a calibration leaves the start to the next power-up, even when `--start` was
  given. The test runs setup's own calibration and start steps in bash, with stand-ins, and counts the
  openings.
- **The image's first start (`deploy/image/`):**
  - The installer's sudo grant for everything used to stay on the card after a power cut during the setup. It
    now goes first thing at every first-start run, and a tmpfiles rule removes it early at every boot.
  - A failed removal of the key and hotspot password from `slot17.txt` was still reported as done. Now the
    file is read back, and the setup counts as finished only when no secret is left in it. That removal is
    tried again at the next start without running the setup again.
  - `slot17.txt` with old Mac line endings kept its secrets.
- **Clocks:**
  - The supervisor's READY check, the heartbeat's `ready` flag and a sighting's age (which decides whether it
    keeps its recording) now use the monotonic clock. Before, a UTC step between two readings could hold back
    READY or cost a fresh sighting its recording.
  - `antenna.py`, `drop_probe.py` and `stream.py` measure durations on the monotonic clock, as the project's rules
    require.
- **READY is sent again** when it couldn't be sent. Before, it counted as sent, and systemd stopped a working
  detector at its start limit (with `Restart=no`, for the rest of the drive).
- **Support packages:**
  - With a time window, only the recordings made in that window go in. Until now every recording went in.
  - Recordings are copied in paced pieces, like everything else in a package.
  - A window on a drive with no corrected start time (an interrupted first minute) can't be applied, so no
    recording goes in, and the manifest says why. Before, every recording did.
- **Capture metadata:** a capture clipped on the negative rail used to report no peak and no clipping. In
  int16, abs(-32768) is -32768 again; the samples are now widened first.
- **The Pi image workflow (CI):**
  - A run started by hand now builds the tag's own scripts and first-start files, not the branch it was started
    from.
  - The image records what it was made from: the Slot17 commit, the tarball and the base image with its
    SHA-256.
- **Not changed here:** the `SHA256SUMS` of 1.6.0 and earlier list the tarball and the installer only, not
  `Slot17.ipa` or `Slot17.apk`. Those published files stay as they are.

### The Bluetooth key stays off the Bluetooth link (24 November 2026)

Found in a code review (24 November 2026), and confirmed by running the publisher.

- **The fault in 1.6.0:**
  - When a phone set up Bluetooth, the car computer's answer, `key:` followed by the link key, also went into
    the answers it sends in its Bluetooth state, for 5 minutes.
  - That state is signed, not encrypted. SECURITY.md says so: a phone nearby can read it without pairing.
  - So a phone listening nearby during those 5 minutes could have taken the key, and with it signed the
    requests the link accepts: stop, power off, the Pi log, sightings and the Pushover switch.
  - 1.6.0's own notes say the key goes "only through the server". It did go there, but not only there.
  - The answer was also kept in `~/.cache/slot17-ops-seen.json`.
- **Now:**
  - A pairing answer goes only into the answers for the server, which hands it to the phone that asked. It
    leaves the Bluetooth state's answers, and nothing remembers it on disk.
  - A pairing asked again with the same id runs again, which gives the same key.
  - 1.6.0's file is cleaned when it is read: rows with a key are dropped, and the file is written again
    without them.
  - The apps need no change. Both read the key from the server's answer (and they also apply the Bluetooth
    state's answers, which is why the pairing answer is left out there rather than blanked).
- **A new key:** `python3 -B p0/bluetooth_config.py --new-key` replaces the key, keeping on/off and the name.
  - The link uses the new key once the publisher restarts: at the next power-up, or with
    `sudo systemctl restart tetra-display-publisher`. That restart doesn't touch the receiver.
  - Every phone then needs setting up again.
  - There was no way to replace the key before.
- **Tests:**
  - Two new ones in `display/test_app_backend.py`, both of which fail on 1.6.0:
    - the key is in neither the Bluetooth state nor the line handed to the link, decoded;
    - the file on disk holds no key, and an old one is cleaned.
  - One for `--new-key` in `tests/test_bluetooth.py`.
- **What to do after installing it:** if a phone was set up on 1.6.0 where strangers could have been in
  Bluetooth range, run `--new-key` and set the phone up again.

## 1.6.0, iPhone app 1.9.0 and Android app 1.11.0: everything over Bluetooth (23 November 2026)

The operator's first drive with 1.5.1, with an iPhone and a car computer that had no internet. The detector's recognition is unchanged. [BLUETOOTH.md](design/BLUETOOTH.md) describes the link.

- **What they saw:**
  - "Live over Bluetooth", with Pi and Detector red.
  - Actions that said they could not work.
  - "A phone is connected" for the car computer, and "Not set up on this phone" for the phone.
  - They expect Bluetooth to carry everything the internet carries.
- **Read from the car computer's journal and the display host's:**
  - **The detector stopped by itself after 80 s.** The car computer had undervoltage, and the Airspy dropped off USB at 60 s. With one stream attempt it stayed stopped, as designed.
  - **The publisher ended 30 s later, and Bluetooth with it.** After that the phone heard nothing, not even "stopped".
  - **Two set-ups pressed in the car waited on the server.** They only reached the car computer when it was home on a cable.
- **Causes in the apps, both of them:**
  1. The status chain read only the server.
  2. The actions checked the detector through the server only.
  3. "On the car computer" was the server's last report, however old.
  4. **iPhone: "Not set up" while frames came in.** Forgetting Slot17 under iOS Settings (the 1.5.1 instructions) removes the AccessorySetupKit accessory. The app set its accessory to nil but never cancelled its connection request, so iOS still connected.
  5. Setting up always asked the server for the key, even when the phone had it.
  6. **The link-lost tone listened only to the server.** In a car without internet it would have played while Bluetooth was live.
- **The link now carries everything** (frame kinds 5 and 6, control action 3; old apps ignore them):
  - **State:** every 2 s the whole state the server gets, raw DEFLATE in signed pieces, with the answers to operations. Measured on our car computer: 2669 bytes, 961 packed, about six frames.
  - **Operations from the phone:** sightings, recording requests, stop, power off, the Pushover switch, the setup check, the Pi log and support packages. Each is the same JSON as for the server, in signed control frames. The car computer carries an id out once, whichever route brings it, and remembers its answers across a restart (`~/.cache/slot17-ops-seen.json`).
  - **Fallback:** an operation with no word over Bluetooth within 8 s also goes through the server.
  - **The Pi log** comes straight to the phone over Bluetooth, at about 14 KB/s so a match never waits behind it. It shows under Logs, On server, and is kept on the phone.
  - **Still only through the server:** switching Bluetooth on, and the link key.
- **The car computer:**
  - **The publisher no longer ends with a detector that stopped by itself.** It reports "stopped" with the reason from the session's summary (for example `StreamStall`), so the phone can still read the Pi, get its log or power it off. `systemctl stop` still ends both.
  - **Its state over Bluetooth keeps its own 2 s rhythm,** so a server that hangs cannot hold it up.
  - **A stopped detector's reason** reaches the server as well.
- **The apps:**
  - **Lights, line and receiver warning** follow Bluetooth when the server has nothing fresh. The Pi is "Over Bluetooth", and a detector that stopped by itself is red: "Stopped by itself".
  - **The link-lost tone** counts Bluetooth too. It plays when the detector stopped by itself, not only when the link is lost.
  - **The Pi screen and its actions** work over Bluetooth. Only a sighting needs a running detector.
  - **Set up again needs no server** when the phone has the key: on the iPhone the picker, on Android the chooser. The server is asked only for a first key or a different car computer.
  - **iPhone:**
    - A removed accessory's connection is cancelled.
    - A connection without an accessory is dropped, so the app never shows data from a link it says is not set up.
    - The pairing hint says to tap Set up after forgetting Slot17 under iOS Settings.
  - **Old reports are labelled:** "On the car computer" says "As the car computer last reported, … ago" when it is the server's old report.
  - **A saved sighting stays saved,** even when the server's copy expires later.
- The link is tested with unit tests on all three sides, the car computer and both apps, against the same shared fixture.
- **Tests:**
  - Detector: 235 (10 new: pieces, files, operations, the link).
  - Publisher and server: 9 new.
  - Android core: 5 new; iPhone: 5 new (built on Codemagic).
  - `shared/parity/ble-frames.json` has messages and operations. Its compressed state is a constant, because zlib and zlib-ng compress the same input differently.

## 1.5.1, iPhone app 1.8.1 and Android app 1.10.1: Bluetooth without pairing, and storage that keeps up (23 November 2026)

Two problems from the field, one from the operator's iPhone and one from a user's SD card. The detector's recognition is unchanged.

- **Bluetooth asked to pair at every connection.** On the operator's iPhone a pairing code came up at every connection and after every restart of the app; without confirming it the app had no data from Bluetooth, and setting up said "Application is not in the foreground".
  - **Cause, on the car computer:** BlueZ acts as a GATT client towards a phone that connects. It found the iPhone's own services: Device Information and iOS 18's LE Audio media and call control, cached in `/var/lib/bluetooth`. They are protected, so BlueZ raised the link's security and the phone asked to pair. With no agent on the car computer the pairing was never kept (`bluetoothctl devices Paired` was empty).
  - **Fix:** the setup writes `[GATT] Client = false` into `/etc/bluetooth/main.conf`, with a dated copy, and restarts Bluetooth: Slot17 needs BlueZ only as a server. Signed updates cannot change system files, so the doctor and the app's Bluetooth card say when the setup is still needed. Applied on our car computer on 23 November.
  - **The apps:**
    - The iPhone picker waits until the app is on screen instead of failing.
    - "Set up again" with a car computer already chosen only refreshes the key.
    - Both apps connect again 4 s after a failed read, a missing service or refused notifications, instead of staying connected and silent.
    - Android took a failed read for a different car computer; it no longer does.
- **An SD card could not keep up with a busy band.** A user's drive had 42 candidates a second: 161 of 16,652 snippets were skipped, with 12.5 % iowait, and the detector reported itself degraded for it.
  - **Cause:** every recording (a candidate's snippet, each window's power table) cost two `fsync` calls, its file and its sidecar. ext4 commits its journal on each, which is slow on flash. `events.jsonl`, which the publisher reads for matches, was written by the same thread, so a match could wait behind a recording.
  - **Fix:** events have their own thread and never wait for a sync. Recordings go in batches: everything queued is written to `.tmp`, made durable with one `syncfs` on Linux (one `fsync` per file elsewhere), then renamed. The queue holds 4096 recordings instead of 256, within the same 256 MB.
  - The power-loss promise is unchanged: nothing is renamed before its data is on storage, and about one second is at risk.
  - **Measured** with `scripts/bench_recorder.py` (each sync made as slow as a card's, on a PC):

    | Sync time | Old recorder | New recorder |
    |---|---|---|
    | 15 ms | matches up to 89 ms late | matches at most 12 ms late |
    | 50 ms | 126 of 461 recordings skipped; matches up to 241 ms late | none skipped; matches at most 8 ms late |
    | 150 ms, at 80 candidates a second | 558 of 841 skipped | none skipped; matches at most 18 ms late |

  - **Real `syncfs`** tested on our car computer's ext4.
  - **Skipped recordings** no longer mark the detector degraded: detection, matches and events are untouched. They are still counted, and the doctor's Storage line says what the storage is (SD card, NVMe SSD, USB drive) and how many recordings it could not keep up with.
  - **Candidates** are no longer printed one line each to the system log, at dozens a second; matches still are, and every candidate is in `events.jsonl` (`--log-candidates` brings the lines back).
  - **Sources:**
    - Raspberry Pi Ltd, *Making a more resilient file system* (2026): write to a temporary file, `fsync`, rename; fewer and larger writes on flash.
    - The ext4 and JBD2 notes behind it: a journal commit on every `fsync`; batching small syncs.
- **Tests:**
  - Detector: 225 (10 new: 7 in `tests/test_recorder.py`, 3 for the BlueZ setting).
  - Doctor: 2 new; publisher and server: 1.
  - Android and iPhone: the new state field.

## 1.5.0, iPhone app 1.8.0 and Android app 1.10.0: support packages from the app (22 November 2026)

The support package of 1.4.0 (`setup.sh logs`) can now be made from the app, shared from the phone, and sent to the Slot17 team. The detector's recognition is unchanged. [SUPPORT.md](operations/SUPPORT.md) describes it.

- **Why:** someone whose car computer misbehaves, far away, should not need SSH and `scp` to give us what we need. Asked for when a user's drives had to be reviewed.
- **The app's Support package card** on the Pi screen: Quick, One drive or Everything; for a drive, a list of the newest 12 with their corrected start, length, matches and faults, and optionally only minutes A to B; recordings with Everything on request. **Include this phone's log** is on; **Include drive data with places** is off and asks first.
- **The car computer** makes the package with `p0/support.py` in its own process at the lowest priority (`nice 19`, at most an hour), then uploads it to the display server in pieces of 768 KB. The server answers with what it has, so a lost piece is sent again from there, and the whole is checked against its SHA-256 before it is kept. The push says how far it is, and the app shows it.
- **The server** keeps the newest three packages (150 MB at most, 120 MB each), adds the phone's newest log and, only when asked, its drive data, and lets the app list, download, delete and send them. A request for a package may take two hours to answer instead of three minutes; found while checking the README.
- **Share** downloads the package to the phone and opens the share sheet. The web page's settings list the packages with a Download link.
- **Send to team:** the server sends it (at most 50 MB) to the Slot17 team's inbox at support.slot17.cloud, only with the person's consent, and gets back a code to tell us and a token it keeps. **Take back** and **Delete** remove it there too. The inbox takes only zips made by `support.py`, at most 25 a day per sender and 300 in all, can't be read from the internet and deletes each package after 30 days. The privacy page on slot17.cloud says this in all eight languages.
- **Also:** the README and SECURITY.md said the car computer accepts no incoming connections and listed fewer requests than it knows; both now describe Bluetooth, every request and the support packages, and that a hosted server uses HTTPS. The README's doubled sentence under Updates by themselves is gone.
- **Tests:**
  - Server and publisher: 8 new (a package from the app's request to its download, pieces resumed, a wrong checksum, the limit of three, the phone's files, places only when asked, consent, take back, who may ask, the longer answer time).
  - The inbox: 7, against a real server on a free port.
  - Android core: 3 new; iPhone: the same 3.

## 1.4.0, iPhone app 1.7.0 and Android app 1.9.0: Bluetooth to the app (22 November 2026)

Alerts and the detector's state now also go straight from the car computer to the apps over Bluetooth Low Energy, besides the display server: faster, and they keep coming where there is no internet. The detector's recognition is unchanged. [BLUETOOTH.md](design/BLUETOOTH.md) describes it.

- **Why:** pushes to the server took 0.18 to 0.28 s in a typical minute, 2.8 s or more in 1 minute of 10 and up to 6.7 s; with the 4G stick out of service nothing reached the phone for minutes (3.5 at the Elten border, 8 on the first drive with the stick).
- **Measured before building**, a PC against our car computer, at −54 to −65 dBm:
  - Found in 0.20 s, connected in 0.92 s, MTU 517.
  - 0 of 161 frames lost; 180-byte frames arrived whole.
  - Round trip median 79 ms. Advertising again 0.05 s after a disconnect.
  - At −84 to −90 dBm (another room) the PC saw the car computer but could not connect.
- **The real code on the car computer**, from a scratch folder, checked from the PC with the real decoder:
  - 82 frames in 35 s, none refused for its signature or as a replay, every match key right.
  - Renamed and switched off from the settings while running.
  - The detector's drop counter stayed at its start-up value.
- **Car computer.**
  - `p0/bluetooth_frames.py` (signed frames), `bluetooth_config.py` (the switch, the name and the key), `bluetooth_bluez.py` (BlueZ over D-Bus) and `bluetooth_link.py` (the link's own process).
  - The publisher runs that process while Bluetooth is on, feeds it through a pipe that never blocks, and starts it again ever more slowly if it ends. No new packages: `python3-dbus` and `python3-gi` come with Raspberry Pi OS and Debian 13, and BlueZ lets the normal user register.
  - BlueZ takes a new adapter name a moment after the call returns; `adapter --name` now waits for it (found on the car computer).
- **Pushes on their own thread** (`Pusher`). A server that doesn't answer could take one push several seconds (2.5 s for each network the publisher tries), and the publisher read no new matches meanwhile. Harmless before, but it would have held up Bluetooth in exactly the outages it is for. Now the main loop keeps reading and feeding Bluetooth; a push that comes due meanwhile goes out the moment the stuck one is back; power off and stop still wait for theirs. Found when asked whether the retries slow anything down.
- **Setup.** `setup.sh car` asks "Send alerts over Bluetooth too?", yes by default, and for the name, `Slot17` by default. `--bluetooth`, `--no-bluetooth`, `--bluetooth-name`; `bluetooth=` and `bluetooth_name=` in the image's `slot17.txt`. Skipped without an adapter or a display server. The doctor has a Bluetooth line.
- **The app's switches**, through the server like the Pushover switch: Bluetooth on the car computer, Rename, and the pairing request that gives a linked app the key. The key never travels over Bluetooth or in a state; a state names it by `key_id`.
- **Off and Rename over Bluetooth too**, with the phone connected: a signed control frame written to the car computer, which answers with a reply frame. It names a frame the car computer sent in its last 300, and each works once, so a recorded command cannot be played back. Switching on still goes through the server, because a link that is off hears nothing.
- **One match, one sound.** Both apps remember matches by an 8-byte key, the start of SHA-256 of the match id, which the car computer also sends. A match that comes both ways plays once, and an alert first heard over Bluetooth takes the server's id when it arrives. A match over Bluetooth carries the same fields as through the server.
- **iPhone (iOS 18):** AccessorySetupKit and Core Bluetooth with state restoration; from iOS 26 that is what lets iOS relaunch the app for Bluetooth (Apple TN3115). **Android:** companion device pairing, `autoConnect` GATT, the `connectedDevice` service type.
- **Left out, for now:** Bluetooth without a server for the first set-up, other controls over Bluetooth (stop, power off, sightings), Android starting the app when the car computer appears.
- **A support package**, `bash setup.sh logs` (`p0/support.py`, [SUPPORT.md](operations/SUPPORT.md)): one zip for someone who helps, starting with a plain `REPORT.txt` (the setup check, then per drive its corrected start, length, matches, faults, samples dropped after the start, interference). Three choices:
  - **Quick check:** under 1 MB.
  - **One drive:** without the per-candidate lines, optionally only minutes A to B after its start.
  - **Everything from a drive:** recordings on request.

  Keys, tokens and passwords never go in. It runs at the lowest priority in small steps, and records the dropped samples before and after. The app card and sending it to the team come in the next release.
- **Tests:**
  - Detector: 215 (40 new, `tests/test_bluetooth.py` and `tests/test_support.py`).
  - Server and publisher: 22 new.
  - Setup: 4 new in `scripts/test_doctor.py`, 2 in `scripts/test_bluetooth_parity.py`.
  - Android core: 11 new, run here with the real Kotlin compiler.
  - iPhone: 11 new, run in the cloud build.

## Unreleased: where to buy, and the car charger advice (19 November 2026)

Documents and website only; the software is unchanged.

- **Where to buy** (`slot17.cloud/parts/`, in all 8 languages, in the menu and the footer): a page of the site itself, in its own look. It shows **one country**: the one this browser's time zone or language points to (worked out in the browser; nothing is sent anywhere for it), or the one chosen, which is kept and goes in the address (`#de`). Per part (Raspberry Pi 5, Airspy Mini, antenna, car charger, case, SSD, optional 4G stick) the shops for that country with **price**, stock, delivery time, where they send from, and which are an approved Raspberry Pi reseller, an official Airspy dealer or what we use. The cheapest priced offer from the country itself is ticked first; tick another and the **total** follows. Parts without a price there, and prices in another currency, are named and left out of the total. Outside the EU (United Kingdom, Norway) it warns about import VAT and customs. Prices and links are a snapshot of 19 November 2026 (`website/src/parts/data.ts`, from a web check of each product page; we could not open the shops' pages ourselves). No affiliate links. The first version of that evening, a separate `parts.html` in plain HTML listing every country at once without prices, forwards to the new address.
- **Car charger advice corrected.** The documents said a car charger must give 5 V at 5 A. Our own car runs on a UGREEN Nexode 60 W (EC602), which gives 3 A at 5 V, and showed no undervoltage once during the drive of 19 November (`throttled=0x0` in all 13 uploads). The README, BUILD.md, the FAQ and the website now name that charger, explain that bigger car chargers don't give the Pi more (their extra watts are at 9 to 20 V), and give the fix for a setup that does draw more: a 12 V to 5 V 5 A converter with `PSU_MAX_CURRENT=5000`. The README's troubleshooting row now says how to read `vcgencmd get_throttled`, the temperature and the Pi 5's input voltage.

## 1.3.1, iPhone app 1.6.2 and Android app 1.8.0 (19 November 2026)

The detector's recognition is unchanged. This release is about getting started without knowing Linux.

- **A ready-made image for the Raspberry Pi** (`slot17-VERSION-raspios-lite-arm64.img.xz` on each release, built by `.github/workflows/image.yml`): Raspberry Pi OS Lite (64-bit, checked against its published SHA-256) with Slot17's packages and this release on it, SSH on. On the first start `slot17-firstboot.service` waits for the user Raspberry Pi Imager creates, reads `slot17.txt` from the boot partition (server address, car computer key, hotspot; Windows line endings and the like are fine), runs the normal installer with the release already on the card, then removes the key and the hotspot password from the card and stops running. Everything it prints also goes to `slot17-setup.log` on the card. Before the image is packed, Slot17's own offline checks run inside it under ARM emulation.
- **The phone's hotspot in the setup.** `setup.sh car` asks for it and saves it as a NetworkManager Wi-Fi network (`deploy/hotspot.sh`): the password comes on standard input, never on a command line, into a file only root can read. `--hotspot NAME`, with the password from `SLOT17_HOTSPOT_PASSWORD`, for scripted installs. 4 tests.
- **Updates by themselves for everyone.** Each release now carries a car computer update signed with the Slot17 team's key (`audit/publish_release.py --public`, on the team's computer; without the field recordings). Servers set up from now on copy it from GitHub into their `release/` folder (`relay_update`: only that release's own assets, size and SHA-256 checked, swapped in whole; the server holds no key), hosted servers get it directly, and `setup.sh car` offers to trust the team's public key (`deploy/slot17-release-key.pub`, on by default; `--no-updates`). The car computer installs only what that key signed, after testing it. The setup now installs `python3-cryptography`, which checking the signature needs. 5 server tests.
- **One line for the car computer from Discord.** `/server create` shows the whole car computer setup as one line to paste, with the address and key filled in.
- **The setup check in the apps.** The Pi screen starts with **Setup check**: every link from the phone to the receiver (private link, server, car computer, detector, receiver, sound, and on Android notifications and running in the background), with what to do about each problem and the first one to fix on top. Below it the car computer's own `setup.sh doctor` verdicts: the publisher runs `doctor --json` 90 s after it starts, every 15 minutes and when the app asks (**Check the car computer again**, a new operation `doctor`); they go out with a push only when new and every 5 minutes, and the server keeps the last ones between pushes. 7 server and publisher tests, 1 doctor test, 5 Android and 5 iPhone tests.
- **Android updates itself.** Settings, About: **Download and install** fetches `Slot17.apk` from the release and hands it to Android's installer, which asks to confirm; Android accepts it only if it is signed with the same key as the installed app. The update notice now compares with the Android app's own version (`android` in `versions.json`, which the server passes on); before, it compared with the iPhone app's.
- **Docs:** [BUILD.md](BUILD.md), from the parts to the first drive, with spots for photos; the README's step 1 starts with the image; the FAQ says so.

## Android app 1.8.0 (19 November 2026)

- **Urgent beeps on the Android Auto screen.** Android Auto has no place for a status like CarPlay's Live Activity: an app with its own car screen (Android for Cars App Library) must come from Google Play, and Android Auto's "unknown sources" option does not cover such apps (developer.android.com/training/cars/testing). It does cover messaging notifications, so an urgent beep is now a message from "Slot17" (MessagingStyle, `developer.android.com/training/cars/messaging`) that Android Auto shows and reads out: "Strong burst on L154, 55 dB". At most one every 30 s, and only while Android Auto is connected (the state `androidx.car.app`'s CarConnection reads), so the phone itself never gets them. Chimes stay sound only.
- **Mark what you saw by voice.** Android Auto requires a reply action on messages; a reply here is a sighting: police, ambulance, fire, nothing or other, in English, Dutch or German, whole words only. The app answers "Marked: Police", or that it didn't understand. The reply's words are not logged.
- On by default; **Settings > Android Auto** switches it off. Needs "Unknown sources" in Android Auto's developer settings, since the app isn't from Google Play.

## 1.3.0 and Android app 1.7.0 (19 November 2026)

The detector, the server and the iPhone app are unchanged; only the version number of the car computer software moves, so it matches the release.

- **Android app 1.7.0.** The functions of the iPhone app, built the Android way (Kotlin, Jetpack Compose, Material 3): the live meters and sounds, the Pi screen with its controls, sightings, logs, settings, update and fault notices. With the screen off a foreground service keeps the link and plays the sounds, with an ongoing notification (Live Updates on Android 16); a home screen widget and two Quick Settings tiles take the place of the Lock Screen widgets and Control Center controls. The rules that decide sounds, link timing and status words are a port of `ios/Slot17/Core` without Android in them (`android/core`), tested against the same cases as the iPhone app (`shared/parity`: 20 alert scenarios and a server message). Signed with our own key. Built and tested on Codemagic; every screen is drawn as a picture in the build. *Not yet done:* a drive with a real Android phone, and running `shared/parity` in the iPhone tests too.
- **Hosted display servers.** `/server create` in the Discord makes a server for you on `name.slot17.cloud`: its own system user, folder (closed to others), keys and port on 127.0.0.1, behind Cloudflare with HTTPS; `/server info` shows whether it runs, when the car computer last sent something and what is stored; `/server new-keys` and `/server delete` do what they say, and a deleted address is never reused. Running your own server stays as it was.
- **The car computer's setup accepts a hosted address**: `name.slot17.cloud` becomes `https://name.slot17.cloud` on the normal port, where an address of your own still gets `http://` and port 5732.
- *Known limit:* the update notice compares the app with the iPhone app's version in `versions.json`, so the Android app does not yet hear about a newer Android app.

## App 1.6.1 (18 November 2026)

- **Update notices.** The display server asks GitHub every 6 hours for the newest release (`releases/latest`, then `versions.json` at that tag, which states the software and app versions; a test keeps it equal to `VERSION` and the app's version). It opens only GitHub's own https addresses, sends nothing about the server or car, keeps the last good answer when a check fails, and can be turned off (`"release_check": false`) or pointed at a fork (`"release_repo"`). The app shows a newer app in Settings > About with a link to the release, newer car computer software on the Pi screen, and one notification per new version where fault notifications are already allowed (it never asks for permission just for this). The web page says it in its footer. While the repository is private GitHub answers 404, and nothing is shown. 6 server tests, 4 app tests.
- **Buttons answer again.** On the operator's iPhone the buttons of app 1.6.0 did nothing when tapped (first seen on *See how it looks*). 1.6.0 put Liquid Glass on each button as interactive glass; that glass either took the touch itself or, replacing the solid background, left only the icon and words tappable. Our simulator test tapped the exact centre of each button, where the words are, and passed. The glass is now only a picture behind the button that never takes a touch, and the whole rounded shape is the tap area. A new UI test taps buttons near their edge, as a finger lands: I SAW, mute, and two Settings buttons that open sheets. Which of the two causes it was has not been told apart; the change removes both.

## 1.2.9 and app 1.6.0 (18 November 2026)

Detector tests: 175 (4 new, `tests/test_notify_switch.py`). Server and page tests: 173 (9 new); the iPhone app's own tests gain 7, run in the cloud. Setup tests: 10 new (`scripts/test_doctor.py`). The detector's recognition is exactly that of 1.2.7.

- **Pushover can be switched off and on from the app.** The car computer keeps the setting in `~/.config/slot17-notify.json`; anything but `{"pushover": false}` means on, so a missing or damaged file never silences it. Off stops every Pushover message, from the detector and from the display publisher. The switch is a new operation, `notify`, which the server hands only to a car computer that lists it among its features, so an older one is never sent something it cannot do; a switch made while the car is off waits on the server up to a day. The tests point the switch at an empty temporary folder, so a real "off" on the car computer cannot make them fail there.
- **The app can show the faults itself** (Settings, Fault messages): detector degraded with its reason, receiver hears nothing, detector stopped (not when stopped from the app), and once when all is well again. One notification per fault per episode and at most one a minute, as Pushover does. *Limit:* only while iOS keeps the app running, as it does with sound armed. A closed app sends nothing, which Pushover does not suffer from, so the screen says to switch the app's notifications on before switching Pushover off.
- **Each match says which rule recognised it** (standard, weak or coherent), on the app's Logs screen, and the Pi screen shows the car computer's software version (`p0/version.py`, kept equal to `VERSION` by a test). A rule says how the training word was found, not how close or who.
- **`bash setup.sh doctor`**: one plain verdict per part (receiver on USB, service, detector heartbeat, network, clock, display server, power, storage, calibration, Pushover, updates), the first problem usually being the cause of the rest. It only looks: it never opens the receiver or touches a service, and it reads the heartbeat twice rather than trusting a clock this computer does not have. `setup.sh doctor server` checks the server.
- **The server setup prints the private link as a QR code** (`qrencode`).
- **The web page installs as a home-screen app**: a manifest and icons, so it opens full screen with its own icon. A home-screen app on iPhone keeps its own cookies, so it started on "open the private link"; that page now takes the pasted link. *Not done:* an offline page shell and web push, because browsers allow service workers only over HTTPS and this server is plain HTTP by choice.
- **App 1.6.0:** Liquid Glass on buttons when built with the iOS 26 SDK and running on iOS 26 (the meters, cards and tab bar stay solid, for reading at a glance in a car; older iOS and older Xcode get the previous look); Control Center controls and Lock Screen widgets for I SAW; VoiceOver reads the meters in words; the screens are split into one file each (a pure move).
- **For people and AI assistants:** `AGENTS.md` (the hard rules and commands), `docs/SETUP-WITH-AI.md` (a prompt that walks someone through the README step by step), and issue templates that ask for the doctor's output.
- **Corrections:** `ios/README.md` said Signulous was untried and Sideloadly in use; it is the other way round since app 1.5.1. The README and SECURITY said the phone can send two commands; it can send five, none of which can start the receiver.
- **Checked and left as it was:** the Live Activity already sends state changes at once and other changes at most every 10 s, so no update-budget change was needed. `AppModel.swift` was not split: its parts share about 60 private properties, which a split would expose to the whole app.

## 1.2.8 (18 November 2026)

Detector tests: 171 (4 new, `tests/test_preflight_start.py`). App and server unchanged. The detector's recognition is exactly that of 1.2.7.

- **The detector is ready about 30 seconds sooner after power-up.** Every start ran the whole test suite before the receiver opened: 32 to 34 s on the car computer, ready 44 s after power, and 76 s after an update, when the suite ran twice. The checks re-tested files that had already passed. `preflight.py --tests-if-changed` now runs the full suite only when a SHA-256 fingerprint of everything the tests depend on (all of `p0/` and `tests/`, the Python, NumPy and SciPy versions and the machine) differs from the last full pass, and in any case every 20th start (counted in starts: the car computer has no clock to trust at boot). Otherwise it runs 8 representative tests in about 3 s, among them the exact replay of saved field decisions; the configuration checks still run every time. A tested update copy records its fingerprint, so the first start after an update is quick too. `--tests` still always runs the full suite.
- **To use it, the service file changes** (`deploy/tetra-detector.service`: `--tests-if-changed` instead of `--tests`). Signed updates never replace service files, so an existing car computer keeps running the full suite at every start until the new service file is installed, which must happen only after 1.2.8 is installed (the old `preflight.py` does not know the option). Running the installer again does both.
- **Tried and left out (not released):** a second way in for control bursts at a coherent score of 0.82 when their three tail symbols fit (ETSI Table 19). On field data it added 11 matches (about 1 %) with no false one, but the final validation accepted 1 of 12,000 generated TETRA-shaped bursts without the word: such bursts keep real tails, so only the lower limit protected. Not worth it.

## 1.2.7 (18 November 2026)

Detector tests: 167 (7 new, `tests/test_coherent.py`). App and server unchanged.

- **A third recognition rule, `coherent`, finds weaker bursts.** The standard and weak rules test the training word on differential symbols (each symbol times the previous one), which multiplies two noisy samples and costs about 2 dB at low SNR. The new rule correlates the matched-filter symbols with the word's own symbols over a grid of carrier offsets (±1.5 kHz in 150 Hz steps), as the sync search of the Osmocom GMR/TETRA receivers does. It only adds: a burst the existing rules accept keeps their decision and rule. Normal bursts must still fit their tail symbols.
  - *Measured:* generated control bursts at 6 dB recognised 112 of 150 (36 before), at 7 dB 144 (95); normal bursts at 6 dB 50 (35), at 7 dB 128 (100). On 142,219 field snippets of three drives: 1,061 matches against 910, the existing rules' 910 unchanged.
  - *Limits chosen on two drives, checked on the third:* 0.899 for the control-burst word, 0.88 for the normal-burst words: the highest control that could otherwise pass was 0.869 and 0.753, plus 0.03, and at least the standard rule's limits. The held-out drive: 36 more matches, 0 false.
  - *False matches:* 0 in 338,438 examinations: 284,438 field controls (all snippets time-reversed, and conjugated control bursts) and 54,000 generated signals of seven kinds (TETRA modulation without the word, noise, tones, plain QPSK, DMR-like 4FSK, 2FSK, LTE-like noise bursts). The 151 added field matches look like real radios as often as the weak-rule matches do: frequency within 300 Hz of the drive's typical value 89 % (weak rules 96 %, chance 20 %), right length 95 % (98 %), another match on the same channel within two minutes 91 % (91 %). Details: [FALSE-MATCHES.md](design/FALSE-MATCHES.md).
  - *Found on the way:* normal training sequence 2 is the conjugate of sequence 1, so a conjugated normal burst is a real normal burst. The control method of 12 November (reversed and conjugated snippets) tested the normal-burst tail check, not its word, with conjugated controls; the code was never affected, because the tail check refused them all.
  - *Tried and left out:* testing both kinds of word when the envelope length is ambiguous (7 to 11 ms). At 6 dB 57 of 150 weak normal bursts measure under 9 ms, but none of them is recognised either way: their tail symbols are lost too.
- A candidate check takes 2.5 ms on a PC, against 10.5 ms in 1.2.5.

## 1.2.6 (18 November 2026)

Detector tests: 160 (8 new, `tests/test_reach.py`). App and server unchanged. From a signal and software audit of 17 November (in the private archive, `audit/signal-audit-20261117/`).

- **Cutting a channel out of the band is twice as fast.** A candidate check took 10.5 ms on a PC, and 6.2 ms of that was `examine.tune`: an oscillator evaluated at every one of 6 million samples a second, then a 1,601-tap filter designed afresh on each call. The recogniser itself took 0.3 ms. `tune` now shifts the frequency through the taps of a 31-tap first stage that also divides the rate by 8, and runs the same final filter as before at 750 kHz. Same samples to within -43 dB; identical decisions on 785 real checks and on 240 bursts injected into real car recordings (204 recognised by both, 0 differences); 2.2 times faster per check. Other sample rates keep the old path (`tune_direct`). More of each window's time is left for candidates that today go unchecked (11 to 36 % on recent drives).
- **The recogniser tries 8 symbol timings instead of 4.** With a quarter-symbol grid, even a perfect generated burst came out with a vector error of 0.14; a 12 dB burst lost 1 dB at the median and 4.6 dB in the worst tenth. On generated bursts at 7 dB, 46 and 52 of 80 are now recognised (control and normal bursts), against 35 and 45. On 142,219 saved field snippets from three drives: 859 matches against 844, none lost, and **0 false matches in 284,438 reversed or conjugated control examinations with either grid**. The rules and limits are unchanged. The recogniser's own time per burst roughly doubles (0.9 to 1.7 ms on a PC), paid for by the item above.
- **All 200 channels are examined.** Channels 0-2 and 196-199 (380.0125-380.0625 and 384.9125-384.9875 MHz) fell outside the 4.8 MHz used. Measured on our receiver, its noise floor stays within about 2 dB to ±2.45 MHz and falls 1 to 2.7 dB by ±2.5 MHz (a review gives the Airspy Mini 5 MHz alias-free; the maker's page states no figure), and in the car those channels sat 0.2 to 3.4 dB below the middle, like the outer channels already used. Their candidates and matches carry `band_edge: true`, because a strong signal just outside 380-385 MHz could fold onto them; no real TETRA match there has been seen yet.
- **Corrections:** `examine.py` gave a control burst 111 symbols (the standard's Table 7 says 103; 1,128 field bursts measured 103.9); TETRA-SIGNALS.md said no normal burst had ever been recognised (281 of 1,409 recent matches are) and gave 615 masts (a suppliers' note to parliament, March 2025: 620).

## 1.2.5 (17 November 2026)

Detector tests: 152 (9 new, `tests/test_carrier.py`). Server: the drive report names start-up losses. App unchanged; its position log is now taken every 2 s instead of every 10 s, a server setting (`track_every_s`).

- **Samples lost in the stream's first minute no longer mark the drive degraded or cut checking to one thread.** On 17 November two power-ups showed "degraded" and were restarted: each had lost 0.1 s once or a few times, 0.3 to 19 s after the stream opened, while the 4G stick on the receiver's USB controller was starting; the restart that followed lost 0.1 s too, 16 s in. Every loss since the stick arrived (14 November) came in the first 25 s but one (105 s). The degraded mark lasted the whole session, and the five minutes on one thread left 36 % of candidates unchecked against 11 % on the next drive. Such losses are now counted as `startup_dropped` (heartbeat) and `startup_dropped_samples` (summary), shown by `status.py` and the drive report, and are no fault. A loss after the first minute is a fault exactly as before. `SLOT17_STARTUP_GRACE_S` sets the time (default 60, at most 600, 0 for the old behaviour).
- *What it does not do:* stop the loss. Plugging the stick into the other USB controller should; see [MOBILE-MODEM.md](operations/MOBILE-MODEM.md). A restart does not help and reopens the receiver.
- **Each match records its carrier offset measured over the whole burst**, `matched_burst.carrier_hz`, with `carrier_noise_hz` (the first half's figure minus the second half's, divided by four). The older `residual_offset_hz` uses the training pattern only (11 to 15 symbols).
  - *Measured:* on generated bursts with a known offset, from 15 dB up the error is 0.3 Hz for normal and 1.2 to 1.7 Hz for control bursts, against 16 to 49 Hz for `residual_offset_hz`; at 10 to 12 dB it is 7 to 41 Hz with occasional outliers. Replaying 1,409 saved field matches of 8 to 17 November: all accepted again with the same training pattern, so no decision changes; 0.2 ms per burst on a PC.
  - *Why record it:* it is what any study of Doppler or of telling radios apart needs. Such a study on those 1,409 matches found nothing yet: our receiver's frequency drifts by 37 to 124 Hz over a drive, more than the Doppler of a 100 km/h pass (at most ±35 Hz), and once that drift and a straight drift in time are allowed for, no pass fitted a transmitter standing still better than chance. [TETRA-SIGNALS.md](design/TETRA-SIGNALS.md) §7 has the details.
- **Documents corrected:** the README said the training pattern is at the start of a burst (it is in the middle), counted 140 tests and said Signulous was untried; [TETRA-SIGNALS.md](design/TETRA-SIGNALS.md) §7 blamed Doppler's failure partly on a ±40 Hz precision that came from the estimator; [FIELD-TEST.md](operations/FIELD-TEST.md) and [APP-OVERVIEW.md](design/APP-OVERVIEW.md) still gave 0.5 s windows; [MOBILE-MODEM.md](operations/MOBILE-MODEM.md) still called the stick's interference unmeasured.

## 1.2.4 (15 November 2026)

Detector tests: 143.

- **The interference catch-up of 1.2.2 is removed, with its counters of 1.2.3.** Candidates flagged as one broadband burst of interference are again checked last and only within their window's own time, as in 1.2.1. `--impulse-catchup`, `SLOT17_IMPULSE_CATCHUP_S`, `catchup_checks`, `catchup_matches`, `catchup_seconds`, `max_backlog_s` and `found_in_catchup` no longer exist.
- *Why:* the ten interference windows saved on 15 November, replayed with every candidate checked and no time limit, recognised nothing the live budget had missed, and the 34 dB burst that prompted the change failed the check too. The catch-up's cost was certain: matches up to 2 s late after interference, which comes in 7 % of windows while driving. It never ran on a drive.
- **Kept from 1.2.2:** a channel recognised in the last minute is checked first, interference or not.
- **What to do about the interference instead:** it is an LTE transmitter close to the receiver. Move a 4G stick away from the receiver on a USB lead; see [MOBILE-MODEM.md](operations/MOBILE-MODEM.md).

## 1.2.3 (15 November 2026, superseded by 1.2.4 before the car computer installed it)

Detector tests: 149. Server: the drive report shows the catch-up.

- **Whether the interference catch-up of 1.2.2 is worth its delay is now measured.** `catchup_matches` counts candidates recognised as TETRA only because of the catch-up; such an event carries `found_in_catchup: true`. Beside it: `catchup_checks`, `catchup_seconds` (time spent past windows' own budgets) and `max_backlog_s` (the furthest analysis was behind the receiver, the delay a match could have had). In the heartbeat, `summary.json`, `status.py` and the drive report on the server.
- *Why:* 1.2.2 was made at the operator's request although the ten saved interference windows of 15 November gave no extra match when everything was checked. Its cost (up to 2 s later matches after interference) is certain and its gain is not, so it stays at 2 s as a trial and is judged on these figures after a few drives: if `catchup_matches` stays 0, lower it or turn it off (`SLOT17_IMPULSE_CATCHUP_S=0`).

## 1.2.2 (15 November 2026, release 10, superseded before the Pi installed it)

Detector tests: 148 (8 new).

- **Interference bursts are now checked too, with catch-up time.** Candidates flagged as one broadband burst of interference (about 180 channels at once) are still checked last, but no longer only in whatever is left of the window's 0.17 s. They may go on being checked while the analysis stays at most 2 s behind the receiver; the receiver's 15 s buffer holds the rest, and ordinary windows (about 0.06 s of work in each 0.25 s) catch up. *Why:* on the drive of 15 November 1042 of 4745 candidates were never checked, among them a 34 dB burst on L119 one second after an L119 match.
- **The cost:** after an interference burst, the next windows, and so any match in them, are up to 2 s late until analysis has caught up. Candidates that are not flagged are never held back by the catch-up inside their own window.
- **A channel recognised in the last minute is checked first**, interference or not.
- Settable with `--impulse-catchup S` or `SLOT17_IMPULSE_CATCHUP_S`; 0 is the behaviour of 1.2.1. Off after dropped samples, like the extra checking threads. The heartbeat and summary count `catchup_checks`.
- **What it found when tried:** the ten interference windows saved on 15 November, replayed on the PC with everything checked, gave no recognition that the live budget had missed. The 34 dB L119 burst failed too (correlation 0.49, 8 symbol errors). On the PC, checking all of the worst window took 1.1 s; on the Pi 5 it is expected to take about twice that, so at 2 s most but not quite all of the worst windows are covered.

## App 1.5.1 (15 November 2026, not yet in a release)

- **The sighting widget shows that a tap was taken**: the tapped button carries a tick for a minute, and a line under the buttons says what you marked and counts up from it for half an hour ("✓ Police 0:42"). The widget redraws at once after a tap and by itself when the tick and the line expire, so it uses no background budget.
- This needs an **App Group** (`group.nl.slot17.app`), shared storage for the app and its widget. The build now gives the app and the widget an ad-hoc signature that carries it, so a re-signing service such as Signulous or Sideloadly can see which group to keep. A service that renames the group to its own team is handled: the app also reads the group granted in the installed provisioning profile.
- If the signing keeps no App Group, the widget stays plain buttons and nothing else changes. The app's log says which it is at every start: "widget sharing on (App Group …)" or "widget sharing off …".

## App 1.5.0 (15 November 2026, not yet in a release)

The car computer and server are unchanged; this is the iPhone app only.

### The CarPlay and Lock Screen tile

- **A call pattern and rising strength are on the tile**: a phone-and-waveform icon (*In a call* on the Lock Screen) and an arrow (*Rising*) beside the strength.
- **"+2" after the channel** when other channels were heard in the last minute, so the tile tells you that more is going on than the one channel it shows.
- **While nothing is heard, the tile says which channel it heard last and counts up from then** ("Last L60 2:13"), for up to 30 minutes, instead of only "Listening". The clock counts on the phone by itself, so this costs no updates.
- **"Car data late"** in amber when the car's newest data is more than 5 s old but not yet lost (the car pushes at least every 2 s). It clears below 3.5 s, so it does not flicker. It shows that what the tile shows may be behind, before the tile changes to *No link*.
- **Sent sooner**: a step up in colour (for example from silent to chime level), a call starting and data turning late now go to the tile at once, instead of waiting up to `tile_gap_s` (3 s). Stepping down still waits. *Why this is safe:* in 3,481 logged drive minutes iOS took on every update the app sent except in 12 minutes, at up to 12 a minute, so sending a few more urgent ones is not expected to be throttled.
- **A widget tap answers with context**: "You marked: Police · L60 12 s before" names the channel heard nearest the tap (within 2 minutes). The text stays fixed once shown, even if a later burst arrives.

### The app's own screen

- The channels beyond the three meters are now chips under them (channel, dB, age, call or rising, fading with age), instead of "+N more channels".

## 1.2.1 (15 November 2026)

Detector tests: 140 (unchanged; the detector itself is the same as 1.2.0). App 1.4.9.

### Connection details in the app

- **The Pi screen's Connection card now shows what the reports travel over**, under the chain of lights: the phone's own round trip to the server in ms; the network the car computer uses (4G stick, Wi-Fi hotspot or cable, with its device name) and which others it can fall back to; its uploads over the last minute (median and slowest ms, sent and failed, how many went over another network); the Wi-Fi signal in dBm when the hotspot is used; and whether its clock has been set from the network.
- **For the 4G stick**: the network type and provider, signal (RSRP in dBm with a plain word, and bars), quality (RSRQ and SNR), the stick's own ping to the server (average, worst, lost), whether data roaming is on, the cell, and how long it has been without a data session when it is down.
- The stick check (`deploy/modem_link.py`) keeps its latest reading in `/run/slot17-modem.json`, now readable by the publisher; the publisher reads it without running programs and leaves out a reading older than two minutes. The server rebuilds the block from known fields only and drops a malformed one without refusing the push.
- *Signal words* are the usual LTE ranges (RSRP from -90 dBm good, -105 fair, -115 weak), not a measurement of this car.
- A Pi with release 8 or older sends no such block; the card then says the details come with release 9.

## 1.2.0 (15 November 2026)

Detector tests: 140. App 1.4.8.

### Faster, and no burst cut in two

- **A new analysis window every 0.25 s instead of every 0.5 s**, each also looking 64 ms back, longer than the longest burst the detector keeps (58.7 ms). A burst belongs to the one window it starts in, so every burst is seen whole and counted once. With 0.5 s windows about 3 % of normal bursts were cut at a window edge.
- **A recognised match is written the moment it is recognised**, instead of after every other candidate in its window has been checked, which in a busy window could take most of the window's checking time.
- **The car computer's publisher checks for new matches every 0.05 s** instead of 0.1 s.
- *Expected gain:* about 0.1 to 0.35 s from burst to phone; most in busy windows. Not yet measured on a drive.
- *Checked on* the 46 six-second full-band recordings on the car computer (276 s of real drives), through each version's own pipeline with its live time limit: the same radios found (64 radio-half-seconds each, 63 in common, one only in each), no burst counted twice. On the ten busiest, the average window took 0.104 s of its 0.25 s, against 0.204 s of 0.5 s before: the same share of the computer. 90 % of windows took under 0.244 s, the slowest 0.25 s.
- *Cost:* more candidates are left unchecked in whole-band interference (950 against 687 on those recordings), because each window's fixed work now comes twice as often. As before, these were interference, not missed radios.
- A generated recording with a burst on every window edge is a test now: each burst found once at its time, and the old windows shown to lose the one on their edge.

### A radio in a call, and rising strength

- **Call pattern** (`call_like` and `call_bursts` on each event, `call_matches` in the heartbeat and summary): a recognised match on a channel with three or more normal-length bursts (11–16 ms) one to three frames apart. That is what a radio in a call sends; a radio signing on sends single short control bursts. On the November drives 224 of about 250 recognised normal bursts sat in such a run and almost none of 943 control bursts did. It is traffic on the channel, not identity or distance.
- **A soft call tone** in the app (1.4.8) and on the web page: two low sine notes at under half the chime's level, for a call-pattern match from 18 dB up to the chime level, at most once a minute per channel and once per 15 s. It never holds back a chime or an urgent beep. Server settings `call_db` (99 switches it off), `call_gap_channel_s`, `call_gap_any_s`.
- **Rising**: a channel whose newest match is at least 6 dB above the weakest of the last minute and the strongest so far, with three matches or more. Shown on the meters as *Rising*; no sound. Louder is not nearer.
- The meters say *In a call* for 10 s after a call-pattern match.

### Drive reports

- **The server writes a report for each drive** when the app's drive data arrives (`display/drive_report.py`): matches, sounds, burst-to-phone delay, sightings with what was heard around them, speed, and from the car computer's logs the session's counters, noise floor, link and 4G figures. The app lists it under Logs as `report-…`. The report picks the car computer session that overlaps the drive, so a session started at home afterwards is not mistaken for it.

## 1.1.3 (14 November 2026)

Detector tests: 136. App unchanged (1.4.7). The first release that contains `bash setup.sh modem`.

### After the first drives with the 4G stick

- **Dropped samples no longer cost the whole session its checking threads.** Until now any dropped sample put candidate checking on one thread until the detector stopped. Now it is one thread for five minutes after the last loss, then the chosen number again, once; samples lost after that return keep one thread for the rest of the session, because the threads may then be the cause.
  - *Why:* with the stick on the receiver's own USB controller, 4, 14 and 18 buffers were dropped in the first three minutes of three sessions and none afterwards. One of those drives then spent its remaining 50 minutes on one thread, and 57% of its candidates were never reached (22 to 23% on three threads).
  - *Not yet shown:* that the threads coming back causes no new loss on a drive. If it does, the rule above ends them for that session. The 13 November replay still stands: unreached candidates have not yet been shown to hide a recognisable burst.
- **The stick check says what the stick reports about its reception** (`deploy/modem_link.py`): network type, signal bars and RSRP when a connection is made and when it is not. On the first drive the stick had no mobile connection for eight minutes while the car stood still, and the log could not say whether it had coverage. Run `bash setup.sh modem` again to get the new check.
- **What that drive showed** is in [MOBILE-MODEM.md](operations/MOBILE-MODEM.md): internet 93 seconds after power-up, four single uploads timed out at motorway speed and recovered, one eight-minute loss that ended only when the stick was replugged by hand, and the advice that follows: the other pair of USB sockets, and the hotspot left on at first.
- **The stick recovers without hands, and data roaming stays on.** The check now redials after 2 minutes without a session and asks the stick to restart itself after 5 (once per 15 minutes), which does what the replug did. It switches data roaming on whenever it finds it off: with roaming off, our Lycamobile SIM's home network counts as "Internal" roaming, the likeliest reason it would not connect. `SLOT17_MODEM_ROAMING=leave` turns that off for other SIMs.
- **A stalled push goes out over the other network at once.** A push that fails is now tried again straight away on a new connection, then over every other network with a default route, each pinned to its interface so the kernel cannot send it back over the one that stalled. With the hotspot on, a stall on the stick costs a fraction of a second. After a failure the next push comes within 0.5 s, not 2 s. Without a second network, a stretch with no mobile coverage still cannot be bridged; nothing is lost, as matches wait and are sent with their age, but a match more than 10 s old when it reaches the phone does not beep.
- **Much more about the link in the logs:** every five minutes (and at once on any change) the stick's network, band, cell, RSRP, RSRQ, SNR and the round trip to the server in milliseconds; every minute the publisher's pushes, failures and milliseconds and the network used; when the clock gets its network time. The Pi log the server collects now carries these lines, the network addresses and the routes.

### Corrections

- **The quiet-receiver warning stays at one minute whatever the window length.** The car computer's publisher and the web page each had a fallback count for a detector that does not state its own limit: 20 and 40 windows, from the days of 3 s and 1.5 s windows, which would have meant 10 and 20 seconds with 0.5 s windows. Both now take one minute of windows of the length the detector reports. The detector has always stated its limit, so nothing changes in normal use.
- **`summary.json` carries `window_s` and `confirm_workers`.** The check in [POWER-LOSS.md](operations/POWER-LOSS.md) multiplied windows by 3 s; it now reads the window length from the session.
- **`p0/stress_test.py` used 1.5 s windows** under a comment saying it must match the detector's, which has used 0.5 s since 1.1.0. It now uses 0.5 s, and a test fails if the two ever differ again. All 32 of its quick checks give the same verdicts at both lengths.
- Comments and guides that still described 3 s or 1.5 s windows, a 180 s cooldown and 36 tests are corrected; the README says which iOS versions the Live Activity and CarPlay need, and what to check on a USB-C car charger.
- `ruff` reports no findings again (three in `p0/update.py` since 1.1.0, each a deliberate exception now marked with its reason).

## Setup: a 4G stick instead of the hotspot (14 November 2026)

No change to the detector or the app, and nothing to do if you use a hotspot.

- **`bash setup.sh modem`** sets up a ZTE MF833N 4G USB stick on the car computer, so the phone's hotspot is only the fallback. The order becomes wired network, stick, Wi-Fi. See [MOBILE-MODEM.md](operations/MOBILE-MODEM.md).
  - *Why it needs a setup:* on a Raspberry Pi 5 this stick activates when asked but never confirms it, so Linux loads no driver. The setup installs a USB fix made by someone else ([zte-mf79n-pi5-fix](https://github.com/hamacherengineering/zte-mf79n-pi5-fix), pinned to the version we read and tested) and a check of ours, `deploy/modem_link.py`, that asks the stick to connect when its data session is down and ranks its route.
  - *Checked on* our Pi at home: internet over the stick with no transfer errors; a data session cut by hand came back on the next check; and after a restart the stick was up by itself in about 95 seconds.
  - *Seen once, with the stick on the receiver's own USB controller:* the receiver dropped 18 buffers (about a third of a second) in the first half minute of the session, while the stick was starting up, against none or one in the sessions before. None followed in the next eleven minutes. Any dropped sample puts candidate checking back on one thread for that session, so put the stick on the other controller; the guide says how to tell.
  - *Not yet shown:* a drive; that the other controller avoids those dropped buffers; whether a 4G transmitter this close raises the interference figure; and whether the stick reconnects by itself after losing coverage.
  - *Tried and left out:* making the Pi look like Windows to the stick, waiting before activating it, retrying 120 times, and the kernel's timing options. None changed anything.

## 1.1.2 (13 November 2026)

Detector tests: 131. App unchanged (1.4.7).

- **The number of checking threads follows the computer.** 1.1.1 always used three, which suits a four-core Raspberry Pi and nothing else. Now it is chosen from the cores available, leaving one free for the receiver: one thread on one or two cores, two on three, three on four, and four from five cores up. The detector prints its choice at start.
- **You can set it yourself** with `--check-threads N` or the environment variable `SLOT17_CHECK_THREADS`; see "Slower and faster computers" in the README. Your own number is not limited to four.
- *Not measured:* more than four threads, and computers with one or two cores. On those the detector behaves as it did before 1.1.1.

## 1.1.1 (13 November 2026)

Detector tests: 126. App unchanged (1.4.7).

- **Candidates are checked on three threads at once.** With 0.5 s windows each window has a third of the time to check its candidates, and on the first drives 23% of candidates were never reached. The numerical work releases Python's lock, so three threads examine 2.2 times as much as one on a four-core Raspberry Pi 5, with identical decisions. The order of priority is unchanged: only candidates of the same round run side by side.
  - *Checked on* the 74 full-band windows saved from those drives, replayed through the real pipeline with its live time limit: candidates never reached fell from 38% to 22%, candidates with a burst of their own left unchecked from 9 to 0, partly checked from 396 to 54, and no window took longer than real time (longest 0.44 s of 0.5 s).
  - *Safety:* if the receiver has dropped any samples in a session, checking goes back to one thread for the rest of it. `confirm_workers` in the heartbeat shows which.
  - *Not yet shown:* that reception stays free of dropped samples on a drive with three threads busy. One core is left free for the receiver for that reason.
  - *What it does not change:* with no time limit at all, those 74 windows held no recognisable burst, so this closes a gap that had not yet cost a match.

## 1.1.0 (13 November 2026)

App 1.4.7. Detector tests: 123.

### Recognition

- **Weaker bursts are recognised.** Weak bursts were mostly refused because the whole burst no longer looked enough like TETRA's modulation, while the training pattern in its middle still fitted. Three extra rules accept a lower modulation figure, two of them only with a stricter fit of the pattern. A match says which rule made it (`matched_burst.rule`: `standard` or `weak`), and the heartbeat counts them (`weak_matches`).
  - *Checked on* 380,277 saved field bursts, each also examined time-reversed and mirrored, where no training pattern can exist: 222 more matches on top of 2,594 (8.6%), and no false match in 760,554 control examinations. The limits were chosen on 243,867 of those bursts and scored on the other 136,410.
  - *Found on the way:* the standard rule accepted 1 of those 760,554 controls. It is unchanged.
  - *What it does not do:* these bursts are far below any sensible beep level, so beeps barely change. Nothing is recognised below about 6 dB by any rule.
- **A real burst is no longer left unchecked behind interference.** When a short burst of interference lights up the whole band, about 180 channels become candidates at once and the checking time runs out. Candidates that also hold a burst at a moment of their own are now checked first. `unchecked_own_burst` counts the ones still missed; it should stay near zero.
  - *Why:* on two drives, 680 of 2,002 and 362 of 4,636 unchecked candidates held such a burst.

### Delay

- **Analysis windows are 0.5 s instead of 1.5 s.** A burst waited on average 0.75 s for its window to end; now 0.25 s. Measured before: the phone decided 2.0 s after the burst (median), and a beep started 2.5 to 3.3 s after it. Measured on the first drives after: the phone decides 1.0 s after the burst (median of 112 matches, longest 1.25 s).
  - *Checked on* 458 s of real full-band recordings: every radio found with 1.5 s windows is found with 0.5 s windows. An active radio is now reported up to three times as often.
  - *Cost:* a burst lying across the end of a window is cut, and each window has a third of the time to check candidates. On the first drives the count of candidates left unchecked rose, so the 74 full-band windows saved from them were replayed with no time limit: nothing more was recognised in any of them. Overlapping windows were tried on the same recordings and found one radio more in 60, so they were left out.
- **The app starts a sound sooner.** The wait for the music to dip is 0.05 s for the urgent beep and 0.15 s for others, instead of 0.4 s.
- **The Live Activity changes sooner.** Ordinary changes wait at most 3 s instead of 10 s (`tile_gap_s` in the server's settings). Beeps and warnings were always sent at once.
- **It cannot be instant:** a match travels car computer, phone hotspot, server, phone.

### Updates and logs

- **Signed updates for the car computer** (`p0/update.py`, off until you install a public key). You sign a release on your own computer and put it on your server; the car computer fetches it, checks the signature, tests the complete new software in a copy, and installs it at its next start, before the receiver is opened. A failed release is rejected and the old software stays; a power cut during the install is finished at the next start. It never replaces your calibration, channel list or service files. See the README.
- **The car computer's log arrives by itself.** It loses power with the ignition and cannot send a log at the end of a drive, so the server asks for one two minutes into a drive and every ten minutes after.
- **The app shows the car computer's software version**, and the Live Activity says "Updated to …" for a few minutes after an update, or that one failed.

### Measuring whether the alerts are useful

- **Encounter scorecard** (`scripts/scorecard.py`): from the app's uploads, how many marked emergency vehicles had a beep before them, how many did not, and how often an ordinary moment has a beep before it. Only sightings marked independently of the beeps mean anything.
- **A double tap is one sighting**: the same button within 3 s.

### Getting more out of the antenna

- **Noise floor and interference, live in the app.** The Pi screen shows the band's quiet level against the receiver's own, and the share of windows with whole-band interference in the last minute. Set `floor_quiet_db` in the server's settings to your receiver's floor with nothing to hear.
  - *Why:* on our recordings a lower detection threshold found nothing more, while the floor in the car sat 3 to 6.5 dB above the receiver alone. Every dB of floor is a dB of reach, and the cause can only be found by moving the antenna or unplugging things and watching the figure.
- **Counters** `whole_band_windows` and `weak_matches` in the heartbeat and `summary.json`.

### The app, lighter

- The log file is written off the main thread with the file kept open. It was opened, written and closed on the main thread for every match.
- The log list is only redrawn while the Logs screen is open.
- Location updates every 30 m instead of 10 m.

### Tried and left out

| Idea | Result | Decision |
|---|---|---|
| Lower detection threshold (5, 4, 3 dB instead of 6) | At most 1 more radio in 60 on real recordings, for 22 to 83% more work | Left at 6 dB |
| Shorter minimum burst (3 ms instead of 4) | Nothing more, 25% more work | Left at 4 ms |
| Overlapping analysis windows | 1 more radio in 60 | Left out for now |
| Lower gain against overload | Signals from about 56 dB up overload the receiver and put copies on 9 to 27 other channels, but none of 220 copies was recognised as TETRA. Lower gain would cost the weak bursts | Gain unchanged |
| A second band ("H") | 390 to 395 MHz is the masts, which transmit whether or not a vehicle is near; 410 to 430 MHz is not the emergency services. The receiver sees 5 MHz at a time | Not added |
| The car computer pulling from GitHub directly | It would need a GitHub login on the car computer | Signed releases from your own server instead |
| A distance or "police probability" from signal strength | The evidence cannot support it | Not built |

### What is still not shown

- That a beep means a vehicle is near. One ambulance was marked 45 s after an urgent beep with one channel rising for two minutes before; one case proves nothing.
- The delay from the phone's decision to the sound, with the shorter wait of app 1.4.7.
- Where the raised noise floor in the car comes from. Whole-band interference grows with speed and not with acceleration, and the case fan shows no effect on the floor.

## 1.0.0 (9 November 2026)

First release: detector, display server, iPhone app, setup scripts.
