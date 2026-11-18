#!/usr/bin/env python3
"""Checks what the end-to-end run really did, from the Pi side and from the app's own log.

    check_e2e.py OUTPUT_DIR APP_LOG
"""
import json
import os
from pathlib import Path
import re
import sys

out, log_path = Path(sys.argv[1]), Path(sys.argv[2])
report = json.loads((out / "e2e-report.json").read_text())
log = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def measured(ok, what):
    """Real sound. On a Mac with no audio system the app reports a pretend result, which proves nothing."""
    if os.environ.get("NO_AUDIO") == "1":
        print("skip " + what + " (no audio system on this Mac)")
    else:
        # A debug build's stand-in reports -120 dBFS, which is never real sound.
        check(ok and not any("sound played" in l and "peak -120" in l for l in log), what)


def after(marker):
    for index, line in enumerate(log):
        if marker in line:
            return log[index:]
    return []


marks = report["marks"]
labels = [m["label"] for m in marks]
check("police" in labels, "the Police sighting reached the detector inbox")
check(any(m["label"] == "unknown" for m in marks), "Save a recording reached the detector inbox")
police = next((m for m in marks if m["label"] == "police"), {})
check("phone_utc=" in police.get("note", ""), "the sighting carries the phone's time in its note")
check(abs(police.get("entered_utc", 0) - police.get("utc", 1e12)) < 10, "the sighting's time is close to when it arrived")
check(len(labels) == len(set(m["id"] for m in marks)), "no sighting was saved twice")

check(report["poweroffs"] == 1, "power off arrived exactly once")
check(report["stops"] == 1, "stop arrived exactly once")
sudo = report["sudo"]
check(["sudo", "-n", "systemctl", "--no-block", "poweroff"] in sudo, "power off used the exact permitted command")
check(["sudo", "-n", "systemctl", "--no-block", "stop", "tetra-detector.service"] in sudo, "stop used the exact permitted command")
check(not any("reboot" in " ".join(c) or " start" in " ".join(c) for c in sudo), "nothing asked for a reboot or a start")
check(report["pi_logs"] >= 1, "the Pi log was uploaded and stored")
check(len(list((out / "server-logs").glob("app-*.log"))) >= 1, "the app log was uploaded and stored")
check(report["publisher_errors"] == [], "the publisher raised no errors")

check(any("urgent for L101" in l for l in log), "a 55 dB match played the urgent beep")
check(any("chime for L102" in l for l in log), "a 30 dB match played the chime")
check(any("silent for L103" in l and "below" in l for l in log), "a 15 dB match stayed silent, with the reason")
measured(any("sound played: urgent" in l and "peak" in l for l in log), "the urgent beep made measured sound")
measured(any("sound played: chime" in l for l in log), "the chime made measured sound")
check(any("link lost for 30 s" in l for l in log), "losing the car played the link warning")
measured(any("sound played: linkLost" in l for l in log), "the link warning made measured sound")

background = after("app moved to the background")
until_open = []
for line in background:
    if "app opened" in line:
        break
    until_open.append(line)
check(any("urgent for L101" in l for l in until_open), "beeps kept coming with the app in the background")
measured(any("sound played: urgent" in l for l in until_open), "sound was measured with the app in the background")

check(any("applied from the server" in l for l in log), "the app took its settings from the server")
check(any("sound urgent now plays test_bell.wav" in l for l in log), "the urgent sound came from a WAV file on the server")
check(any("(Quiet mode)" in l for l in log), "Quiet mode silenced a chime below its level")
nothing = [m for m in marks if m["label"] == "noise"]
check(bool(nothing), "Saw nothing reached the detector inbox")
check(any("lat=" in m.get("note", "") for m in marks), "a sighting carried the phone's location to the Pi")
drive = [p for p in (out / "server-logs").glob("app-*.log") if b"slot17-drive-data" in p.read_bytes()]
check(bool(drive), "drive data was uploaded and stored")
if drive:
    data = json.loads(drive[0].read_text())
    check(any(a.get("place") for a in data["alerts"]), "alerts in the drive data carry places")
    check(len(data["track"]) > 0, "the drive data has a location track")

check(any("live activity started" in l or "live activity taken over" in l for l in log), "the Live Activity is showing")
tile = [re.search(r"(\d+) updates in the last minute \((\d+) in the background\); (\d+) changes sent, (\d+) observed by iOS; "
                  r"(\d+) changes sent in the background, (\d+) observed while in the background", l) for l in log if "live activity:" in l]
tile = [tuple(map(int, m.groups())) for m in tile if m]
for row in tile:
    print("     Live Activity per minute: sent %d (%d in background); changes %d, observed %d; "
          "in background %d, observed %d" % row)
check(any(sent > 0 for sent, *_ in tile), "the Live Activity was updated")
check(any(confirmed > 0 for _, _, _, confirmed, _, _ in tile), "iOS observed new Live Activity content")
check(any(bg > 0 for *_, bg, _ in tile), "the Live Activity was updated with the app in the background")
check(any("newest sent R" in l and "reported by iOS R" in l for l in log), "the tile's update numbers are in the log")

armed = [i for i, l in enumerate(log) if "audio armed" in l or "audio skipped" in l]
opened = [i for i, l in enumerate(log) if "app opened" in l]
check(bool(armed) and bool(opened) and opened[0] < armed[0], "the sound started only after the app was opened")

problems = [l for l in log if "PROBLEM" in l]
# iOS sometimes pauses the app for a few seconds: real phone logs from 5 to 8 November show nine pauses
# of 5 to 36 s in 119 moves to the background. The test Mac has no sound, so the app's silent keep-alive
# isn't running there either. Allow one short pause in that mode and print it. Anything else still fails.
if os.environ.get("NO_AUDIO") == "1":
    short = [l for l in problems if (m := re.search(r"iPhone stopped this app for (\d+) s", l)) and int(m[1]) <= 15]
    if len(short) == 1:
        print("note one short pause by iOS, allowed on a Mac with no audio system")
        print("     ", short[0])
        problems = [l for l in problems if l not in short]
check(problems == [], f"the app logged no problems ({len(problems)})")
for line in problems[:20]:
    print("     ", line)

print(f"{len(failures)} failed")
sys.exit(1 if failures else 0)
