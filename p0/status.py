#!/usr/bin/env python3
"""One-shot health readout: is the detector actually alive, right now?

**Why this exists.** On 7 September 2026 the Airspy wedged at 18:54 and the
detector died. For nine minutes afterwards its state was reported as healthy -
"550 windows, 0 dropped" - by reading `run/watch.heartbeat` and quoting the
numbers in it. The numbers were real; the file was nine minutes stale. The
heartbeat carries a `t` field for exactly this reason and it was not checked.

That is the same mistake as earlier testing, one layer up: trusting a
liveness signal without asking whether it is *fresh*. A counter in a file proves
only that something wrote it once. So this tool leads with the age and refuses
to print a reassuring summary of a stale file.

    python3 status.py
    python3 status.py --json
    python3 status.py --watch --interval 3

Reads detector snapshots only; never opens or retunes the Airspy. Live polling
does not scan growing capture logs. --history explicitly scans the session once.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

# A heartbeat is written ~1/s. Anything older than this is not "the detector's
# state", it is an artefact of a detector that has stopped.
STALE_AFTER_S = 10.0


def read_heartbeat(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def detector_processes():
    try:
        out = subprocess.run(["ps", "-eo", "pid,etime,cmd"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        if ("watch.py" in line or "supervise.py" in line) and "grep" not in line \
                and "status.py" not in line:
            rows.append(line.strip())
    return rows


def airspy_on_usb():
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True,
                             timeout=5).stdout
        return any("Airspy" in l or "1d50:60a1" in l for l in out.splitlines())
    except Exception:
        return None


def newest_session():
    dirs = sorted(glob.glob(os.path.join(paths.CAPTURES, "*", "events.jsonl")),
                  key=os.path.getmtime)
    return dirs[-1] if dirs else None


def collect(hb_path, include_history=False):
    now = time.time()
    hb = read_heartbeat(hb_path)
    if not isinstance(hb, dict):
        hb = None
    def age_of(stamp):
        try:
            age = now-float(stamp)
            return age if math.isfinite(age) else None
        except (TypeError, ValueError):
            return None
    age = age_of(hb.get('t')) if hb else None
    procs = detector_processes()
    # The Airspy as always; another receiver by its own USB ID (p0/receiver.py).
    try:
        import notify
        kind = notify.receiver_kind()
    except Exception:  # noqa: BLE001 - status stays readable
        kind = "airspy"
    usb = airspy_on_usb() if kind == "airspy" else notify.receiver_present(kind)
    session = hb.get('session') if hb else None
    ev = os.path.join(session, 'events.jsonl') if session else (newest_session() if include_history else None)
    n_ev = None
    n_alert = hb.get('confirmed') if hb else None
    last_ev = None
    if ev and include_history:
        n_ev = n_alert = 0
        try:
            with open(ev, encoding="utf-8") as fh:
                for l in fh:
                    try:
                        e = json.loads(l)
                    except ValueError:
                        continue
                    n_ev += 1
                    # rf_health and other telemetry are not candidate records.
                    if e.get('event_type') == 'candidate' or ('tetra_channel' in e and 'confirmed' in e):
                        last_ev = e.get('local_time')
                        n_alert += e.get('confirmed') is True
        except Exception:  # noqa: S110 - status stays readable with a damaged event file
            pass
    du = shutil.disk_usage(paths.CAPTURES) if os.path.isdir(paths.CAPTURES) else None
    fresh = age is not None and 0 <= age <= STALE_AFTER_S
    pid_matches = hb is not None and any(p.split()[0] == str(hb.get("pid")) for p in procs)
    rf = (hb or {}).get('rf_health') or {}
    host = (hb or {}).get('host_health') or {}
    return {"now": now, "heartbeat": hb, "heartbeat_age_s": age,
            "heartbeat_stale": not fresh,
            "ready": bool(fresh and hb.get("ready") and pid_matches),
            "processes": procs, "airspy_on_usb": usb, "receiver": kind,
            "session": session or (os.path.dirname(ev) if ev else None),
            "rf_age_s": age_of(rf.get('measured_utc')), 'host_age_s': age_of(host.get('measured_utc')),
            "events": n_ev, "alerts": n_alert, "last_event": last_ev,
            "free_gb": (du.free / 1024 ** 3) if du else None}


def report(st):
    age = st["heartbeat_age_s"]
    hb = st["heartbeat"]
    print("=" * 62)
    if hb is None:
        print("  DETECTOR: no heartbeat file at all")
    elif st["heartbeat_stale"]:
        print(f"  *** DETECTOR STATUS UNKNOWN - heartbeat stale/invalid (age {number(age)}s) ***")
        print("  Figures below are historical, not evidence of current reception.")
    elif st["ready"]:
        print(f"  DETECTOR READY - completed analysis, heartbeat {age:.1f}s old")
    else:
        print(f"  DETECTOR NOT READY - heartbeat {age:.1f}s old, awaiting healthy analysis/process")
    print("=" * 62)

    if hb:
        print(f"  windows {hb.get('windows')}   hits {hb.get('hits')}   "
              f"dropped {hb.get('dropped')}"
              + (f" ({hb['startup_dropped']} at start-up, not a fault)" if hb.get("startup_dropped") else "")
              + f"   overflow {hb.get('overflow')}")
        print(f"  elapsed {hb.get('elapsed_s')}s   centre "
              f"{(hb.get('centre_hz') or 0)/1e6:.3f} MHz   pid {hb.get('pid')}")
        report_health(st)
    usb = st["airspy_on_usb"]
    kind = st.get("receiver", "airspy")
    print(("  Airspy on USB: " if kind == "airspy" else f"  {kind} on USB: ")
          + ("yes" if usb else "not enumerated"
             if usb is False else "unknown"))
    if st["processes"]:
        print("  processes:")
        for p in st["processes"]:
            print(f"    {p[:110]}")
    else:
        print("  processes: *** none - neither watch.py nor supervise.py running ***")
    if st["session"]:
        print(f"  newest session: {os.path.basename(st['session'])}")
        if st['events'] is not None:
            print(f"    {st['events']} log records, {st['alerts']} confirmed candidates, last at {st['last_event']}")
    if st["free_gb"] is not None:
        print(f"  free space: {st['free_gb']:.0f} GB")
    print()


def number(value, digits=1):
    return f'{value:.{digits}f}' if isinstance(value, (int, float)) and math.isfinite(value) else 'unknown'


def report_health(st):
    hb = st['heartbeat'] or {}
    rf = hb.get('rf_health') or {}
    host = hb.get('host_health') or {}
    print(f"  settings: gain indices {hb.get('gain_indices')} | threshold {number(hb.get('threshold_db'))} dB")
    print(f"  processing: last {number(hb.get('window_seconds'), 3)}s | max {number(hb.get('max_window_seconds'), 3)}s"
          f" | backlog {number(hb.get('analysis_backlog_s'))}s | telemetry {number(hb.get('telemetry_seconds'), 4)}s")
    if hb.get('weak_matches') is not None:
        print(f"  of the matched, {hb.get('weak_matches')} by a weak rule")
    print(f"  confirmation totals: {hb.get('confirmed')} matched | {hb.get('unchecked')} unchecked"
          f" | {hb.get('partial_confirmations')} partial | errors {hb.get('confirm_errors')}")
    if hb.get('unchecked_own_burst') is not None:
        print(f"    of the unchecked, {hb.get('unchecked_own_burst')} held a burst outside a broadband moment")
    print('    coverage is a status/summary advisory; no coverage phone reminders')
    print(f"  NUB envelopes: {hb.get('nub_training_matches', 0)} training matches before tails"
          f" | {hb.get('nub_tail_rejections', 0)} rejected by tails (not proven TETRA)")
    print(f"  matched attribution fallbacks: {hb.get('refinement_failures', 0)}")
    print(f"  faults: callback {hb.get('callback_errors')} | window {hb.get('window_errors')}"
          f" | telemetry {hb.get('telemetry_errors')} | heartbeat {hb.get('heartbeat_errors')}")
    for name in ('last_telemetry_error', 'last_heartbeat_error'):
        if hb.get(name):
            print('    '+name+': '+str(hb[name]))
    if not rf:
        print('  RF diagnostics: unavailable (await a window from the current release)')
    else:
        age = st['rf_age_s']
        label = 'STALE/HISTORICAL' if st['heartbeat_stale'] or age is None or not 0 <= age <= 10 else 'recent'
        print(f"  RF {rf.get('window_id')}: {label}, age {number(age)}s")
        sig = rf.get('input') or {}
        print(f"    input RMS {number(sig.get('rms_dbfs'))} dBFS | digital headroom {number(sig.get('headroom_db'))} dB"
              f" | near-rail samples {number(100*sig.get('fraction_near_rail', 0), 3)}%")
        print('    antenna connection/efficiency: UNKNOWN; digital headroom does not rule out analog overload')
        print(f"    channel floor median {number(rf.get('median_floor_db'))} relative dB units"
              f" | change {number(rf.get('median_floor_change_db'))} dB vs {rf.get('floor_reference_window')}")
        print(f"    elevated-floor channels {rf.get('elevated_floor_channels')} | floor/activity sampled"
              f" in {rf.get('frames_sampled')}/{rf.get('frames_total')} frames; not calibrated dBm")
        if rf.get('warnings'):
            print('    advisories: '+', '.join(rf['warnings']))
        for row in rf.get('highest_floor_channels', [])[:4]:
            print(f"    floor ch {row['channel']:3d} {row['frequency_hz']/1e6:.4f} MHz: {row['floor_db']:.1f}"
                  f" | change {row['floor_change_db']:+.1f} dB | sampled activity {100*row['activity_fraction_sampled']:.1f}%")
        recent = rf.get('last_match')
        if recent:
            age = st['now']-recent['processed_utc'] if recent.get('processed_utc') is not None else None
            print(f"    last RF match: ch {recent['channel']} {recent['frequency_hz']/1e6:.4f} MHz"
                  f" | {number(recent.get('peak_snr_db'))} dB above local floor | decision age {number(age)}s")
        else:
            print('    last RF match: none in this process')
        costs = rf.get('costliest_candidates') or []
        if costs:
            print('    largest confirmation costs: '+', '.join(f"ch {r['channel']} {r['seconds']:.3f}s" for r in costs[:4]))
    if host:
        label = 'STALE/HISTORICAL' if st['heartbeat_stale'] or st['host_age_s'] is None or not 0 <= st['host_age_s'] <= 45 else 'recent'
        print(f"  host {label}, age {number(st['host_age_s'])}s: {number(host.get('cpu_temp_c'))} C"
              f" | load {host.get('load_average')} | process RAM {number((host.get('process_rss_bytes') or 0)/2**20)} MiB")
        power = host.get('power')
        print('    power: '+(', '.join(k for k, v in power.items() if v is True) or 'no firmware flags') if power
              else '    power: unknown')
        if host.get('error'):
            print('    host probe error: '+host['error'])
    else:
        print('  host telemetry: unavailable')
    for key in ('recorder', 'notifications'):
        values = hb.get(key)
        print('  '+key+': '+(json.dumps(values, sort_keys=True) if values is not None else 'unavailable/disabled'))


def selftest():
    import tempfile
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("status.py selftest (offline)")
    d = tempfile.mkdtemp(prefix="p0-status-")
    try:
        fresh = os.path.join(d, "fresh.json")
        with open(fresh, "w") as fh:
            json.dump({"t": time.time(), "windows": 10, "hits": 1}, fh)
        st = collect(fresh)
        check("a fresh heartbeat is not stale", st["heartbeat_stale"] is False,
              f"age {st['heartbeat_age_s']:.1f}s")

        old = os.path.join(d, "old.json")
        with open(old, "w") as fh:
            json.dump({"t": time.time() - 600, "windows": 550, "hits": 40}, fh)
        st = collect(old)
        check("a 10-minute-old heartbeat IS flagged stale",
              st["heartbeat_stale"] is True,
              f"age {st['heartbeat_age_s']:.0f}s")
        check("the stale reading still carries its (frozen) numbers",
              st["heartbeat"]["windows"] == 550,
              "numbers preserved, but labelled stale")

        st = collect(os.path.join(d, "missing.json"))
        check("a missing heartbeat is stale, not a crash",
              st["heartbeat"] is None and st["heartbeat_stale"] is True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heartbeat", help="explicit path; otherwise check service/runtime/legacy locations")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument('--watch', action='store_true', help='repeat passive status until Ctrl-C; no SDR access')
    ap.add_argument('--interval', type=float, default=3, help='poll seconds, minimum 1 (default 3)')
    ap.add_argument('--history', action='store_true', help='explicit one-shot scan of session events (can be slow)')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not math.isfinite(a.interval) or a.interval < 1:
        ap.error('interval must be finite and at least 1 second')
    if a.watch and a.history:
        ap.error('--history is one-shot; omit it when watching')
    candidates = ["/run/tetra/watch.heartbeat", os.path.join(paths.ROOT, "runtime/watch.heartbeat"),
                  os.path.join(paths.ROOT, "run/watch.heartbeat")]
    try:
        while True:
            path = a.heartbeat or max((p for p in candidates if os.path.isfile(p)),
                                      key=os.path.getmtime, default=candidates[1])
            st = collect(path, include_history=a.history)
            st['heartbeat_path'] = path
            if a.json:
                print(json.dumps(st, indent=None if a.watch else 2, default=str), flush=True)
            else:
                if a.watch and sys.stdout.isatty():
                    print('\033[2J\033[H', end='')
                report(st)
                sys.stdout.flush()
            if not a.watch:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0
    return 0 if st["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
