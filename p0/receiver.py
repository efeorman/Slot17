#!/usr/bin/env python3
"""Which receiver this car computer has, and how the detector reaches it.

The receiver is chosen once, by `setup.sh car` from the USB IDs it sees, and kept in
~/.config/slot17-receiver.json. It is never probed at a start: opening a receiver to find
out what it is would be a second session, which the Airspy Mini does not survive (rule 3
in CONTRIBUTING.md). Without that file the receiver is the Airspy Mini, as it was before
receivers could be chosen, so an existing car computer changes nothing.

Only the chosen receiver's module is imported (`backend`), so an Airspy car computer never
loads SDRplay code, and an RSP1B car computer never loads libairspy.

Every backend offers the same small interface, used by watch.py:

    rx = backend(kind).Receiver(kind, fs=..., centre_hz=..., gain=...)
    info = rx.open()          # find and configure the device; returns what it is (model, serial, ...)
    rx.start(sink)            # stream into sink.first(n) / sink.drop(lost) / sink.push(int16 I/Q) / sink.lost(n)
    rx.fault()                # None, or why the stream can no longer be trusted (the device was removed, ...)
    rx.close()                # once, at the end of the session
    rx.gain_record()          # the gain, for session.json
    rx.status()               # counters for the heartbeat (overloads, gaps, ...)

    python receiver.py --show                 # what is configured
    python receiver.py --set rsp1b            # what setup.sh does
    python receiver.py --usb "$(lsusb)"       # which known receivers an `lsusb` listing shows
"""
import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path

SETTINGS = Path(os.environ.get("SLOT17_RECEIVER_SETTINGS", Path.home() / ".config" / "slot17-receiver.json"))
DEFAULT = "airspy"

# kind: the name people see, the module that drives it, its USB IDs (vendor:product), and gain defaults.
KINDS = {
    "airspy": {"name": "Airspy Mini", "module": "receiver_airspy", "usb": ("1d50:60a1",),
               "gain": {"lna": 12, "mixer": 12, "vga": 12}},
    # RSP1B and RSP1A take the same API parameters (SDRplay API 3.15 specification, "for the RSP1B, use RSP1A
    # update and structure parameters"). Gain: LNA state 0 to 9 (0 is the most gain), IF gain reduction 20 to
    # 59 dB. The defaults are a starting point until they are measured with an antenna (docs/design/RECEIVERS.md).
    "rsp1b": {"name": "SDRplay RSP1B", "module": "receiver_sdrplay", "usb": ("1df7:3050",),
              "gain": {"lna_state": 2, "if_gain_reduction_db": 40}},
    "rsp1a": {"name": "SDRplay RSP1A", "module": "receiver_sdrplay", "usb": ("1df7:3000",),
              "gain": {"lna_state": 2, "if_gain_reduction_db": 40}},
    # Two RTL-SDRs, each hearing half of the band (receiver_rtlsdr.py). 0bda:2838 is nearly every current stick
    # (RTL-SDR Blog V3 and V4 included), 0bda:2832 some older ones. Tuner gain in tenths of a dB (the nearest the
    # tuner offers is used), the rate each stick runs at (2560 or 2880 kS/s), and each stick's ppm correction,
    # A being the one whose serial sorts first. The settings file holds them all, as it holds an RSP's gain.
    "rtlsdr2": {"name": "RTL-SDR pair", "module": "receiver_rtlsdr", "usb": ("0bda:2838", "0bda:2832"),
                "devices": 2, "gain": {"tuner_gain_db10": 297, "rate_khz": 2560, "ppm_a": 0, "ppm_b": 0}},
}


class ReceiverError(RuntimeError):
    """The receiver could not be found, opened or configured; the message says what to do."""


def configured(path=None):
    """The kind set up on this car computer; the Airspy Mini when nothing is set.

    A file that exists but can't be read, or names an unknown receiver, is an error rather than a silent
    Airspy: opening the wrong kind of receiver helps nobody.
    """
    path = Path(path or SETTINGS)
    if not path.exists():
        return DEFAULT
    try:
        kind = json.loads(path.read_text(encoding="utf-8")).get("kind")
    except (OSError, ValueError, AttributeError) as exc:
        raise ReceiverError(f"{path} can't be read ({exc}); run: bash setup.sh car --receiver KIND") from exc
    if kind not in KINDS:
        raise ReceiverError(f"{path} names an unknown receiver {kind!r}; known: {', '.join(KINDS)}")
    return kind


def gain_for(kind, path=None):
    """This kind's gain: its defaults, then any "gain" the settings file gives for it.

    The file is where an RSP's gain is tuned, because signed updates can't change the service file:
    {"kind": "rsp1b", "gain": {"lna_state": 3, "if_gain_reduction_db": 38}}. Unknown keys are refused.
    """
    gain = dict(KINDS[kind]["gain"])
    path = Path(path or SETTINGS)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReceiverError(f"{path} can't be read ({exc})") from exc
        extra = data.get("gain") if isinstance(data, dict) and data.get("kind") == kind else None
        if extra is not None:
            if not isinstance(extra, dict) or set(extra) - set(gain) \
                    or not all(isinstance(v, int) and not isinstance(v, bool) for v in extra.values()):
                raise ReceiverError(f"{path}: 'gain' takes whole numbers for {', '.join(gain)} only")
            gain.update(extra)
    return gain


def save(kind, path=None):
    """Write the kind, whole or not at all (setup.sh calls this through --set). A gain tuned for the same kind
    is kept; one for another kind is dropped with it."""
    if kind not in KINDS:
        raise ReceiverError(f"unknown receiver {kind!r}; known: {', '.join(KINDS)}")
    path = Path(path or SETTINGS)
    data = {"kind": kind}
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(old, dict) and old.get("kind") == kind and isinstance(old.get("gain"), dict):
            data["gain"] = old["gain"]
    except (OSError, ValueError):
        pass                                 # an unreadable file is replaced
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, data)
    return path


def _write(path, data):
    """On the disk before it replaces the old file: a power cut leaves one or the other, never half of one."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    if hasattr(os, "O_DIRECTORY"):           # and the rename itself (found in review, 24 November 2026)
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def update_gain(kind, values, path=None):
    """Change some of this kind's tuned values in the settings file (calibrate.py sets an RTL pair's ppm_a and
    ppm_b), keeping the rest; whole or not at all. Only for the kind the file already names."""
    if kind not in KINDS:
        raise ReceiverError(f"unknown receiver {kind!r}; known: {', '.join(KINDS)}")
    known = KINDS[kind]["gain"]
    if set(values) - set(known) or not all(isinstance(v, int) and not isinstance(v, bool) for v in values.values()):
        raise ReceiverError(f"{kind} takes whole numbers for {', '.join(known)} only")
    path = Path(path or SETTINGS)
    if configured(path) != kind:
        raise ReceiverError(f"{path} is set up for {configured(path)}, not {kind}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReceiverError(f"{path} can't be read ({exc})") from exc
    gain = dict(data.get("gain") or {}) if isinstance(data.get("gain"), dict) else {}
    gain.update(values)
    data = {"kind": kind, "gain": gain}
    _write(path, data)
    return gain


def backend(kind):
    """The module that drives this kind; nothing of the other receivers is imported."""
    if kind not in KINDS:
        raise ReceiverError(f"unknown receiver {kind!r}; known: {', '.join(KINDS)}")
    return importlib.import_module(KINDS[kind]["module"])


def name(kind):
    return KINDS.get(kind, {}).get("name", kind)


def short_name(kind):
    """The word in messages: 'Airspy' as it has always been for the Airspy, the full name for the others."""
    return "Airspy" if kind == "airspy" else name(kind)


def usb_devices(sysfs="/sys/bus/usb/devices"):
    """The vendor:product of every device on the USB bus, one entry per device, read from sysfs (nothing needs to
    be installed); None off Linux or when sysfs can't be read."""
    if not sys.platform.startswith("linux") and sysfs == "/sys/bus/usb/devices":
        return None
    found = []
    try:
        entries = list(Path(sysfs).iterdir())
    except OSError:
        return None
    for dev in entries:
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue                         # interfaces and hubs without IDs
        found.append(f"{vid}:{pid}")
    return found


def present(kind, sysfs="/sys/bus/usb/devices"):
    """How many of this kind's devices are on USB (0 when none); None when that can't be read."""
    devices = usb_devices(sysfs)
    if devices is None:
        return None
    return sum(d in KINDS[kind]["usb"] for d in devices)


def kinds_on_usb(listing):
    """The known receivers an `lsusb` listing shows, in the order of KINDS (lines like 'ID 1df7:3050 ...')."""
    ids = {m.lower() for m in re.findall(r"\bID ([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\b", listing or "")}
    return [k for k, v in KINDS.items() if ids & set(v["usb"])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print the configured receiver as JSON")
    g.add_argument("--set", choices=sorted(KINDS), metavar="KIND", help=f"one of {', '.join(KINDS)}")
    g.add_argument("--usb", metavar="LSUSB", help="print the known receivers in this lsusb output, one per line")
    g.add_argument("--present", choices=sorted(KINDS), metavar="KIND",
                   help="exit 0 when every device this kind needs is on USB (exactly two RTL-SDRs for the pair), else 1")
    args = ap.parse_args(argv)
    try:
        if args.present:
            count = present(args.present)
            need = KINDS[args.present].get("devices", 1)
            print(f"{args.present}: {count if count is not None else 'unknown'} of {need} on USB")
            # The pair's receiver takes exactly two sticks and refuses three; until 24 November 2026 three passed
            # here, and the start then failed (found in review).
            return 0 if count is not None and (count == need if need > 1 else count >= need) else 1
        if args.show:
            kind = configured()
            print(json.dumps({"kind": kind, "name": name(kind), "file": str(SETTINGS),
                              "set": SETTINGS.exists()}))
        elif args.set:
            print(f"receiver: {name(args.set)} ({save(args.set)})")
        else:
            for kind in kinds_on_usb(args.usb):
                print(kind)
    except ReceiverError as exc:
        print(f"receiver: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
