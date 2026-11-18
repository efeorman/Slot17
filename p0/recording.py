"""Bounded asynchronous event and IQ recording.

The analysis thread never waits for disk. Metadata has its own queue, so large
manual IQ captures cannot fill the event queue. Binary outcomes are recorded in
recordings.jsonl; an event referencing queued IQ is not a claim the file exists.
"""
import json
import os
from pathlib import Path
import queue
import shutil
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


class Recorder:
    def __init__(self, directory, max_bytes=MAX_PENDING_BYTES, min_free=MIN_FREE_BYTES,
                 event_capacity=4096):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue(maxsize=event_capacity)
        self.data = queue.Queue(maxsize=256)
        self.max_bytes = max_bytes
        self.min_free = min_free
        self.pending_bytes = 0
        self.event_errors = self.data_errors = self.dropped_events = self.skipped_data = 0
        self.written_events = self.written_data = 0
        self.last_error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="recording", daemon=True)
        self._thread.start()
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

    def snapshot(self):
        return {"pending_bytes": self.pending_bytes, "pending_events": self.events.qsize(),
                "event_errors": self.event_errors, "data_errors": self.data_errors,
                "dropped_events": self.dropped_events, "skipped_data": self.skipped_data,
                "written_events": self.written_events, "written_data": self.written_data,
                "alive": bool(self._thread and self._thread.is_alive()), "last_error": self.last_error}

    def stop(self, timeout=10):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        finished = not self._thread or not self._thread.is_alive()
        return finished and self.events.empty() and self.data.empty()

    def _error(self, error, event=False):
        if event:
            self.event_errors += 1
        else:
            self.data_errors += 1
        self.last_error = f"{type(error).__name__}: {error}"[:240]

    def _run(self):
        try:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as events, \
                 (self.directory / "recordings.jsonl").open("a", encoding="utf-8") as index:
                last_sync = time.monotonic()
                dir_dirty = False
                def drain_events():
                    for _ in range(self.events.maxsize):
                        try:
                            line = self.events.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            events.write(line)
                            self.written_events += 1
                        except OSError as exc:
                            self._error(exc, event=True)
                        finally:
                            self.events.task_done()
                    events.flush()
                while not self._stop.is_set() or not self.events.empty() or not self.data.empty():
                    # Drain currently queued metadata first, bounded for fairness.
                    for _ in range(256):
                        try:
                            line = self.events.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            events.write(line)
                            events.flush()
                            self.written_events += 1
                        except OSError as exc:
                            self._error(exc, event=True)
                        finally:
                            self.events.task_done()
                    try:
                        name, samples, metadata = self.data.get(timeout=0.05)
                    except queue.Empty:
                        name = None
                    if name is not None:
                        outcome = {"name": name, "bytes": samples.nbytes, "metadata": metadata}
                        try:
                            # A producer may have queued metadata while data.get
                            # was waiting. Flush that metadata before large I/O.
                            drain_events()
                            if shutil.disk_usage(self.directory).free - samples.nbytes < self.min_free:
                                outcome["state"] = "low_disk"
                                self.skipped_data += 1
                            else:
                                target = self.directory / name
                                tmp = self.directory / (name + ".tmp")
                                with tmp.open("wb") as fh:
                                    fh.write(memoryview(samples).cast("B"))
                                    fh.flush()
                                    os.fsync(fh.fileno())
                                os.replace(tmp, target)
                                sidecar = self.directory / (name + ".json")
                                with (self.directory / (name + ".json.tmp")).open("w", encoding="utf-8") as fh:
                                    json.dump(metadata, fh, allow_nan=False)
                                    fh.flush()
                                    os.fsync(fh.fileno())
                                os.replace(self.directory / (name + ".json.tmp"), sidecar)
                                outcome["state"] = "written"
                                self.written_data += 1
                                dir_dirty = True
                        except Exception as exc:
                            self._error(exc)
                            outcome.update(state="error", error=self.last_error)
                            for leftover in (name + ".tmp", name + ".json.tmp"):
                                try:
                                    (self.directory / leftover).unlink(missing_ok=True)
                                except OSError:
                                    pass
                        finally:
                            with self._lock:
                                self.pending_bytes -= samples.nbytes
                            self.data.task_done()
                        try:
                            index.write(_index_line(outcome))
                            index.flush()
                        except OSError as exc:
                            self._error(exc, event=True)
                    if time.monotonic() - last_sync >= 1 or self._stop.is_set():
                        for fh in (events, index):
                            try:
                                os.fsync(fh.fileno())
                            except OSError as exc:
                                self._error(exc, event=True)
                        # File contents are fsynced above, but the directory entry
                        # created by os.replace is not, and this rig loses power
                        # at the ignition rather than through a clean shutdown.
                        # Without this a recording can survive as zero bytes or
                        # not at all. Bounded to the same one-second cadence.
                        if dir_dirty and os.name == 'posix':
                            try:
                                fd = os.open(self.directory, os.O_RDONLY)
                                try:
                                    os.fsync(fd)
                                finally:
                                    os.close(fd)
                                dir_dirty = False
                            except OSError as exc:
                                self._error(exc)
                        last_sync = time.monotonic()
        except Exception as exc:
            self._error(exc, event=True)
