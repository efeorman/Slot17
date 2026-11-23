"""Bounded asynchronous event and IQ recording.

The analysis thread never waits for disk. Events and recordings have their own
queues and, since 1.5.1, their own writer threads, so a slow card delays neither
a match reaching the phone nor the detector. Binary outcomes are recorded in
recordings.jsonl; an event referencing queued IQ is not a claim the file exists.
"""
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time

import numpy as np

MIN_FREE_BYTES = 5 * (1 << 30)
MAX_PENDING_BYTES = 256 * (1 << 20)


def atomic_json(path, value, durable=True):
    """Replace a complete JSON snapshot; failed writes retain the previous file."""
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as fh:
        json.dump(value, fh, allow_nan=False)
        fh.flush()
        if durable:
            os.fsync(fh.fileno())
    os.replace(temp, path)
    if durable and os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _index_line(outcome):
    """One line of strict JSON. Metadata that JSON cannot carry is left out, never the outcome."""
    try:
        return json.dumps(outcome, allow_nan=False) + "\n"
    except (TypeError, ValueError):
        kept = {key: value for key, value in outcome.items() if key != "metadata"}
        return json.dumps({**kept, "metadata_error": "not valid JSON"}, allow_nan=False) + "\n"


SYNC_EVERY_S = 1.0          # events.jsonl, recordings.jsonl and new directory entries reach storage this often
DATA_CAPACITY = 4096        # recordings waiting to be written; MAX_PENDING_BYTES limits their size as well
BATCH_MAX_ITEMS = 512       # recordings made durable together by one sync
BATCH_MAX_BYTES = 64 << 20


def _filesystem_sync():
    """syncfs(2) on Linux, or None.

    One call makes everything written on that file system durable, with one journal commit. ext4 commits its
    journal on every fsync, and on an SD card each commit costs milliseconds to tenths of a second; two per
    recording (the file and its sidecar) is what made a busy band outrun a card (1.5.1). Elsewhere each file
    is fsynced as before.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.syncfs
        call.argtypes, call.restype = [ctypes.c_int], ctypes.c_int
    except (OSError, AttributeError):
        return None

    def syncfs(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            if call(fd) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
        finally:
            os.close(fd)
    return syncfs


SYNCFS = _filesystem_sync()


class Recorder:
    """Two writers, so the alert path never waits for the recordings.

    events.jsonl, which the display publisher reads for matches, has its own thread: written and flushed per
    line, fsynced once a second. Recordings have the other: everything queued is written as `.tmp` files,
    made durable together (one syncfs on Linux, one fsync per file elsewhere), then renamed into place and
    recorded in recordings.jsonl. A recording is never renamed before its data is on storage, so a power cut
    leaves either the complete file or a `.tmp` that no index line names.
    """

    def __init__(self, directory, max_bytes=MAX_PENDING_BYTES, min_free=MIN_FREE_BYTES,
                 event_capacity=4096, sync=SYNCFS):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue(maxsize=event_capacity)
        self.data = queue.Queue(maxsize=DATA_CAPACITY)
        self.max_bytes = max_bytes
        self.min_free = min_free
        self.sync = sync
        self.pending_bytes = 0
        self.event_errors = self.data_errors = self.dropped_events = self.skipped_data = 0
        self.written_events = self.written_data = 0
        self.batches = self.largest_batch = self.syncs = 0
        self.slowest_batch_s = 0.0
        self._events_unsynced = False
        self.last_error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        self._threads = [threading.Thread(target=self._events_loop, name="recording-events", daemon=True),
                         threading.Thread(target=self._data_loop, name="recording-data", daemon=True)]
        for thread in self._threads:
            thread.start()
        return self

    def event(self, event):
        # Serialize before queueing to freeze metadata against later mutation.
        try:
            if self._stop.is_set():
                self.dropped_events += 1
                return False
            self.events.put_nowait(json.dumps(event, allow_nan=False) + "\n")
            return True
        except (queue.Full, TypeError, ValueError):
            self.dropped_events += 1
            return False

    def samples(self, name, samples, metadata):
        if Path(name).name != name or not name or name in {".", ".."}:
            raise ValueError("recording name must be a plain filename")
        samples = np.ascontiguousarray(samples)
        size = samples.nbytes
        with self._lock:
            if size > self.max_bytes - self.pending_bytes or self._stop.is_set():
                self.skipped_data += 1
                return "queue_full"
            self.pending_bytes += size
        try:
            self.data.put_nowait((name, samples, dict(metadata)))
            return "queued"
        except queue.Full:
            with self._lock:
                self.pending_bytes -= size
            self.skipped_data += 1
            return "queue_full"

    def alive(self):
        return bool(self._threads) and all(t.is_alive() for t in self._threads)

    def snapshot(self):
        return {"pending_bytes": self.pending_bytes, "pending_events": self.events.qsize(),
                "pending_recordings": self.data.qsize(),
                "event_errors": self.event_errors, "data_errors": self.data_errors,
                "dropped_events": self.dropped_events, "skipped_data": self.skipped_data,
                "written_events": self.written_events, "written_data": self.written_data,
                "batches": self.batches, "largest_batch": self.largest_batch, "syncs": self.syncs,
                "slowest_batch_s": round(self.slowest_batch_s, 3),
                "durable": "syncfs" if self.sync is not None else "fsync",
                "alive": self.alive(), "last_error": self.last_error}

    def stop(self, timeout=10):
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        finished = not any(t.is_alive() for t in self._threads)
        return finished and self.events.empty() and self.data.empty()

    def _error(self, error, event=False):
        if event:
            self.event_errors += 1
        else:
            self.data_errors += 1
        self.last_error = f"{type(error).__name__}: {error}"[:240]

    # events.jsonl

    def _events_loop(self):
        try:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as events:
                last_sync = time.monotonic()
                while not self._stop.is_set() or not self.events.empty():
                    try:
                        line = self.events.get(timeout=0.05)
                    except queue.Empty:
                        line = None
                    if line is not None:
                        self._write_event(events, line)
                        for _ in range(255):
                            try:
                                self._write_event(events, self.events.get_nowait())
                            except queue.Empty:
                                break
                        self._events_unsynced = True
                    # With syncfs the recordings thread makes events.jsonl durable too, once a second, so this
                    # thread never waits for a sync; a slow card's commit cannot hold up the next match.
                    if self.sync is None and time.monotonic() - last_sync >= SYNC_EVERY_S:
                        self._fsync(events, event=True)
                        last_sync = time.monotonic()
                self._fsync(events, event=True)
        except Exception as exc:
            self._error(exc, event=True)

    def _write_event(self, events, line):
        try:
            events.write(line)
            events.flush()
            self.written_events += 1
        except OSError as exc:
            self._error(exc, event=True)
        finally:
            self.events.task_done()

    def _fsync(self, fh, event=False):
        try:
            os.fsync(fh.fileno())
        except OSError as exc:
            self._error(exc, event=event)

    # recordings

    def _data_loop(self):
        try:
            with (self.directory / "recordings.jsonl").open("a", encoding="utf-8") as index:
                last_sync, dirty = time.monotonic(), False
                while not self._stop.is_set() or not self.data.empty():
                    batch = self._take_batch()
                    if batch:
                        dirty = self._write_batch(batch, index) or dirty
                    unsynced_events = self.sync is not None and self._events_unsynced
                    if (dirty or unsynced_events) and time.monotonic() - last_sync >= SYNC_EVERY_S:
                        self._settle(index)
                        last_sync, dirty = time.monotonic(), False
                if dirty or (self.sync is not None and self._events_unsynced):
                    self._settle(index)
        except Exception as exc:
            self._error(exc, event=True)

    def _take_batch(self):
        try:
            first = self.data.get(timeout=0.05)
        except queue.Empty:
            return []
        batch, size = [first], first[1].nbytes
        while len(batch) < BATCH_MAX_ITEMS and size < BATCH_MAX_BYTES:
            try:
                item = self.data.get_nowait()
            except queue.Empty:
                break
            batch.append(item)
            size += item[1].nbytes
        return batch

    def _write_batch(self, batch, index):
        """Write a batch, make it durable, rename it into place. True when anything new needs settling."""
        started = time.monotonic()
        outcomes, staged = [], []
        try:
            try:
                free = shutil.disk_usage(self.directory).free
            except OSError as exc:
                free, problem = None, exc
            for name, samples, metadata in batch:
                outcome = {"name": name, "bytes": samples.nbytes, "metadata": metadata}
                outcomes.append(outcome)
                if free is None:
                    self._fail(outcome, problem)
                    continue
                if free - samples.nbytes < self.min_free:
                    outcome["state"] = "low_disk"
                    self.skipped_data += 1
                    continue
                try:
                    self._write_tmp(name, samples, metadata, fsync=self.sync is None)
                    free -= samples.nbytes
                    staged.append(outcome)
                except Exception as exc:
                    self._fail(outcome, exc)
            if staged and self.sync is not None:
                try:
                    self.sync(self.directory)
                    self.syncs += 1
                except Exception as exc:
                    for outcome in staged:
                        self._fail(outcome, exc)
                    staged = []
            for outcome in staged:
                name = outcome["name"]
                try:
                    os.replace(self.directory / (name + ".tmp"), self.directory / name)
                    os.replace(self.directory / (name + ".json.tmp"), self.directory / (name + ".json"))
                    outcome["state"] = "written"
                    self.written_data += 1
                except Exception as exc:
                    self._fail(outcome, exc)
        finally:
            with self._lock:
                self.pending_bytes -= sum(samples.nbytes for _, samples, _ in batch)
            for _ in batch:
                self.data.task_done()
        try:
            for outcome in outcomes:
                index.write(_index_line(outcome))
            index.flush()
        except OSError as exc:
            self._error(exc, event=True)
        self.batches += 1
        self.largest_batch = max(self.largest_batch, len(batch))
        self.slowest_batch_s = max(self.slowest_batch_s, time.monotonic() - started)
        return True

    def _write_tmp(self, name, samples, metadata, fsync):
        with (self.directory / (name + ".tmp")).open("wb") as fh:
            fh.write(memoryview(samples).cast("B"))
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        with (self.directory / (name + ".json.tmp")).open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, allow_nan=False)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())

    def _fail(self, outcome, exc):
        self._error(exc)
        outcome.update(state="error", error=self.last_error)
        for leftover in (outcome["name"] + ".tmp", outcome["name"] + ".json.tmp"):
            try:
                (self.directory / leftover).unlink(missing_ok=True)
            except OSError:
                pass

    def _settle(self, index):
        """recordings.jsonl, the renamed directory entries and, with syncfs, events.jsonl to storage. This rig
        loses power at the ignition, not through a clean shutdown: without it a recording can survive as zero
        bytes or not at all."""
        if self.sync is not None:
            try:
                self._events_unsynced = False      # cleared first: a line written during the sync waits for the next
                self.sync(self.directory)
                self.syncs += 1
                return
            except Exception as exc:
                self._events_unsynced = True       # tried again in a second
                self._error(exc)
        self._fsync(index, event=True)
        if os.name == "posix":
            try:
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                self._error(exc)
