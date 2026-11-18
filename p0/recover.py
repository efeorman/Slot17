#!/usr/bin/env python3
"""Recover a wedged Airspy without a human unplugging it.

earlier testing is the hard fact this module is built around: after a
stop/start cycle this board's firmware hangs its vendor-request handler, and
**only removing power clears it** - a USB re-enumeration alone does not. In a car
there is nobody to pull the plug, so recovery has to be done in software, and it
escalates:

  * ``relaunch``     - just start ``watch.py`` again. A fresh process re-runs
                       ``airspy_init``/``airspy_open``; the kernel has already
                       reclaimed the USB interface a crashed process left behind.
                       This clears a *crash*, but not a true firmware hang.
  * ``usb_reset``    - ask the kernel to reset the device (Linux USBDEVFS_RESET).
                       Re-enumerates without cutting VBUS. Sometimes enough, often
                       not for this firmware, but it is free and non-destructive.
  * ``power_cycle``  - actually cut and restore VBUS on the port, via ``uhubctl``.
                       This is the only thing that reliably clears the firmware
                       hang, and it needs a hub whose ports support power
                       switching (many do not). Opt-in, because on a hub that
                       cannot switch power it does nothing useful, and because it
                       briefly powers the port down.

Nothing here runs unless the supervisor asks for it, and ``power_cycle`` only if
the operator turned it on. On Windows (the bench) the two hardware steps are
no-ops that report "unsupported", so the logic is still testable there.

    python recover.py --find                 # show the Airspy's USB location
    python recover.py --usb-reset            # one reset attempt (Linux)
    python recover.py --power-cycle --hub-location 1-1   # one uhubctl cycle
    python recover.py --selftest             # offline logic checks
"""
import argparse
import shutil
import subprocess
import sys

# Airspy Mini / Airspy One USB identity (Great Scott Gadgets range).
AIRSPY_VID = 0x1d50
AIRSPY_PID = 0x60a1

# Escalation ladder. Index by how many times recovery has already been tried for
# the *current* run of trouble; capped at the last entry so it keeps trying the
# strongest available action.
_LADDER = ["relaunch", "usb_reset", "power_cycle"]


def recovery_action(consecutive_failures, allow_usb_reset, allow_power_cycle):
    """Pick the next recovery step, honouring what the operator enabled.

    Pure and total, so the escalation policy can be unit-tested without any
    hardware. ``consecutive_failures`` is how many recovery attempts have already
    failed in the current episode (0 for the first).
    """
    ladder = ["relaunch"]
    if allow_usb_reset:
        ladder.append("usb_reset")
    if allow_power_cycle:
        ladder.append("power_cycle")
    idx = min(max(consecutive_failures, 0), len(ladder) - 1)
    return ladder[idx]


def backoff_delay(consecutive_failures, base=2.0, cap=60.0):
    """Exponential backoff with a ceiling, so a dead board is retried calmly."""
    return float(min(cap, base * (2 ** max(consecutive_failures, 0))))


# --------------------------------------------------------------------------- #
# Locating the device

def find_airspy():
    """Return {'bus','device','path'} for the Airspy, or None. Linux best-effort.

    Reads sysfs rather than shelling out, so it needs nothing installed. On any
    non-Linux platform (or if the device is absent) it returns None.
    """
    if not sys.platform.startswith("linux"):
        return None
    import glob
    import os
    for dev_dir in glob.glob("/sys/bus/usb/devices/*"):
        try:
            vid = open(os.path.join(dev_dir, "idVendor")).read().strip()
            pid = open(os.path.join(dev_dir, "idProduct")).read().strip()
        except OSError:
            continue
        if int(vid, 16) == AIRSPY_VID and int(pid, 16) == AIRSPY_PID:
            try:
                busnum = int(open(os.path.join(dev_dir, "busnum")).read())
                devnum = int(open(os.path.join(dev_dir, "devnum")).read())
            except OSError:
                continue
            return {"bus": busnum, "device": devnum,
                    "path": "/dev/bus/usb/%03d/%03d" % (busnum, devnum),
                    "sysfs": os.path.basename(dev_dir)}
    return None


# --------------------------------------------------------------------------- #
# The three actions. Each returns (ok, detail) and never raises.

def do_relaunch():
    """No device action - the supervisor simply starts watch.py again."""
    return True, "process will be relaunched"


def do_usb_reset(target=None):
    """Reset the device with USBDEVFS_RESET (Linux). Non-destructive."""
    if not sys.platform.startswith("linux"):
        return False, "usb_reset unsupported on this platform"
    import fcntl
    target = target or find_airspy()
    if not target:
        return False, "Airspy not found on the USB bus"
    USBDEVFS_RESET = ord("U") << 8 | 20            # from <linux/usbdevice_fs.h>
    try:
        fd = open(target["path"], "wb")
    except OSError as e:
        return False, f"cannot open {target['path']}: {e}"
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        return True, f"reset {target['path']}"
    except OSError as e:
        return False, f"ioctl reset failed: {e}"
    finally:
        fd.close()


def do_power_cycle(hub_location=None, target=None, timeout=20.0):
    """Cut and restore VBUS on the Airspy's port with uhubctl.

    ``hub_location`` is uhubctl's -l value (e.g. '1-1'); if omitted uhubctl acts
    on all switchable hubs it finds, which is coarse but usually fine on a Pi
    with one downstream hub. Requires uhubctl installed and a hub with per-port
    power switching.
    """
    if not sys.platform.startswith("linux"):
        return False, "power_cycle unsupported on this platform"
    exe = shutil.which("uhubctl")
    if not exe:
        return False, "uhubctl not installed (apt install uhubctl)"
    cmd = [exe, "-a", "cycle"]
    if hub_location:
        cmd += ["-l", hub_location]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "uhubctl timed out"
    if p.returncode == 0:
        return True, "uhubctl cycled " + (hub_location or "all switchable ports")
    return False, f"uhubctl rc={p.returncode}: {(p.stderr or p.stdout).strip()[:200]}"


def perform(action, hub_location=None):
    """Run one recovery action by name. Returns (ok, detail)."""
    if action == "relaunch":
        return do_relaunch()
    if action == "usb_reset":
        return do_usb_reset()
    if action == "power_cycle":
        return do_power_cycle(hub_location=hub_location)
    return False, f"unknown action {action!r}"


# --------------------------------------------------------------------------- #

def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("recover self-test (offline)")

    # escalation ladder honours what is enabled
    check("first attempt is always the cheap relaunch",
          recovery_action(0, True, True) == "relaunch")
    check("second attempt resets USB when allowed",
          recovery_action(1, True, True) == "usb_reset")
    check("third attempt power-cycles when allowed",
          recovery_action(2, True, True) == "power_cycle")
    check("escalation caps at strongest enabled action",
          recovery_action(9, True, True) == "power_cycle")
    check("without power-cycle it stops at usb_reset",
          recovery_action(9, True, False) == "usb_reset")
    check("with nothing enabled it only ever relaunches",
          recovery_action(9, False, False) == "relaunch")
    check("power-cycle can be reached even if usb_reset is off",
          recovery_action(1, False, True) == "power_cycle",
          recovery_action(1, False, True))

    # backoff grows then plateaus
    d0, d1, d5 = backoff_delay(0), backoff_delay(1), backoff_delay(5)
    check("backoff increases", d1 > d0, f"{d0}->{d1}")
    check("backoff is capped", d5 <= 60.0, f"{d5}")

    # hardware actions degrade gracefully on a non-Linux bench
    if not sys.platform.startswith("linux"):
        ok1, _ = do_usb_reset()
        ok2, _ = do_power_cycle()
        check("usb_reset reports unsupported off Linux", ok1 is False)
        check("power_cycle reports unsupported off Linux", ok2 is False)
    else:
        # on Linux without hardware present, find/reset simply report absence
        ok1, detail = do_usb_reset()
        check("usb_reset handles a missing device without raising",
              ok1 in (True, False), detail)

    # unknown action never raises
    okx, _ = perform("nonsense")
    check("unknown action returns False, not an exception", okx is False)

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", action="store_true", help="locate the Airspy on USB")
    ap.add_argument("--usb-reset", action="store_true", help="one USBDEVFS_RESET")
    ap.add_argument("--power-cycle", action="store_true", help="one uhubctl cycle")
    ap.add_argument("--hub-location", help="uhubctl -l value, e.g. 1-1")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())
    if args.find:
        t = find_airspy()
        print(t if t else "Airspy not found (or not on Linux).")
        raise SystemExit(0 if t else 1)
    if args.usb_reset:
        ok, detail = do_usb_reset()
        print(("ok: " if ok else "failed: ") + detail)
        raise SystemExit(0 if ok else 1)
    if args.power_cycle:
        ok, detail = do_power_cycle(hub_location=args.hub_location)
        print(("ok: " if ok else "failed: ") + detail)
        raise SystemExit(0 if ok else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
