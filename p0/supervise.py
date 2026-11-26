#!/usr/bin/env python3
"""Supervise one detector; the installed car service uses --no-restart.

With --no-restart, failure stops reception and reports it without another stream
attempt, USB reset or power cycle. Historical recovery options remain explicit.

This is the process the car's Raspberry Pi runs at boot (see
deploy/tetra-detector.service). It does three things watch.py cannot do for
itself:

  * **Restart on any failure.** If watch.py crashes, exits because the stream
    stalled (exit %d), or is killed, the supervisor starts it again - with
    exponential backoff so a truly dead board is retried calmly, not in a hot
    loop.

  * **Detect a silent wedge.** watch.py writes a heartbeat every second
    (--heartbeat); its `samples_in` count must keep rising. If it freezes while
    the process is still alive - the classic firmware hang from earlier testing - the supervisor kills the child and treats it as a stall. watch.py
    also notices this itself and exits %d; the heartbeat watch is the backstop
    for the case where even that cannot run.

  * **Escalate recovery** through recover.py: relaunch -> USB reset -> power
    cycle. The stronger steps are opt-in (--usb-reset, --usb-power-cycle),
    because a plain relaunch clears a crash but only cutting VBUS clears the
    firmware hang, and cutting VBUS needs a switchable hub.

Everything after `--` is passed straight to watch.py:

    python supervise.py -- --band uplink --sensitivity highway
    python supervise.py --usb-reset --usb-power-cycle --hub-location 1-1 \
                        -- --band uplink
    python supervise.py --selftest          # offline: fake child, no hardware
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import threading
import time
import socket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import recover
import failure

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH_PY = os.path.join(HERE, "watch.py")
STALL_EXIT_CODE = 75            # must match watch.py.STALL_EXIT_CODE
__doc__ = __doc__ % (STALL_EXIT_CODE, STALL_EXIT_CODE)


# A window is several seconds of signal, so `windows` advances far more slowly
# than `samples_in`. Multiply the stream freeze limit rather than reuse it.
ANALYSIS_FREEZE_FACTOR = 3.0

# --- tell the phone when the radio dies -------------------------
# The firmware hang needs a human to pull the plug, and in a car that human is
# driving, not watching a terminal. supervise.log is the only other signal and
# nobody reads it at 100 km/h.
#
# Two rules, both from earlier scars:
#   1. ONE message per episode, not per retry. The supervisor relaunches every
#      few seconds under backoff; a push per attempt would be unusable. It fires
#      on the SECOND consecutive failure - one failure can be an ordinary crash -
#      then stays silent until the state changes, except one reminder every
#      WEDGE_REMINDER_S because a driver will miss a single push.
#   2. A notification must NEVER be able to break supervision. Every call is
#      wrapped; a missing, unconfigured or exploding notifier is a no-op. A
#      detector that stops supervising because Pushover is misconfigured is a far
#      worse failure than a silent wedge.
WEDGE_MIN_FAILURES = 2
WEDGE_REMINDER_S = 900.0


def receiver_label(kind):
    """'Airspy' for the Airspy, as these messages always said; the receiver's name for the others."""
    try:
        import receiver
        return receiver.short_name(kind)
    except Exception:                                # noqa: BLE001 - a word in a message
        return "Airspy"


class HardwareNotifier:
    """Pushes 'the radio died' and 'the radio is back', and nothing else.

    Wraps notify.Notifier (queue + daemon thread, spooled when offline), so a
    dead network costs the supervisor nothing.
    """

    def __init__(self, notifier=None, log=None,
                 min_failures=WEDGE_MIN_FAILURES, reminder_s=WEDGE_REMINDER_S):
        self._n = notifier
        self.log = log or (lambda m: None)
        self.min_failures = min_failures
        self.reminder_s = reminder_s
        self.wedged = False
        self._last_push = 0.0
        self.pushed = 0

    def _push(self, message, title, priority=0):
        """Every notification failure is contained here. Rule 2."""
        if self._n is None:
            return False
        try:
            self._n.post(message, title=title, priority=priority)
            self.pushed += 1
            self.log(f"  notify: pushed '{title}'")
            return True
        except Exception as e:                       # noqa: BLE001
            self.log(f"  notify: push failed ({e}) - supervision continues")
            return False

    def on_failure(self, consecutive, now=None):
        """One push per episode, plus a reminder. Rule 1."""
        now = time.monotonic() if now is None else now
        if consecutive < self.min_failures:
            return False                             # one failure can be a crash
        if self.wedged and (now - self._last_push) < self.reminder_s:
            return False                             # already told them
        first = not self.wedged
        self.wedged = True
        self._last_push = now
        try:
            import notify
            kind = notify.receiver_kind()
            present = notify.receiver_present(kind)
        except Exception:                            # noqa: BLE001
            kind, present = "airspy", None
        label = receiver_label(kind)
        where = {True: "It is on the USB bus but will not stream.",
                 False: "It has vanished from the USB bus entirely.",
                 None: "Its USB state could not be read."}[present]
        msg = (f"The {label} has stopped after {consecutive} consecutive "
               f"failures.\n{where}\nOnly unplugging it and plugging it back in "
               f"clears this - software cannot. The detector is not receiving "
               f"until you do.")
        return self._push(msg, f"{label} wedged" if first else
                          f"{label} still wedged", priority=1)

    def on_streaming(self, now=None):
        """Called ONLY when samples are seen actually arriving. Proof, not hope."""
        if not self.wedged:
            return False
        self.wedged = False
        self._last_push = time.monotonic() if now is None else now
        try:
            import notify
            label = receiver_label(notify.receiver_kind())
        except Exception:                            # noqa: BLE001
            label = "Airspy"
        return self._push("Samples are arriving again - the detector is "
                          "receiving. No action needed.",
                          f"{label} recovered", priority=0)

    def on_stopped(self, outcome):
        """A failure is not automatically proof of a firmware wedge."""
        detail = {'receiver unavailable': 'The receiver could not be opened. Check its USB connection when parked.',
                  'stall': 'The receiver sample stream stopped advancing. Check the logs when parked; a hardware wedge may need physical unplug/replug.',
                  'analysis failure': 'Samples could not be analysed within the watchdog deadline. Check the analysis and host-load logs when parked.'}.get(
                      outcome, 'Check the recorded runtime error when parked.')
        return self._push(f'Detector stopped ({outcome}). {detail} '
                          'Automatic reopening is disabled. Reception has stopped.',
                          'TETRA detector stopped', priority=1)

    def finish(self, grace=0):
        """Allow a failure notice a bounded delivery opportunity after RF stops."""
        if self._n is None:
            return
        end = time.monotonic()+grace
        while time.monotonic() < end:
            state = self._n.snapshot()
            if not state['unfinished'] and not state['pending']:
                break       # everything accepted has now been delivered, refused or expired
            time.sleep(.05)
        self._n.stop(timeout=2)


def _make_notifier(enabled, log):
    """A started notify.Notifier, or None. Never raises."""
    if not enabled:
        return None
    try:
        import notify
        if not notify.configured():
            log("  notify: no Pushover credentials - hardware alerts disabled")
            return None
        n = notify.Notifier(log=log, spool=notify.SPOOL_PATH + ".supervisor")
        if not n.start():
            return None
        return n
    except Exception as e:                           # noqa: BLE001
        log(f"  notify: could not start ({e}) - continuing without alerts")
        return None


def classify(rc, killed_for_stall):
    """Turn a child exit into one of clean / stall / crash. Pure - unit-tested."""
    if killed_for_stall:
        return "stall"
    return failure.outcome(rc)


def read_heartbeat(path):
    """Load the heartbeat file, or None if it is missing / mid-write."""
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def stop_child(child, term_grace=40.0):
    """Ask the child to stop, escalate to kill if it will not. Returns exit code."""
    # watch.py can spend 8 s joining analysis and 15 s each draining notification
    # and recording workers. Five seconds could kill it before its final summary.
    # The service's 45 s outer deadline still bounds a hung shutdown.
    if child.poll() is not None:
        return child.returncode
    try:
        child.terminate()                      # SIGTERM (POSIX) / TerminateProcess
    except OSError:
        pass
    try:
        return child.wait(timeout=term_grace)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        try:
            return child.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            return None


class Log:
    def __init__(self, path=None):
        self.path = path
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, msg):
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        print(line, flush=True)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


def build_cmd(opts):
    """The command that runs one watch.py session under this supervisor."""
    return ([sys.executable, WATCH_PY] + list(opts.watch_args)
            + ["--heartbeat", opts.heartbeat,
               "--stall-timeout", str(opts.stall_timeout)])


def service_notify(message):
    """Notify systemd from the supervisor, without a dependency or SDR access."""
    address = os.environ.get('NOTIFY_SOCKET')
    if not address or not hasattr(socket, 'AF_UNIX'):
        return False
    if address.startswith('@'):
        address = '\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), address)
        return True
    except OSError:
        return False


def fresh(snap, now_mono):
    """The heartbeat was written, and analysis last succeeded, within 10 s. On the monotonic clock when the
    heartbeat has it (the Pi boots on a stale clock that steps later; a step between two UTC readings made a
    working child look stale), else on UTC as before."""
    if snap.get('t_mono') is not None and snap.get('last_success_mono') is not None:
        return (0 <= now_mono - snap['t_mono'] <= 10) and (0 <= now_mono - snap['last_success_mono'] <= 10)
    return (0 <= time.time() - snap.get('t', 0) <= 10) and (0 <= time.time() - snap.get('last_success_utc', 0) <= 10)


def monitor(child, opts, stop, log, hardware=None):
    """Watch one child until it ends. Returns clean / stall / crash.

    Independently of the child's own stall check, the heartbeat's `samples_in`
    must keep rising; if it freezes for stall_timeout + grace the child is killed
    and reported as a stall.

    **`windows` is checked too, and that is the point.** `samples_in`
    is `ring.write`, advanced by watch.py's USB callback - so it keeps climbing
    even when the analysis thread is dead and nothing is being detected. Watching
    only `samples_in` made this supervisor and watch.py's own stall check the
    same watchdog wearing two hats. `windows` only advances when a window is
    actually analysed, so it is the one that says the detector is *working*
    rather than merely *receiving*. It gets a longer limit because a window is
    seconds long by design.
    """
    freeze_limit = opts.stall_timeout + opts.grace if opts.stall_timeout else 0
    window_limit = freeze_limit * ANALYSIS_FREEZE_FACTOR if freeze_limit else 0
    last_samples = None
    last_change = time.monotonic()
    last_windows = None
    last_window_change = time.monotonic()
    start = time.monotonic()
    ready_announced = ready_unsent = False
    while True:
        rc = child.poll()
        if rc is not None:
            return classify(rc, killed_for_stall=False)
        if stop.is_set():
            stop_child(child)
            return "clean"
        snap = read_heartbeat(opts.heartbeat)
        if snap is not None and snap.get("pid", child.pid) != child.pid:
            snap = None
        now = time.monotonic()
        if (not ready_announced and snap is not None and snap.get('pid') == child.pid
                and snap.get('ready') and snap.get('windows', 0) > 0 and fresh(snap, now)):
            sent = service_notify('READY=1\nSTATUS=Receiving; first analysis window completed')
            # Until 24 November 2026 a READY that couldn't be sent counted as sent, and systemd, never told,
            # stopped a working detector at its start limit (found in review). Sent again while it stays ready.
            if sent or not os.environ.get('NOTIFY_SOCKET'):
                log('READY: checked current child and completed analysis window')
                ready_announced = True
            elif not ready_unsent:
                log('READY could not be sent to systemd; sending it again while the detector stays ready')
                ready_unsent = True
        if snap is not None and "samples_in" in snap:
            if snap["samples_in"] != last_samples:
                # Proof of recovery is samples ADVANCING between two
                # reads - not a successful airspy_open, not a process
                # that started. After the stale-heartbeat mistake the bar
                # for calling something healthy is deliberately higher.
                if last_samples is not None and hardware is not None and snap.get("ready", False):
                    hardware.on_streaming()
                last_samples, last_change = snap["samples_in"], now
            elif freeze_limit and now - last_change > freeze_limit:
                log(f"  heartbeat frozen for {now-last_change:.0f}s "
                    f"(samples_in stuck at {last_samples}) - killing child")
                stop_child(child)
                return "stall"
        if snap is not None and "windows" in snap:
            if snap["windows"] != last_windows:
                last_windows, last_window_change = snap["windows"], now
            elif window_limit and now - last_window_change > window_limit \
                    and now - start > window_limit:
                log(f"  analysis frozen for {now-last_window_change:.0f}s "
                    f"(windows stuck at {last_windows} while samples kept "
                    f"arriving) - killing child")
                stop_child(child)
                return "analysis failure"
        elif freeze_limit and now - start > freeze_limit + opts.startup_grace:
            log("  no heartbeat within startup window - killing child")
            stop_child(child)
            return "startup timeout"
        stop.wait(0.5)


def run(opts, stop, log, _build_cmd=build_cmd, hardware=None):
    """The supervision loop. Returns a process exit code."""
    consecutive = 0
    launches = 0
    if hardware is None:
        hardware = HardwareNotifier(
            _make_notifier(not getattr(opts, "no_notify", False), log),
            log=log)
    while not stop.is_set():
        cmd = _build_cmd(opts)
        log(f"launch #{launches + 1}: {' '.join(str(c) for c in cmd)}")
        try:
            child = subprocess.Popen(cmd)
        except OSError as e:
            log(f"  cannot launch: {e}")
            return 1
        launches += 1
        # Monotonic, never the wall clock. This Pi has no RTC and cannot get one
        # (the ignition cuts power outright), so it cold-boots on a stale time
        # and timesyncd steps it as soon as the hotspot answers - +170275 s on
        # 16 September. Measured with time.time() a six-minute child that
        # straddled that step reported "clean after 170675s", and worse, cleared
        # `consecutive` below because it looked healthier than --healthy-after.
        # That reset the recovery ladder in exactly the cold-boot-in-the-car
        # case the ladder exists for. watch.py already uses monotonic for the
        # same reason; the supervisor had been left behind.
        started = time.monotonic()
        outcome = monitor(child, opts, stop, log, hardware=hardware)
        ran = time.monotonic() - started

        if stop.is_set():
            log(f"supervisor stopping (child {outcome} after {ran:.0f}s)")
            break
        if outcome == "clean":
            log(f"watch.py exited cleanly after {ran:.0f}s - nothing to recover")
            break

        if getattr(opts, 'no_restart', False):
            log(f'watch.py {outcome}; --no-restart: stopped after one stream attempt')
            service_notify('STATUS=Detector stopped; automatic receiver reopening disabled')
            try:
                hardware.on_stopped(outcome)
            except Exception as exc:
                log(f'  failure notification unavailable: {type(exc).__name__}')
            return STALL_EXIT_CODE

        if ran >= opts.healthy_after:          # it had been healthy: fresh episode
            if consecutive:
                log(f"  (child was healthy for {ran:.0f}s; resetting escalation)")
            consecutive = 0

        action = recover.recovery_action(consecutive, opts.usb_reset,
                                         opts.power_cycle)
        log(f"watch.py {outcome} after {ran:.0f}s -> recovery '{action}' "
            f"(consecutive failure {consecutive + 1})")
        ok, detail = recover.perform(action, hub_location=opts.hub_location)
        log(f"  recovery '{action}': {'ok' if ok else 'FAILED'} - {detail}")
        if action == "relaunch" and not (opts.usb_reset or opts.power_cycle) \
                and consecutive >= 1:
            log("  note: only process-restart is enabled; a true firmware wedge "
                "needs a power cycle (enable --usb-power-cycle with a switchable "
                "hub) or a physical replug")

        consecutive += 1
        # Tell the phone. Contained, so it can never break supervision.
        try:
            hardware.on_failure(consecutive)
        except Exception as e:                   # noqa: BLE001
            log(f"  notify: on_failure raised ({e}) - ignored")
        if opts.max_restarts and consecutive > opts.max_restarts:
            log(f"reached --max-restarts ({opts.max_restarts}) - giving up")
            return 1
        delay = recover.backoff_delay(consecutive, opts.backoff_base,
                                      opts.backoff_cap)
        log(f"  restarting in {delay:.0f}s")
        stop.wait(delay)
    return 0


def _install_signals(stop, log):
    import signal

    def handler(signum, _frame):
        log(f"signal {signum} - shutting down")
        stop.set()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError):
                pass                            # not on the main thread (selftest)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heartbeat", default=os.path.join(paths.ROOT, "run",
                                                        "watch.heartbeat"))
    ap.add_argument("--log", default=os.path.join(paths.ROOT, "logs",
                                                  "supervise.log"))
    ap.add_argument("--stall-timeout", type=float, default=20.0,
                    help="seconds without new samples that counts as a wedge")
    ap.add_argument("--grace", type=float, default=10.0,
                    help="extra slack before the heartbeat backstop kills the child")
    ap.add_argument("--startup-grace", type=float, default=20.0,
                    help="allow this long for the first heartbeat to appear")
    ap.add_argument("--healthy-after", type=float, default=120.0,
                    help="a child that ran this long resets recovery escalation")
    ap.add_argument("--max-restarts", type=int, default=0,
                    help="give up after this many consecutive failures (0 = never)")
    ap.add_argument('--restart', dest='no_restart', action='store_false',
                    help='historic experiments only: reopen the receiver after a failure, which can wedge this Airspy')
    ap.add_argument('--no-restart', dest='no_restart', action='store_true', default=True, help='one child attempt (the default); no automatic reopening/reset/power-cycle after failure')
    ap.add_argument("--no-notify", action="store_true",
                    help="do not push Airspy wedge/recovery to the phone")
    ap.add_argument("--backoff-base", type=float, default=2.0)
    ap.add_argument("--backoff-cap", type=float, default=60.0)
    ap.add_argument("--usb-reset", action="store_true",
                    help="allow a USBDEVFS_RESET as the second recovery tier")
    ap.add_argument("--usb-power-cycle", dest="power_cycle", action="store_true",
                    help="allow a uhubctl VBUS power cycle (needs a switchable hub)")
    ap.add_argument("--hub-location", help="uhubctl -l value for the power cycle")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("watch_args", nargs="*",
                    help="arguments after `--` are passed to watch.py")
    opts = ap.parse_args()

    if opts.selftest:
        raise SystemExit(selftest())
    if opts.no_restart and (opts.usb_reset or opts.power_cycle):
        ap.error('--no-restart cannot be combined with USB reset/power-cycle recovery')

    os.makedirs(os.path.dirname(opts.heartbeat), exist_ok=True)
    log = Log(opts.log)
    log(f"supervisor up (pid {os.getpid()}); watch args: {opts.watch_args or '(none)'}")
    log(f"  recovery: {'disabled; one stream attempt' if opts.no_restart else 'relaunch'}"
        + (" -> usb_reset" if opts.usb_reset else "")
        + (" -> power_cycle" if opts.power_cycle else "")
        + f"; stall after {opts.stall_timeout:g}s")
    stop = threading.Event()
    _install_signals(stop, log)
    hardware = HardwareNotifier(_make_notifier(not opts.no_notify, log), log=log)
    rc = 1
    try:
        rc = run(opts, stop, log, hardware=hardware)
    finally:
        hardware.finish(grace=12 if rc and opts.no_restart and not stop.is_set() else 0)
    log(f"supervisor exit {rc}")
    raise SystemExit(rc)


# --------------------------------------------------------------------------- #

def selftest():
    """Drive the real supervision loop with fake children - no hardware."""
    import tempfile
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("supervise self-test (offline, fake children)")

    # --- pure classification ------------------------------------------------
    check("exit 0 is clean", classify(0, False) == "clean")
    check("exit 75 is a stall", classify(STALL_EXIT_CODE, False) == "stall")
    check("other exit is a crash", classify(1, False) == "crash")
    check("a child we killed for stall is a stall", classify(1, True) == "stall")

    tmp = tempfile.mkdtemp(prefix="suptest_")
    hb = os.path.join(tmp, "hb")
    counter = os.path.join(tmp, "count")

    class Opts:
        no_notify = True           # a test must never reach the operator's phone
        heartbeat = hb
        stall_timeout = 1.0
        grace = 1.0
        startup_grace = 1.0
        healthy_after = 9e9        # never treat as healthy during the test
        max_restarts = 20
        backoff_base = 0.01        # keep the test fast
        backoff_cap = 0.05
        usb_reset = False
        power_cycle = False
        hub_location = None
        watch_args = []

    logs = []
    log = lambda m: logs.append(m)             # noqa: E731 - terse test sink

    # Scenario 1: a child that fails (exit 75) twice, then exits cleanly.
    # The loop must relaunch across the two stalls and stop on the clean exit.
    child_src = (
        "import sys,os,json,time\n"
        "c=os.environ['CNT']\n"
        "n=(int(open(c).read()) if os.path.exists(c) else 0)+1\n"
        "open(c,'w').write(str(n))\n"
        "json.dump({'samples_in':n*1000},open(os.environ['HB'],'w'))\n"
        "time.sleep(0.05)\n"
        "sys.exit(75 if n<3 else 0)\n")
    src_path = os.path.join(tmp, "fakechild.py")
    open(src_path, "w").write(child_src)

    def build_fail_then_clean(_opts):
        return [sys.executable, src_path]

    env_prev = (os.environ.get("CNT"), os.environ.get("HB"))
    os.environ["CNT"], os.environ["HB"] = counter, hb
    try:
        open(counter, "w").write("0")
        stop = threading.Event()
        rc = run(Opts(), stop, log, _build_cmd=build_fail_then_clean)
        launched = int(open(counter).read())
        check("loop relaunched across stalls then stopped on clean exit",
              launched == 3 and rc == 0, f"{launched} launches, rc={rc}")
        check("recovery was escalated per failure",
              any("recovery 'relaunch'" in m for m in logs))

        # Scenario 2: a child that writes one heartbeat then hangs, ignoring it.
        # The heartbeat backstop must kill it and count it as a stall.
        hang_src = (
            "import os,json,time\n"
            "json.dump({'samples_in':42},open(os.environ['HB'],'w'))\n"
            "time.sleep(60)\n")
        hang_path = os.path.join(tmp, "hangchild.py")
        open(hang_path, "w").write(hang_src)
        killed = {"n": 0}

        def build_hang_once(_opts):
            killed["n"] += 1
            # first child hangs; second exits immediately so the loop can end
            return ([sys.executable, hang_path] if killed["n"] == 1
                    else [sys.executable, "-c", "import sys;sys.exit(0)"])

        logs2 = []
        stop2 = threading.Event()
        t0 = time.monotonic()
        rc2 = run(Opts(), stop2, logs2.append, _build_cmd=build_hang_once)
        took = time.monotonic() - t0
        check("frozen heartbeat was detected and the child killed",
              any("heartbeat frozen" in m for m in logs2), "")
        check("supervisor recovered and then stopped cleanly",
              rc2 == 0 and killed["n"] == 2, f"rc={rc2}, {killed['n']} launches")
        check("kill happened promptly (backstop, not a 60 s hang)",
              took < 15, f"{took:.1f}s")
    finally:
        for k, v in zip(("CNT", "HB"), env_prev):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- hardware notifications -------------------------------
    # This class of feature goes wrong by shouting, by going silent when it
    # matters, or by taking the supervisor down with it. One check each.
    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def post(self, message, title=None, priority=0, **kw):
            self.sent.append((title, priority, message))
            return True

    fake = FakeNotifier()
    hw = HardwareNotifier(fake, log=lambda m: None)

    hw.on_failure(1, now=0.0)
    check("one failure is silent (it could be an ordinary crash)",
          len(fake.sent) == 0, f"{len(fake.sent)} push(es)")

    hw.on_failure(2, now=1.0)
    check("the second consecutive failure pushes once", len(fake.sent) == 1,
          fake.sent[0][0] if fake.sent else "nothing")
    check("the wedge push is high priority",
          bool(fake.sent) and fake.sent[0][1] == 1)

    for k in range(3, 9):
        hw.on_failure(k, now=1.0 + k)
    check("six more retries push nothing", len(fake.sent) == 1,
          f"{len(fake.sent)} total after 6 more failures")

    hw.on_streaming(now=20.0)
    check("recovery pushes once", len(fake.sent) == 2,
          fake.sent[-1][0] if len(fake.sent) > 1 else "nothing")

    hw.on_streaming(now=21.0)
    check("a second healthy report is silent", len(fake.sent) == 2)

    hw.on_failure(1, now=30.0)
    hw.on_failure(2, now=31.0)
    check("a NEW episode pushes again", len(fake.sent) == 3,
          f"{len(fake.sent)} total")

    # the reminder: silent inside the interval, fires after it
    hw2 = HardwareNotifier(FakeNotifier(), log=lambda m: None,
                           reminder_s=100.0)
    hw2.on_failure(2, now=0.0)
    hw2.on_failure(3, now=50.0)
    inside = len(hw2._n.sent)
    hw2.on_failure(4, now=150.0)
    after = len(hw2._n.sent)
    check("the reminder fires only after its interval",
          inside == 1 and after == 2, f"{inside} at 50s, {after} at 150s")

    # rule 2: a notification must never be able to break supervision
    class ExplodingNotifier:
        def post(self, *a, **kw):
            raise RuntimeError("pushover on fire")

    hw3 = HardwareNotifier(ExplodingNotifier(), log=lambda m: None)
    try:
        hw3.on_failure(2)
        hw3.on_streaming()
        contained = True
    except Exception:                                # noqa: BLE001
        contained = False
    check("an exploding notifier does not break supervision", contained)

    hw4 = HardwareNotifier(None, log=lambda m: None)
    try:
        hw4.on_failure(5)
        hw4.on_streaming()
        safe = True
    except Exception:                                # noqa: BLE001
        safe = False
    check("no notifier at all is safe", safe)

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    main()
