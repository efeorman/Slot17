"""Transactional, bounded notification storage; never stores API credentials."""
import json
import math
import uuid

# Ceiling on how long a spooled message may wait for a network. Five minutes
# was too short for a status message queued while the car's hotspot was down.
MAX_TTL_S = 3600
import os
from pathlib import Path
import sqlite3
import time

ALLOWED = {"message", "title", "priority", "timestamp", "sound", "url", "url_title", "ttl", "html", "monospace"}

try:
    BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
except OSError:
    # Without a shared boot identity, conservatively expire RF on process restart.
    BOOT_ID = str(uuid.uuid4())


def fresh_until(deadline_mono):
    return {'boot_id': BOOT_ID, 'deadline_mono': float(deadline_mono)}


def eligible(fields, expires, now=None, mono=None):
    """RF deadlines survive NTP steps and process restarts within the same boot.

    A reboot invalidates queued RF: a saved monotonic timestamp is then unusable.
    Legacy RF has no trustworthy clock basis and is discarded during migration.
    The API's display TTL is independent of either delivery deadline.
    """
    if '_freshness' in fields:
        f = fields['_freshness']
        try:
            deadline = float(f['deadline_mono'])
            return (f['boot_id'] == BOOT_ID and math.isfinite(deadline)
                    and (time.monotonic() if mono is None else mono) < deadline)
        except (KeyError, TypeError, ValueError):
            return False
    if str(fields.get('title', '')).startswith('TETRA activity - ch '):
        return False
    return (time.time() if now is None else now) < expires


class Outbox:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)")
        self.db.commit()

    def add(self, fields, cap=200):
        fields = {k: v for k, v in fields.items() if k in ALLOWED or k == '_freshness'}
        timestamp = float(fields.get("timestamp", time.time()))
        ttl = min(MAX_TTL_S, max(1, int(fields.get("ttl", 30))))
        fields.update(timestamp=int(timestamp), ttl=ttl)
        expires = timestamp + ttl
        if '_freshness' in fields:
            expires = time.time() + fields['_freshness']['deadline_mono'] - time.monotonic()
        with self.db:
            self.db.execute("INSERT INTO messages(payload, expires) VALUES (?, ?)",
                            (json.dumps(fields, allow_nan=False), expires))
            self.db.execute("DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT ?)", (cap,))

    def expire(self, now):
        # At most 200 small rows. RF eligibility must not use stepped wall time.
        mono = time.monotonic()
        stale = [(rowid,) for rowid, payload, expires in self.db.execute(
            'SELECT id,payload,expires FROM messages')
            if not eligible(json.loads(payload), expires, now, mono)]
        with self.db:
            self.db.executemany('DELETE FROM messages WHERE id=?', stale)
        return len(stale)

    def next(self):
        row = self.db.execute("SELECT id,payload,expires FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def remove(self, rowid):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE id=?", (rowid,))

    def count(self):
        return self.db.execute("SELECT count(*) FROM messages").fetchone()[0]

    def close(self):
        self.db.close()
