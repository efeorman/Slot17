#!/usr/bin/env python3
"""Fails if a Markdown link between tracked documents points nowhere. Offline.

    python scripts/check_links.py
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SKIP = ()


def main():
    files = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout.splitlines()
    broken = []
    checked = 0
    for name in files:
        if name.startswith(SKIP):
            continue
        for target in LINK.findall((ROOT / name).read_text(encoding="utf-8", errors="replace")):
            target = target.split("#", 1)[0]
            if not target or re.match(r"^[a-z]+:", target):
                continue
            checked += 1
            if not (ROOT / name).parent.joinpath(target).exists():
                broken.append(f"{name}: {target}")
    print(f"{checked} links checked, {len(broken)} broken")
    for line in broken:
        print(" ", line)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
