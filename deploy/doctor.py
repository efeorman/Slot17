#!/usr/bin/env python3
"""`bash setup.sh doctor`: one plain verdict per part, for "why does it not work?".

Only looks: it never opens the receiver, never starts, stops or restarts a service and changes no file.
It reads the detector's heartbeat twice, two seconds apart, rather than trusting the clock: the car
computer has no clock of its own and boots with a wrong time.

Exit status 0 when nothing is a problem, 1 otherwise. Paste the output into a help request; it
contains no keys or passwords (the server address is shown, the keys are not).
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
HEARTBEAT = Path("/run/tetra/watch.heartbeat")
BLUEZ_MAIN_CONF = Path("/etc/bluetooth/main.conf")
DISPLAY_CONFIG = HOME / ".config/slot17-display.json"
PUSHOVER_CONFIG = HOME / ".config/tetra-pushover.json"
PUSHOVER_SWITCH = HOME / ".config/slot17-notify.json"
BLUETOOTH_SETTINGS = HOME / ".config/slot17-bluetooth.json"
RELEASE_KEY = HOME / ".config/slot17-release-key.pub"
SERVICE_FILE = Path("/etc/systemd/system/tetra-detector.service")
SERVER_CONFIG = Path("/opt/slot17-display/config.json")
AIRSPY_USB = "1d50:60a1"
RECEIVER_SETTINGS = HOME / ".config/slot17-receiver.json"
# kind: the name people see, its USB ID (vendor:product); as in p0/receiver.py.
RECEIVERS = {"airspy": ("Airspy Mini", AIRSPY_USB), "rsp1b": ("SDRplay RSP1B", "1df7:3050"),
             "rsp1a": ("SDRplay RSP1A", "1df7:3000"), "rtlsdr2": ("RTL-SDR pair", "0bda:2838")}
SDRPLAY_LIBRARIES = (Path("/usr/local/lib/libsdrplay_api.so.3"), Path("/usr/lib/libsdrplay_api.so.3"))
RTLSDR_USB = ("0bda:2838", "0bda:2832")
RTLSDR_LIBRARIES = tuple(Path(d) / "librtlsdr.so.0" for d in ("/usr/lib/aarch64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
                                                              "/usr/lib/arm-linux-gnueabihf", "/usr/local/lib", "/usr/lib"))
DVB_MODULE = Path("/sys/module/dvb_usb_rtl28xxu")       # Linux's DVB-T television driver for these sticks

OK, NOTE, PROBLEM = "ok", "note", "PROBLEM"


class Probe:
    """Everything the doctor learns from the machine, in one place, so tests can pretend."""

    def run(self, *cmd, timeout=5):
        """(exit status, output), or (None, "") when the command does not exist or hangs."""
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        return out.returncode, (out.stdout or "").strip()

    def read(self, path):
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def exists(self, path):
        return Path(path).exists()

    def free_gb(self, path):
        try:
            return shutil.disk_usage(path).free / 1e9
        except OSError:
            return None

    def get(self, url, timeout=6):
        """(HTTP status, None) or (None, why)."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - our own server
                return resp.status, None
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, str(getattr(exc, "reason", exc))

    def wait(self, seconds):
        time.sleep(seconds)


def _json(text):
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def check_python(p):
    v = sys.version_info
    if v < (3, 10):
        return [("Python", PROBLEM, f"{v.major}.{v.minor}; Slot17 needs 3.10 or newer")]
    status, out = p.run(sys.executable, "-c", "import numpy, scipy; print(numpy.__version__, scipy.__version__)")
    if status != 0:
        return [("Python", PROBLEM, f"{v.major}.{v.minor}, but NumPy or SciPy is missing: run the setup again")]
    return [("Python", OK, f"{v.major}.{v.minor}, NumPy and SciPy {out}")]


UNREADABLE = "unreadable"


def receiver_kind(p):
    """The receiver setup chose (p0/receiver.py), the Airspy Mini when nothing is set; UNREADABLE for a file that
    exists but can't be read (the detector refuses it, so this mustn't pass it as the Airspy); None for a kind
    this doctor doesn't know."""
    if not p.exists(RECEIVER_SETTINGS):
        return "airspy"
    data = _json(p.read(RECEIVER_SETTINGS))
    if data is None or not isinstance(data.get("kind"), str):
        return UNREADABLE
    return data["kind"] if data["kind"] in RECEIVERS else None


def check_receiver(p):
    kind = receiver_kind(p)
    if kind == UNREADABLE:
        return [("Receiver", PROBLEM, f"{RECEIVER_SETTINGS} can't be read, and the detector won't start with it: "
                                      "run bash setup.sh car --receiver KIND")]
    if kind is None:
        return [("Receiver", PROBLEM, f"{RECEIVER_SETTINGS} names an unknown receiver: run bash setup.sh car")]
    if kind == "airspy":
        status, _ = p.run("lsusb", "-d", AIRSPY_USB)
        if status is None:
            return [("Receiver", NOTE, "cannot tell: lsusb is missing (sudo apt install usbutils)")]
        if status != 0:
            return [("Receiver", PROBLEM, "no Airspy on USB. Check the cable; if it was plugged in, unplug it, plug it "
                                          "back in and power the computer off and on")]
        return [("Receiver", OK, "Airspy found on USB (not opened)")]
    if kind == "rtlsdr2":
        return check_rtl_pair(p)
    name, usb = RECEIVERS[kind]
    out = []
    status, _ = p.run("lsusb", "-d", usb)
    if status is None:
        out.append(("Receiver", NOTE, "cannot tell: lsusb is missing (sudo apt install usbutils)"))
    elif status != 0:
        out.append(("Receiver", PROBLEM, f"no {name} on USB. Check the cable; if it was plugged in, unplug it and plug "
                                         "it back in"))
    else:
        out.append(("Receiver", OK, f"{name} found on USB (not opened)"))
    # The SDRplay API: SDRplay's installer puts the library in /usr/local/lib and runs its service as sdrplay.service.
    if not any(p.exists(path) for path in SDRPLAY_LIBRARIES):
        out.append(("SDRplay API", PROBLEM, f"not installed: run bash setup.sh car --receiver {kind}"))
    else:
        _, active = p.run("systemctl", "is-active", "sdrplay.service")
        if active == "active":
            out.append(("SDRplay API", OK, "installed; its service runs"))
        else:
            out.append(("SDRplay API", PROBLEM, f"installed, but its service is {active or 'not running'}: "
                                                "sudo systemctl enable --now sdrplay"))
    return out


def check_rtl_pair(p):
    """Both sticks on USB (counted, not opened), librtlsdr installed, and whether the television driver is loaded."""
    out = []
    status, listing = p.run("lsusb")
    if status is None:
        out.append(("Receiver", NOTE, "cannot tell: lsusb is missing (sudo apt install usbutils)"))
    else:
        n = sum(1 for line in (listing or "").lower().splitlines() if any(f"id {u}" in line for u in RTLSDR_USB))
        if n == 2:
            out.append(("Receiver", OK, "both RTL-SDRs of the pair found on USB (not opened)"))
        elif n < 2:
            out.append(("Receiver", PROBLEM, f"{n} of the pair's two RTL-SDRs on USB. Check both cables; two sticks "
                                             "are happier on a powered hub or on the computer's own ports"))
        else:
            out.append(("Receiver", PROBLEM, f"{n} RTL-SDRs on USB; Slot17 uses exactly two: unplug the others"))
    if any(p.exists(path) for path in RTLSDR_LIBRARIES):
        out.append(("librtlsdr", OK, "installed"))
    else:
        out.append(("librtlsdr", PROBLEM, "not installed: run bash setup.sh car --receiver rtlsdr2"))
    if p.exists(DVB_MODULE):
        out.append(("Television driver", NOTE, "loaded; librtlsdr detaches it when the detector opens the sticks, and "
                                               "bash deploy/install-rtlsdr.sh keeps it off after the next power-up"))
    return out


def check_service(p):
    _, enabled = p.run("systemctl", "is-enabled", "tetra-detector.service")
    _, active = p.run("systemctl", "is-active", "tetra-detector.service")
    out = []
    if enabled in ("", "not-found") or not enabled:
        return [("Detector service", PROBLEM, "not installed: run bash setup.sh car")]
    if enabled != "enabled":
        out.append(("Starts at power-up", NOTE, f"no ({enabled}); start it by hand with sudo systemctl start tetra-detector"))
    else:
        out.append(("Starts at power-up", OK, "yes"))
    if active == "active":
        out.append(("Detector service", OK, "running"))
    elif active == "activating":
        out.append(("Detector service", NOTE, "starting: its checks take up to a minute"))
    elif active == "failed":
        out.append(("Detector service", PROBLEM, "its last start failed. Read why: journalctl -u tetra-detector -n 40 "
                                                 "--no-pager. Don't keep restarting it: if the Airspy hangs, remove "
                                                 "power"))
    else:
        out.append(("Detector service", NOTE, f"{active or 'unknown'}: it starts at the next power-up, or with "
                                              "sudo systemctl start tetra-detector"))
    unit = p.read(SERVICE_FILE)
    if unit and "--tests-if-changed" not in unit and "--tests" in unit:
        out.append(("Start-up checks", NOTE, "the full test suite runs at every start (about 30 s). Run the installer "
                                             "again to run it only when the code changed"))
    return out


def check_heartbeat(p):
    first = _json(p.read(HEARTBEAT))
    if first is None:
        return [("Detector", NOTE, "no heartbeat: the detector is not running")]
    p.wait(2.0)
    second = _json(p.read(HEARTBEAT)) or {}
    moved = any(second.get(k) != first.get(k) for k in ("windows", "samples_in", "windows_started"))
    if not moved:
        return [("Detector", PROBLEM, "its heartbeat did not change in 2 s: stuck, or just stopped. "
                                      "journalctl -u tetra-detector -n 40 --no-pager")]
    reasons = [str(r.get("fault")) for r in (second.get("degraded_reasons") or []) if isinstance(r, dict)]
    if second.get("degraded") or reasons:
        return [("Detector", NOTE, "running, degraded: " + (", ".join(reasons) or "see the app's Pi screen")
                 + ". Restarting does not help; see Troubleshooting in the README")]
    if not second.get("ready"):
        return [("Detector", NOTE, "running, not ready yet")]
    return [("Detector", OK, f"running, {second.get('windows', '?')} windows analysed")]


def check_display(p):
    cfg = _json(p.read(DISPLAY_CONFIG))
    if cfg is None:
        return [("Display server", NOTE, "not configured: the phone app gets nothing. bash setup.sh car sets it up")]
    url = str(cfg.get("url", ""))
    base = url.split("/api/")[0].rstrip("/")
    if not base.startswith("http"):
        return [("Display server", PROBLEM, f"{DISPLAY_CONFIG} has no usable address")]
    status, why = p.get(base + "/healthz")
    if status == 200:
        return [("Display server", OK, f"{base} answers")]
    if status is None:
        return [("Display server", PROBLEM, f"{base} cannot be reached ({why}). Is there internet, and is the server's "
                                            "port open in the hosting provider's firewall?")]
    return [("Display server", PROBLEM, f"{base} answered {status}; is it the Slot17 server?")]


def check_network(p):
    status, out = p.run("ip", "route", "show", "default")
    if status is None:
        return []
    if not out:
        return [("Internet", PROBLEM, "no network: is the phone's hotspot on, or the 4G stick connected?")]
    via = out.split(" dev ")[1].split()[0] if " dev " in out else "?"
    return [("Internet", OK, f"a network is up (through {via})")]


def check_clock(p):
    status, out = p.run("timedatectl", "show", "-p", "NTPSynchronized", "--value")
    if status is None:
        return []
    if out == "yes":
        return [("Clock", OK, "set from the internet")]
    return [("Clock", NOTE, "not set from the internet yet. Normal just after power-up (this computer has no clock "
                            "of its own); folder names of this session may be wrong")]


def check_bluetooth(p):
    """The Bluetooth link to the app: switched on here, and an adapter that is powered."""
    settings = _json(p.read(BLUETOOTH_SETTINGS))
    if not settings or settings.get("on") is not True:
        return [("Bluetooth", NOTE, "off (optional: alerts straight to the app, besides the server). "
                                    "bash setup.sh car --bluetooth, or the app's Pi screen, switches it on")]
    name = settings.get("name") if isinstance(settings.get("name"), str) else "Slot17"
    status, out = p.run(sys.executable, "-B", str(ROOT / "p0/bluetooth_link.py"), "adapter", timeout=10)
    adapter = _json(out.splitlines()[-1] if out else "")
    if adapter is None or adapter.get("present") is None:
        why = (adapter or {}).get("error") or "BlueZ did not answer"
        return [("Bluetooth", PROBLEM, f"on, but Bluetooth cannot be used: {why}. Is bluetooth.service running?")]
    if not adapter.get("present"):
        return [("Bluetooth", PROBLEM, "on, but this computer has no Bluetooth adapter that can advertise")]
    if not adapter.get("powered"):
        return [("Bluetooth", PROBLEM, "on, but the adapter is switched off (bluetoothctl power on)")]
    if phones_asked_to_pair(p):
        return [("Bluetooth", NOTE, f"on, shown as {name}, but phones are asked to pair at every connection: "
                                    "run bash setup.sh car --bluetooth once (it switches BlueZ's GATT client off)")]
    return [("Bluetooth", OK, f"on, shown as {name}")]


def phones_asked_to_pair(p):
    """BlueZ still acts as a GATT client (p0/bluetooth_config.py gatt_client). False when it cannot tell."""
    text = p.read(BLUEZ_MAIN_CONF)
    if text is None:
        return False
    sys.path.insert(0, str(ROOT / "p0"))
    try:
        from bluetooth_config import gatt_client
    except Exception:                       # noqa: BLE001 - an older p0: say nothing rather than guess
        return False
    return gatt_client(text)


def check_power(p):
    status, out = p.run("vcgencmd", "get_throttled")
    if status != 0 or "=" not in out:
        return []
    try:
        bits = int(out.split("=")[1], 16)
    except ValueError:
        return []
    if bits & 0x1:
        return [("Power", PROBLEM, "undervoltage right now: use a 5 V 5 A charger and a short, thick cable")]
    if bits & 0x4:
        return [("Power", PROBLEM, "throttled right now: too hot or too little power")]
    if bits & 0x50000:
        return [("Power", NOTE, "undervoltage or throttling happened since power-up; fine now")]
    return [("Power", OK, "no undervoltage or throttling since power-up")]


def storage_kind(p):
    """What the captures are written to, from the device under the project folder: SD card, SSD or USB drive."""
    status, out = p.run("findmnt", "-n", "-o", "SOURCE", "--target", str(ROOT))
    source = out.strip() if status == 0 else ""
    if "mmcblk" in source:
        return "SD card"
    if "nvme" in source:
        return "NVMe SSD"
    if source.startswith("/dev/sd"):
        return "USB drive"
    return None


def check_disk(p):
    free = p.free_gb(ROOT)
    if free is None:
        return []
    kind = storage_kind(p)
    on = f" on the {kind}" if kind else ""
    if free < 5:
        return [("Storage", PROBLEM, f"{free:.1f} GB free{on}: recording has paused. Delete old folders in captures/")]
    # Recordings the storage could not write in time this session (1.5.1 writes them in batches, one sync each).
    recorder = (_json(p.read(HEARTBEAT)) or {}).get("recorder") or {}
    skipped = recorder.get("skipped_data") if isinstance(recorder.get("skipped_data"), int) else 0
    if skipped:
        written = recorder.get("written_data") if isinstance(recorder.get("written_data"), int) else 0
        return [("Storage", NOTE, f"{free:.0f} GB free{on}; {skipped} of {skipped + written} recordings this session "
                                  "were skipped because it could not keep up. Detection is not affected; only those "
                                  "snippets for later study are missing. A faster card (A2, U3) or an SSD keeps them all")]
    if free < 32:
        return [("Storage", NOTE, f"{free:.0f} GB free{on}; recordings grow by several GB per hour of driving")]
    return [("Storage", OK, f"{free:.0f} GB free{on}")]


def check_settings(p):
    out = []
    if p.exists(ROOT / "p0/calibration.local.json"):
        out.append(("Calibration", OK, "this receiver's own (calibration.local.json)"))
    else:
        # calibration.json may be the shipped example or, on an older install, this receiver's own
        # measurement: the doctor cannot tell, so it says what the file says.
        cal = _json(p.read(ROOT / "p0/calibration.json")) or {}
        ppm, when = cal.get("median_ppm"), str(cal.get("measured_utc", ""))[:10]
        said = f"{ppm:+.2f} ppm, measured {when}" if isinstance(ppm, (int, float)) and when else "no measurement in it"
        out.append(("Calibration", NOTE, f"calibration.json ({said}). If that was not measured on this receiver, "
                                         "bash setup.sh car can measure it"))
    if not p.exists(PUSHOVER_CONFIG):
        out.append(("Pushover", NOTE, "not set up (optional; the app can show faults itself)"))
    else:
        switch = _json(p.read(PUSHOVER_SWITCH)) or {}
        off = switch.get("pushover") is False
        out.append(("Pushover", OK, "set up, switched off in the app" if off else "set up and on"))
    out.append(("Signed updates", OK, "on") if p.exists(RELEASE_KEY)
               else ("Signed updates", NOTE, "off: no release key installed"))
    return out


def check_server(p):
    cfg = _json(p.read(SERVER_CONFIG))
    if cfg is None:
        return [("Display server", PROBLEM, f"{SERVER_CONFIG} cannot be read: run bash setup.sh server (or use sudo)")]
    _, active = p.run("systemctl", "is-active", "slot17-display.service")
    out = [("Server service", OK, "running") if active == "active"
           else ("Server service", PROBLEM, f"{active or 'unknown'}: sudo journalctl -u slot17-display -n 40 --no-pager")]
    port = cfg.get("port", 5732)
    status, why = p.get(f"http://127.0.0.1:{port}/healthz")
    if status == 200:
        out.append(("Server answers", OK, f"on port {port} here. If the phone cannot reach it, open port {port} in "
                                          "the hosting provider's firewall"))
    else:
        out.append(("Server answers", PROBLEM, f"not on port {port} here ({why or status})"))
    return out


def diagnose(p, role):
    if role == "server":
        return check_server(p)
    out = []
    for check in (check_python, check_receiver, check_service, check_heartbeat, check_network, check_clock,
                  check_display, check_bluetooth, check_power, check_disk, check_settings):
        try:
            out += check(p)
        except Exception as exc:              # noqa: BLE001 - one broken check must not hide the others
            out.append((check.__name__.replace("check_", ""), NOTE, f"could not check: {type(exc).__name__}"))
    return out


def software_version(p):
    """What runs: p0/version.py (1.2.9 and later). VERSION comes second: signed updates replace p0/ but
    never VERSION, so on a car computer that updates itself VERSION is out of date."""
    found = re.search(r'^SOFTWARE = "([0-9.]+)"', p.read(ROOT / "p0/version.py") or "", re.M)
    if found:
        return found.group(1)
    return (p.read(ROOT / "VERSION") or "").strip() or "unknown"


def guess_role(p):
    return "server" if p.exists(SERVER_CONFIG) and not p.exists(HEARTBEAT.parent) and not p.exists(DISPLAY_CONFIG) else "car"


def main(argv=None, probe=None):
    ap = argparse.ArgumentParser(description="Plain verdicts on this Slot17 machine. Changes nothing.")
    ap.add_argument("role", nargs="?", choices=("car", "server"), help="which machine this is (guessed when left out)")
    ap.add_argument("--json", action="store_true", help="the verdicts as JSON, for the apps (display_publisher.py)")
    args = ap.parse_args(argv)
    p = probe or Probe()
    role = args.role or guess_role(p)
    version = software_version(p)
    if args.json:
        results = diagnose(p, role)
        print(json.dumps({"software": version, "items": [[name, level, text] for name, level, text in results]}))
        return 1 if any(level == PROBLEM for _, level, _ in results) else 0
    print(f"Slot17 doctor, {role}, software {version}. Only looks; changes nothing.\n")
    results = diagnose(p, role)
    width = max((len(name) for name, _, _ in results), default=10)
    for name, level, text in results:
        mark = {OK: "ok     ", NOTE: "note   ", PROBLEM: "PROBLEM"}[level]
        print(f"  {mark}  {name:<{width}}  {text}")
    problems = sum(1 for _, level, _ in results if level == PROBLEM)
    print()
    print(f"{problems} problem(s). The first one is usually the cause of the others." if problems
          else "No problems found.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
