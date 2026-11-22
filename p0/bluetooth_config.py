#!/usr/bin/env python3
"""The Bluetooth link's settings on this car computer: on or off, the name it shows, and its key.

    python3 bluetooth_config.py --show             what is set (never the key itself)
    python3 bluetooth_config.py --on [--name N]    switch it on, and make a key if there is none
    python3 bluetooth_config.py --off

`setup.sh car` and the app's switch both write this file; the publisher reads it. It lives in
~/.config, only its owner can read it, and every write replaces it whole, so a power cut leaves the
old settings or the new ones. The key signs every frame (p0/bluetooth_frames.py); the app gets it
over the link to the display server, never over Bluetooth.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys

from bluetooth_frames import KEY_BYTES, key_id

PATH = os.path.expanduser("~/.config/slot17-bluetooth.json")
DEFAULT_NAME = "Slot17"
NAME_MAX = 20
# Letters, digits, spaces, dots, dashes and underscores, starting with a letter or digit: what every phone
# shows the same way. Apple advises against ':' and ';' in a Bluetooth name.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]*")


def valid_name(name) -> bool:
    return isinstance(name, str) and 0 < len(name) <= NAME_MAX and NAME.fullmatch(name) is not None \
        and name == name.strip()


def load(path: str | None = None) -> dict:
    """{"on": bool, "name": str, "key": bytes or None}. A missing or damaged file means off."""
    try:
        with open(path or PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        key = bytes.fromhex(data["key"]) if isinstance(data.get("key"), str) else None
    except ValueError:
        key = None
    if key is not None and len(key) != KEY_BYTES:
        key = None
    return {"on": data.get("on") is True and key is not None,
            "name": data["name"] if valid_name(data.get("name")) else DEFAULT_NAME, "key": key}


def save(path: str | None = None, *, on: bool | None = None, name: str | None = None) -> dict:
    """Change what is given, keep the rest, and make a key the first time. Returns the new settings."""
    path = path or PATH
    current = load(path)
    if name is not None and not valid_name(name):
        raise ValueError(f"not a usable name: 1 to {NAME_MAX} letters, digits, spaces, '.', '-' or '_'")
    key = current["key"] or secrets.token_bytes(KEY_BYTES)
    data = {"on": current["on"] if on is None else bool(on), "name": current["name"] if name is None else name,
            "key": key.hex()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    partial = f"{path}.{os.getpid()}.partial"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(partial, path)
    return load(path)


def public(settings: dict) -> dict:
    """What may be shown or sent anywhere: everything but the key, which is named by its key id."""
    return {"on": settings["on"], "name": settings["name"],
            "key_id": key_id(settings["key"]) if settings["key"] else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The Bluetooth link's settings on this car computer.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true", help="switch the link on (makes a key if there is none)")
    group.add_argument("--off", action="store_true", help="switch the link off")
    ap.add_argument("--name", help=f"the name phones show, 1 to {NAME_MAX} characters (default {DEFAULT_NAME})")
    ap.add_argument("--show", action="store_true", help="print the settings as JSON, without the key")
    ap.add_argument("--path", default=PATH, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        if args.on or args.off or args.name is not None:
            settings = save(args.path, on=True if args.on else False if args.off else None, name=args.name)
        else:
            settings = load(args.path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(public(settings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
