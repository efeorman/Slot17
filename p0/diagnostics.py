#!/usr/bin/env python3
"""Receiver-output and host diagnostics, with explicit measurement limits.

The passive dipole has no health/status sensor. Received samples cannot certify
its connection, efficiency or impedance. Report levels and comparisons instead.

Three groups, each usable on its own:

  * receiver_info(dev)  - needs an open Airspy: firmware, serial, sample rates.
  * signal_health(iq, fs) - digital output levels/headroom and a legacy low-input
    advisory. This cannot certify raw ADC headroom or antenna connection.
  * host_health()       - the computer: CPU temperature, under-voltage/throttle
    (Raspberry Pi), OS and uptime. No device needed.

    python diagnostics.py --selftest     # everything that needs no hardware
    python diagnostics.py                 # opens the Airspy for a full report
"""
import argparse
import ctypes
import os
import platform
import subprocess
import sys
import time
import threading
import shutil

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SOFTWARE_VERSION = "p0-1.1"

# Legacy constant name: this is the int16 I/Q OUTPUT limit, not a raw ADC rail.
# libairspy digitally converts the 12-bit ADC samples before this program sees IQ.
ADC_FULL_SCALE = 32767.0
# Legacy diagnostic constant; validity is now checked on each signed component,
# including non-finite data. A large complex magnitude alone is not clipping.
GLITCH_MAGNITUDE = 1.01 * np.sqrt(2.0) * ADC_FULL_SCALE

# Antenna-present heuristic. This is an ABSOLUTE ADC-count test, so a threshold
# is only meaningful at the GAIN it was measured at - which is what went wrong:
# the 1500 below was measured at lna/mixer/vga = 12/12/12, but watch.py defaults
# to 8/8/8, and a live 20-minute run duly spent the whole time warning "input
# level low - check the antenna" at peak ~375 while detecting signals perfectly
# well. A false "check the antenna" is expensive: chasing a disconnected input
# once cost us an evening, and crying wolf is how a real one gets
# ignored.
#
# So the thresholds live in a table keyed by gain, and at a gain we have never
# measured the honest answer is "unknown", not "suspect".
ANTENNA_SUSPECT_PEAK = {
    # (lna, mixer, vga): peak ADC counts below which the input looks open
    (12, 12, 12): 1500.0,        # dipole connected 4654, input open 845
}
# ...and even where we do have a reference, a genuinely strong signal lifts a
# disconnected-looking floor, so the flag is advisory, never a hard error.


def software_version():
    from version import RELEASE, provenance
    return {"software": RELEASE, "source_sha256": provenance()["source_sha256"],
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": f"{platform.system()} {platform.release()}",
            "machine": platform.machine()}


def receiver_info(dev):
    """Firmware, serial and supported sample rates from an open device."""
    import airspy_ctypes as A
    info = {}
    try:
        info["firmware"] = A.version_string(dev)
    except Exception as e:                       # noqa: BLE001 - report, never raise
        info["firmware"] = f"unreadable ({e})"
    try:
        info["serial"] = A.serial_number(dev)
    except Exception:
        info["serial"] = None
    try:
        info["sample_rates_hz"] = A.samplerates(dev)
    except Exception:
        info["sample_rates_hz"] = None
    return info


def signal_health(iq, fs, gain=None):
    """Digital int16 I/Q output levels, not calibrated dBm or raw ADC headroom.

    `saturating` is a digital near-rail advisory; analog overload can exist without
    it. The legacy gain-specific input heuristic cannot prove antenna connection.
    A caller that subsamples must report its stride; brief peaks can be missed.
    """
    # The detector passes a strided view across 144 MB. Copy those selected
    # samples once rather than walking that wide memory range for every statistic.
    iq = np.ascontiguousarray(iq)
    mag = np.abs(iq)
    finite = bool(np.isfinite(iq).all())
    component = np.maximum(np.abs(iq.real), np.abs(iq.imag))
    peak = float(mag.max()) if mag.size and finite else 0.0
    component_peak = float(component.max()) if mag.size and finite else 0.0
    rms = float(np.sqrt(np.mean(mag ** 2, dtype=np.float64))) if mag.size and finite else 0.0
    headroom_db = float(20 * np.log10(32768 / component_peak)) if component_peak > 0 else None
    frac_near_rail = (float(np.mean(component > 0.95 * 32768))
                      if mag.size else 0.0)
    # A physically impossible peak means the block is corrupt; its stats (rms,
    # near-rail fraction, antenna check) are then meaningless, so report it as a
    # glitch and do not let it masquerade as clipping or a connected antenna.
    glitch = not finite or bool(((iq.real < -32768) | (iq.real > 32767) |
                                 (iq.imag < -32768) | (iq.imag > 32767)).any())
    thr = ANTENNA_SUSPECT_PEAK.get(tuple(gain) if gain is not None else None)
    if glitch or thr is None:
        antenna_suspect = None            # corrupt block, or no reference gain
        antenna_note = ("block corrupt" if glitch else
                        f"no reference for gain {tuple(gain)}" if gain is not None
                        else "gain not supplied")
    else:
        antenna_suspect = peak < thr
        antenna_note = f"peak {peak:.0f} vs {thr:.0f} at gain {tuple(gain)}"
    return {
        "peak_counts": round(peak, 1),
        "component_peak_counts": round(component_peak, 1),
        "rms_counts": round(rms, 1),
        "units": "int16 I/Q output counts; not raw ADC or calibrated received power",
        "rms_dbfs": round(float(20*np.log10(rms/32768)), 2) if rms > 0 and not glitch else None,
        "dbfs_reference": "complex RMS amplitude / 32768; a full-scale complex tone is 0 dBFS",
        "headroom_db": round(headroom_db, 1) if headroom_db is not None else None,
        "fraction_near_rail": round(frac_near_rail, 4),
        "glitch_block": glitch,                       # corrupt/garbage samples
        "saturating": (not glitch) and frac_near_rail > 1e-4,
        "antenna_suspect": antenna_suspect,       # True / False / None=unknown
        "antenna_note": antenna_note,
        "samples": int(mag.size),
        "seconds": round(mag.size / fs, 2) if fs else None,
    }


def _read_pi_temperature_c():
    """SoC temperature in Celsius on Linux/Pi, else None."""
    path = "/sys/class/thermal/thermal_zone0/temp"
    try:
        with open(path) as fh:
            return int(fh.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=2).stdout
        return float(out.split("=")[1].split("'")[0])
    except Exception:                                # noqa: BLE001
        return None


def _read_pi_throttled():
    """Decode `vcgencmd get_throttled` into the states that matter, or None."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
        bits = int(out.strip().split("=")[1], 16)
    except Exception:                                # noqa: BLE001
        return None
    return {
        "undervoltage_now": bool(bits & 0x1),
        "frequency_capped_now": bool(bits & 0x2),
        "throttled_now": bool(bits & 0x4),
        "soft_temperature_limit_now": bool(bits & 0x8),
        "undervoltage_since_boot": bool(bits & 0x10000),
        "frequency_capped_since_boot": bool(bits & 0x20000),
        # Firmware bit 18 is historical throttling; bit 19 is the distinct
        # historical soft-temperature-limit flag.
        "throttled_since_boot": bool(bits & 0x40000),
        "soft_temperature_limit_since_boot": bool(bits & 0x80000),
        "raw": hex(bits),
    }


def host_health():
    """The computer's own health: temperature, power, OS, uptime."""
    h = {"os": f"{platform.system()} {platform.release()}",
         "machine": platform.machine()}
    temp = _read_pi_temperature_c()
    h["cpu_temp_c"] = round(temp, 1) if temp is not None else None
    # Conservative project advisory, not a claimed Pi operating-temperature limit.
    h["temp_warn"] = (temp is not None and (temp < 0 or temp > 70))
    h["power"] = _read_pi_throttled()          # None off-Pi
    try:
        h["uptime_s"] = round(time.time() - psutil_boot_time())
    except Exception:                            # noqa: BLE001
        h["uptime_s"] = None
    return h


def psutil_boot_time():
    """Boot time without a psutil dependency: /proc/uptime on Linux."""
    with open("/proc/uptime") as fh:
        return time.time() - float(fh.read().split()[0])


class HostSampler:
    """Slow OS probes on their own thread, never the RF callback/analysis loop."""
    def __init__(self, recorder, interval=15):
        self.recorder, self.interval = recorder, interval
        self.latest = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='host-health', daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=6)

    def _run(self):
        while not self._stop.is_set():
            began = time.monotonic()
            row = {'schema_version': 1, 'event_type': 'host_health'}
            try:
                row.update(host_health())
                row['free_bytes'] = shutil.disk_usage(self.recorder.directory).free
                row['load_average'] = list(os.getloadavg()) if hasattr(os, 'getloadavg') else None
                try:
                    with open('/proc/self/status') as f:
                        fields = dict(line.split(':', 1) for line in f if ':' in line)
                    row['process_rss_bytes'] = int(fields['VmRSS'].split()[0])*1024
                except (OSError, KeyError, ValueError):
                    row['process_rss_bytes'] = None
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'[:240]
            row.update(measured_utc=time.time(), probe_seconds=round(time.monotonic()-began, 4))
            self.latest = row
            self.recorder.event(row)
            self._stop.wait(self.interval)


def overall_status(sig=None, host=None):
    """Roll the checks into ok / warn / fault, the way the logo colour would."""
    problems = []
    if sig:
        if sig.get("saturating"):
            problems.append(("warn", "digital I/Q near rail; compare lower gain while parked"))
        if sig.get("antenna_suspect") is True:
            problems.append(("warn", "low input against legacy reference; antenna connection unverified"))
    if host:
        if host.get("temp_warn"):
            problems.append(("warn", f"temperature {host.get('cpu_temp_c')} C "
                                      "outside project's 0-70 C advisory band"))
        p = host.get("power") or {}
        if p.get("undervoltage_now"):
            problems.append(("fault", "under-voltage now - inadequate supply"))
        elif p.get("throttled_now"):
            problems.append(("warn", "CPU throttled now"))
        elif p.get("undervoltage_since_boot"):
            problems.append(("warn", "under-voltage seen since boot"))
    level = ("fault" if any(s == "fault" for s, _ in problems) else
             "warn" if problems else "ok")
    return {"status": level, "issues": [m for _, m in problems]}


def format_report(recv=None, sig=None, host=None):
    L = []
    sw = software_version()
    L.append(f"software {sw['software']}   python {sw['python']}   "
             f"numpy {sw['numpy']}")
    L.append(f"host     {sw['platform']}  ({sw['machine']})")
    if recv is not None:
        L.append(f"receiver firmware {recv.get('firmware')}")
        if recv.get("serial"):
            L.append(f"         serial   {recv['serial']}")
        if recv.get("sample_rates_hz"):
            rates = ", ".join(f"{r/1e6:g}" for r in recv["sample_rates_hz"])
            L.append(f"         rates    {rates} MSPS")
    if host is not None:
        t = host.get("cpu_temp_c")
        L.append(f"temp     {t if t is not None else 'n/a'} C"
                 f"{'  ** OUT OF RANGE **' if host.get('temp_warn') else ''}")
        p = host.get("power")
        if p:
            flags = [k for k in ("undervoltage_now", "throttled_now",
                                 "undervoltage_since_boot", "throttled_since_boot")
                     if p.get(k)]
            L.append(f"power    {p['raw']}  "
                     f"{'  '.join(flags) if flags else 'clean'}")
        if host.get("uptime_s") is not None:
            L.append(f"uptime   {host['uptime_s'] / 3600:.1f} h")
    if sig is not None:
        L.append(f"input    complex peak {sig['peak_counts']:.0f} output counts; "
                 f"digital component headroom {sig['headroom_db']} dB   "
                 f"rms {sig['rms_counts']:.0f}")
        notes = []
        if sig["saturating"]:
            notes.append("DIGITAL NEAR-RAIL")
        if sig["antenna_suspect"] is True:
            notes.append("ANTENNA?")
        elif sig["antenna_suspect"] is None:
            notes.append(f"antenna check n/a ({sig.get('antenna_note')})")
        if notes:
            L.append(f"         {'  '.join(notes)}")
    st = overall_status(sig, host)
    L.append(f"STATUS   {st['status'].upper()}"
             + ("  ->  " + "; ".join(st["issues"]) if st["issues"] else ""))
    return "\n".join(L)


# --------------------------------------------------------------------------- #

def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              f"{'  ' + detail if detail else ''}")

    print("diagnostics self-test (no hardware)")

    # Exercise the documented firmware bit meanings through the command parser.
    # These states differ even though both were previously read as bit 19.
    from unittest.mock import patch
    for raw, expected in (("0x40000", True), ("0x80000", False), ("0x0", False)):
        with patch.object(subprocess, "run") as command:
            command.return_value.stdout = f"throttled={raw}\n"
            power = _read_pi_throttled()
        check(f"firmware {raw}: historical throttling is {expected}",
              power is not None and power["throttled_since_boot"] is expected)

    sw = software_version()
    check("software version present", bool(sw["software"]), sw["software"])

    fs = 6_000_000.0
    rng = np.random.default_rng(0)
    n = int(fs * 0.2)

    # A healthy, quiet block at a realistic level: peak ~4600, no clipping.
    quiet = ((rng.normal(size=n) + 1j * rng.normal(size=n)) * 700
             ).astype(np.complex64)
    hq = signal_health(quiet, fs, gain=(12, 12, 12))
    check("healthy block: not saturating", not hq["saturating"],
          f"near-rail {hq['fraction_near_rail']}")
    check("sample exceeds legacy low-input reference; connection not proven", not hq["antenna_suspect"],
          f"peak {hq['peak_counts']:.0f}")
    check("healthy block: positive headroom",
          hq["headroom_db"] is not None and hq["headroom_db"] > 10,
          f"{hq['headroom_db']} dB")

    # A disconnected input: everything near zero.
    dead = ((rng.normal(size=n) + 1j * rng.normal(size=n)) * 120
            ).astype(np.complex64)
    hd = signal_health(dead, fs, gain=(12, 12, 12))
    check("dead input flagged antenna_suspect", hd["antenna_suspect"] is True,
          f"peak {hd['peak_counts']:.0f}")

    # The live-run finding: at a gain we have no reference for, the honest answer
    # is "unknown", not a warning. A 20-minute run once warned continuously about
    # a perfectly good antenna because 1500 counts was a 12/12/12 number applied
    # at 8/8/8.
    hu = signal_health(dead, fs, gain=(3, 3, 3))
    check("unknown gain -> antenna check is 'unknown', not a false warning",
          hu["antenna_suspect"] is None and not overall_status(hu, None)["issues"],
          hu["antenna_note"])
    hn = signal_health(dead, fs)
    check("no gain supplied -> also 'unknown'", hn["antenna_suspect"] is None,
          hn["antenna_note"])

    # A clipping block: values driven to the rail.
    hot = (np.full(n, ADC_FULL_SCALE) + 1j * np.full(n, ADC_FULL_SCALE)
           ).astype(np.complex64)
    hh = signal_health(hot, fs)
    check("clipping block flagged saturating", hh["saturating"],
          f"near-rail {hh['fraction_near_rail']}")
    check("real clipping is not called a glitch", not hh["glitch_block"],
          f"peak {hh['peak_counts']:.0f}")

    # A corrupt block: physically impossible magnitudes (the live-test finding).
    bad = np.full(n, 500000 + 500000j, np.complex64)
    hb = signal_health(bad, fs)
    check("corrupt block flagged as glitch, not clipping",
          hb["glitch_block"] and not hb["saturating"],
          f"peak {hb['peak_counts']:.0f}")

    host = host_health()
    check("host_health returns a dict", isinstance(host, dict),
          f"temp {host.get('cpu_temp_c')} C, os {host.get('os')}")

    st = overall_status(hq, {"temp_warn": False, "power": None})
    check("healthy rolls up to ok", st["status"] == "ok", st["status"])
    st2 = overall_status(hd, {"temp_warn": True, "cpu_temp_c": 85, "power": None})
    check("temperature and low-input advisories roll up to warn", st2["status"] == "warn",
          "; ".join(st2["issues"]))

    print("\nExample report (synthetic):")
    print("  " + format_report(sig=hq, host=host).replace("\n", "\n  "))

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def live_report(rate=6.0, seconds=1.0):
    """Open the Airspy, grab a short block, and print a full health report."""
    import airspy_ctypes as A
    import collections
    import threading

    fs = int(rate * 1e6)
    A.check(A.lib.airspy_init(), "airspy_init")
    dev = ctypes.c_void_p()
    try:
        A.check(A.lib.airspy_open(ctypes.byref(dev)), "airspy_open")
    except A.AirspyError as e:
        print(f"{e}\nBoard wedged or absent - unplug, wait 5 s, replug.")
        A.lib.airspy_exit()
        return 1

    grabbed = collections.deque()
    want = int(seconds * fs)
    got = threading.Event()

    def _cb(tp):
        t = tp.contents
        try:
            buf = ctypes.string_at(t.samples, t.sample_count * 4)
            grabbed.append(buf)
            if sum(len(b) for b in grabbed) >= want * 4:
                got.set()
        except Exception:  # noqa: BLE001, S110 - nothing may escape into the driver's thread
            pass
        return 0
    cb = A.SAMPLE_BLOCK_CB(_cb)

    recv = receiver_info(dev)
    try:
        A.check(A.lib.airspy_set_sample_type(dev, A.SAMPLE_INT16_IQ), "sample_type")
        A.check(A.lib.airspy_set_samplerate(dev, fs), "samplerate")
        for g, fn in ((12, "set_lna_gain"), (12, "set_mixer_gain"),
                      (12, "set_vga_gain")):
            A.check(getattr(A.lib, f"airspy_{fn}")(dev, g), fn)
        A.check(A.lib.airspy_start_rx(dev, cb, None), "start_rx")
        A.check(A.lib.airspy_set_freq(dev, int(382.5e6)), "set_freq")
        got.wait(timeout=5.0)
    except A.AirspyError as e:
        print(f"{e}")
    finally:
        time.sleep(0.2)
        try:
            A.lib.airspy_stop_rx(dev)
        except OSError:
            pass
        time.sleep(0.3)
        # airspy_close() deliberately skipped.

    raw = np.frombuffer(b"".join(grabbed), dtype=np.int16)
    if raw.size >= 2:
        iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32))
        sig = signal_health(iq, fs)
    else:
        sig = None
        print("  (no samples captured)")
    print(format_report(recv=recv, sig=sig, host=host_health()))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rate", type=float, default=6.0)
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    raise SystemExit(live_report(rate=args.rate))


if __name__ == "__main__":
    main()
