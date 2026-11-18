#!/usr/bin/env python3
"""Prove the radio hardware works, one check at a time.

Run this after every replug. It walks up from "is the OS aware of it" to
"can it actually stream at full rate", and stops at the first thing that
breaks, so the failure is unambiguous.

    python selftest.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

BIN = paths.TOOLS_BIN
AIRSPY_VID = "VID_1D50"
RTL_VID = "VID_0BDA"

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    except FileNotFoundError:
        return -1, "executable not found: " + cmd[0]


def competing_processes():
    """Anything still holding the board stops the next open from working.

    A process that dies mid-stream, or an SDR GUI left open, leaves the
    board configured and streaming; the next caller then gets -1000.
    """
    code, out = run(["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process | "
                     "Where-Object { $_.Name -match 'airspy|rtl_|SDRSharp' } | "
                     "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.Name }"])
    return [l.strip() for l in out.splitlines() if l.strip()]


def pnp_present(vid):
    code, out = run(["powershell", "-NoProfile", "-Command",
                     "Get-PnpDevice -PresentOnly | "
                     "Where-Object { $_.InstanceId -match '%s' } | "
                     "ForEach-Object { $_.Status + ' ' + $_.FriendlyName }" % vid])
    return [l.strip() for l in out.splitlines() if l.strip()]


def stream_test(rate_hz, secs=1.0):
    """Try a real bulk transfer; returns (bytes_written, message)."""
    fd, path = tempfile.mkstemp(suffix=".iq")
    os.close(fd)
    n = int(rate_hz * secs)
    code, out = run([os.path.join(BIN, "airspy_rx.exe"), "-r", path,
                     "-f", "392.5", "-a", str(int(rate_hz)), "-t", "2",
                     "-n", str(n)], timeout=90)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    try:
        os.remove(path)
    except OSError:
        pass
    err = [l.strip() for l in out.splitlines() if "failed" in l or "ERROR" in l]
    return size, (err[0] if err else "")


def main():
    try_six = "--try6" in sys.argv
    print("=" * 68)
    print("  P0 radio self-test")
    print("=" * 68)
    failures = []

    # --- 1. does Windows see the board -----------------------------------
    print("\n0. Nothing else holding the board")
    busy = competing_processes()
    if busy:
        for b in busy:
            print(f"   {BAD} still running: {b}")
        print("        -> close it first. A process holding the board, or one")
        print("           that died mid-stream, makes every later open fail.")
        failures.append("device-busy")
    else:
        print(f"   {OK} No SDR process running.")

    print("\n1. USB enumeration")
    air = pnp_present(AIRSPY_VID)
    if air:
        for line in air:
            print(f"   {OK} Airspy: {line}")
    else:
        print(f"   {BAD} No Airspy on USB (VID_1D50 absent).")
        print("        -> check the cable is a DATA cable, not charge-only.")
        failures.append("airspy-enumeration")

    rtl = pnp_present(RTL_VID)
    if rtl:
        for line in rtl:
            print(f"   {OK} RTL-SDR: {line}")
    else:
        print(f"   {WARN} No RTL-SDR plugged in (optional at this stage).")

    if not air:
        print("\nStopping: nothing to talk to.")
        return 1

    # --- 2. control transfers --------------------------------------------
    print("\n2. Control transfers (airspy_info)")
    code, out = run([os.path.join(BIN, "airspy_info.exe")])
    fw = [l.strip() for l in out.splitlines() if "Firmware" in l]
    if "failed" in out or not fw:
        print(f"   {BAD} Control transfer failed:")
        for l in out.splitlines():
            if l.strip():
                print("        " + l.strip())
        print("        -> the board is wedged. Unplug it, wait 5 s, plug it")
        print("           back in, and run this script again BEFORE anything else.")
        return 1
    for l in out.splitlines():
        if l.strip() and "airspy_lib" not in l:
            print("        " + l.strip())
    print(f"   {OK} Board responds.")

    # --- 3. bulk streaming, low rate first --------------------------------
    print("\n3. Streaming 3 MSPS (12 MB/s)")
    size, err = stream_test(3_000_000)
    if size > 1_000_000:
        print(f"   {OK} {size/1e6:.1f} MB in 1 s.")
    else:
        print(f"   {BAD} got {size} bytes. {err}")
        failures.append("stream-3msps")

    # 6 MSPS is opt-in on purpose. On this board a failed start_rx at 6 MSPS
    # leaves the USB endpoint stalled, and nothing short of a physical replug
    # clears it - libusb_reset_device() is a no-op under WinUSB. Running the
    # test by default would break the very device it is meant to check.
    if try_six:
        print("\n4. Streaming 6 MSPS (24 MB/s)  [opt-in]")
        size, err = stream_test(6_000_000)
        if size > 2_000_000:
            print(f"   {OK} {size/1e6:.1f} MB in 1 s - full 6 MHz window usable.")
        else:
            print(f"   {BAD} got {size} bytes. {err}")
            failures.append("stream-6msps")
            print("        -> the board is now wedged; unplug and replug it.")
            print("           Stay at 3 MSPS. See the day-1 guide on firmware.")
    else:
        print("\n4. Streaming 6 MSPS  [skipped by default]")
        print("        This board fails at 6 MSPS, and a failed attempt stalls")
        print("        its USB endpoint until physically replugged - so the test")
        print("        is opt-in. Run  python selftest.py --try6  to retry it,")
        print("        e.g. after a firmware update or on a different port.")


    # --- 5. RTL-SDR ------------------------------------------------------
    if rtl:
        print("\n5. RTL-SDR (rtl_test)")
        code, out = run([os.path.join(BIN, "rtl_test.exe"), "-t"], timeout=45)
        if "No supported devices found" in out or "usb_open error" in out:
            print(f"   {BAD} Not usable yet - the dongle still has its DVB-T driver.")
            print(r"        -> run Zadig (tools\zadig.exe) as administrator,")
            print("           Options > List All Devices, pick 'Bulk-In, Interface (0)',")
            print("           and replace the driver with WinUSB.")
            failures.append("rtl-driver")
        else:
            for l in out.splitlines():
                if l.strip():
                    print("        " + l.strip())
            print(f"   {OK} RTL-SDR responds.")

    print("\n" + "=" * 68)
    if failures:
        print("  RESULT: not ready - " + ", ".join(failures))
        return 1
    print("  RESULT: all good. Next: python capture.py --freq 392.5 --secs 5 "
          "--tag downlink-survey")
    if not try_six:
        print("  (6 MSPS untested - see above. Everything runs at 3 MSPS.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
