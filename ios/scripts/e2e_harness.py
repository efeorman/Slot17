#!/usr/bin/env python3
"""A pretend car for the app's end-to-end test, built from the real server and publisher.

Only the detector and the system commands are stand-ins: this writes the same heartbeat and
events.jsonl the detector writes, acts on sighting files as marking.py does, and records stop and
power off instead of running them. Everything between that and the app is the production code.

    e2e_harness.py OUTPUT_DIR
"""
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "display"))
import display_publisher as pub  # noqa: E402
import server as srv             # noqa: E402

PORT = 5732
TOKEN = "e2e-pi-token-0123456789012345678901234567"
KEY = "e2e-viewer-key-0123456789"
BLACKOUT_S = 42

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
session = out / "fake-session"
session.mkdir(exist_ok=True)
(session / "session.json").write_text("{}")
events = session / "events.jsonl"
events.write_text("")
heartbeat = out / "fake-heartbeat.json"
report = {"sudo": [], "marks": [], "stops": 0, "poweroffs": 0, "pi_logs": 0, "blackouts": 0,
          "matches_sent": 0, "publisher_errors": []}
lock = threading.Lock()
state = SimpleNamespace(windows=0, detector_up=True, blackout_until=0.0, powered_off=False, seq=0, turn=False)


def save_report():
    with lock:
        (out / "e2e-report.json").write_text(json.dumps(report, indent=1))


def fake_run(cmd, **_):
    """Stands in for subprocess.run, so no test can ever run sudo."""
    if cmd and cmd[0] == "sudo":
        report["sudo"].append(cmd)
        save_report()
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    raise FileNotFoundError(cmd[0] if cmd else "?")


pub.subprocess = SimpleNamespace(run=fake_run)


def write_heartbeat():
    if not state.detector_up:
        heartbeat.unlink(missing_ok=True)
        return
    state.windows += 1
    heartbeat.write_text(json.dumps({
        "pid": 4242, "t": time.time(), "samples_in": state.windows * 18000000, "windows_started": state.windows,
        "windows": state.windows, "hits": state.windows * 2, "unchecked": 1, "partial_confirmations": 0,
        "dropped": 0, "ready": True, "degraded": False, "degraded_reasons": [], "session": str(session),
        "rf_health": {"window_id": f"w{state.windows}", "input": {"peak_counts": 1500.0, "antenna_suspect": False}}}))


def add_match(channel, strength, activity="intermittent"):
    state.seq += 1
    event = {"event_type": "candidate", "confirmed": True, "tetra_channel": channel, "event_id": f"e{state.seq}",
             "frequency_hz": 380.0125e6 + channel * 25e3, "peak_snr_db": strength, "strength_reliable": True,
             "burst_mono": time.monotonic(), "activity": activity, "matched_timing_refined": True}
    with open(events, "a") as fh:
        fh.write(json.dumps(event) + "\n")
    report["matches_sent"] += 1


def act_on_sightings():
    """What p0/marking.py does with each request, including the event the phone is told about."""
    inbox = session / "marks"
    if not inbox.is_dir():
        return
    for path in sorted(inbox.glob("*.request.json")):
        mark = json.loads(path.read_text())
        report["marks"].append(dict(mark, id=path.stem))
        with open(events, "a") as fh:
            fh.write(json.dumps({"event_type": "human_observation", "mark_id": path.stem, "label": mark["label"]}) + "\n")
            fh.write(json.dumps({"event_type": "mark_recording", "mark_id": path.stem, "name": path.stem + ".iq",
                                 "state": "queued"}) + "\n")
        path.replace(path.with_suffix(".processed"))
        if mark["label"] == "fire" and report["blackouts"] == 0:   # the test's signal to cut the link
            report["blackouts"] += 1
            state.blackout_until = time.monotonic() + BLACKOUT_S
    save_report()


def serve_test_settings():
    """Settings and one sound for the app to fetch, as the real server will serve them."""
    import math
    import struct
    import wave
    base = json.loads((Path(__file__).resolve().parent / "e2e-app-config.json").read_text())
    base["message"] = "Test settings from the server"
    base["rules"]["quiet_red_db"] = 35          # so Quiet mode visibly silences the 30 dB chime
    base["sounds"]["urgent"] = {"wav": "test_bell.wav"}
    base["display"] = dict(base.get("display", {}), strong_badge="KRACHTIG", frequent_text="Veel bursts")
    (out / "app-config.json").write_text(json.dumps(base))
    sounds = out / "sounds"
    sounds.mkdir(exist_ok=True)
    rate = 44100
    with wave.open(str(sounds / "test_bell.wav"), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(20000 * math.exp(-i / rate / 0.3) * math.sin(2 * math.pi * 880 * i / rate)))
                          for i in range(int(rate * 0.8)))
        fh.writeframes(frames)


def main():
    serve_test_settings()
    cfg = {"pi_token": TOKEN, "viewer_key": KEY}
    store = srv.Store()
    httpd = srv.CappedServer(("127.0.0.1", PORT), srv.make_handler(
        cfg, store, b"<html></html>", log_dir=out / "server-logs", config_path=out / "app-config.json",
        sound_dir=out / "sounds"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/api/push"
    sender = pub.Sender(url, TOKEN)
    publisher = pub.Publisher(sender, heartbeat=heartbeat)
    publish_cfg = {"url": url, "token": TOKEN}
    write_heartbeat()
    last_beat = last_matches = time.monotonic()
    restart_at = None
    print(f"pretend car ready on port {PORT}", flush=True)
    while True:
        now = time.monotonic()
        if state.powered_off or now < state.blackout_until:
            time.sleep(0.1)
            continue
        if now - last_beat >= 1:
            last_beat = now
            write_heartbeat()
            act_on_sightings()
        if state.detector_up and now - last_matches >= 8:
            last_matches = now
            # Apart, because an urgent beep correctly blocks a chime for the next 6 s.
            state.turn = not state.turn
            if state.turn:
                add_match(101, 55.0, "active")  # urgent, with dense bursts
                add_match(103, 15.0)  # silent: below red
            else:
                add_match(102, 30.0)  # chime when its limits allow
        if restart_at and now >= restart_at:
            restart_at = None
            state.detector_up = True
            write_heartbeat()
        try:
            publisher.step(now)
        except Exception as exc:      # noqa: BLE001 - recorded and checked afterwards
            report["publisher_errors"].append(repr(exc))
        command, sender.pending_command = sender.pending_command, None
        if command:
            if not pub.run_stop_command(command, publisher, runner=fake_run):
                report["stops"] += 1
                state.detector_up = False
                write_heartbeat()
                restart_at = now + 12
        if not pub.handle_ops(sender, publisher, publish_cfg):
            report["poweroffs"] += 1
            state.powered_off = True
        report["pi_logs"] = len(list((out / "server-logs").glob("pi-*.log"))) if (out / "server-logs").exists() else 0
        save_report()
        time.sleep(0.05)


if __name__ == "__main__":
    main()
