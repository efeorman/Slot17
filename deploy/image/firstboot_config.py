#!/usr/bin/env python3
"""slot17.txt on the image's boot partition: what the person filled in after flashing.

    firstboot_config.py read FILE     the settings as shell-safe KEY=VALUE lines (for firstboot.sh)
    firstboot_config.py forget FILE   rewrite the file without the secrets, once they are saved

The file is edited on Windows or a Mac, so it may have Windows line endings, a byte-order mark, spaces
around "=", quotes around values or a stray "https://" in front of the address. Unknown keys are kept
and ignored. Standard library only.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

KEYS = ("server", "car_key", "hotspot_name", "hotspot_password")
SECRETS = ("car_key", "hotspot_password")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in KEYS:
            out[key] = value
    return out


def forget(text: str) -> str:
    """The same file with the secrets emptied and a note that they are saved on the car computer."""
    lines = []
    for raw in text.lstrip("﻿").replace("\r\n", "\n").split("\n"):
        key = raw.split("=", 1)[0].strip().lower() if "=" in raw and not raw.strip().startswith("#") else ""
        if key in SECRETS and raw.split("=", 1)[1].strip():
            lines.append(f"{key}=")
            lines.append(f"# ({key} was saved on the car computer and removed from this card)")
        else:
            lines.append(raw)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("read", "forget"):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    path = Path(argv[2])
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if argv[1] == "read":
        settings = parse(text)
        for key in KEYS:
            print(f"S17_{key.upper()}={shlex.quote(settings.get(key, ''))}")
    else:
        path.write_text(forget(text), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
