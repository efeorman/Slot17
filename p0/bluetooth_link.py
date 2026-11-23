#!/usr/bin/env python3
"""The Bluetooth link's own process: sends the detector's state and matches to the phone apps.

    python3 bluetooth_link.py serve                 run the link (the publisher starts it)
    python3 bluetooth_link.py adapter [--name N]    show this computer's adapter as JSON; rename it

`serve` is started by display/display_publisher.py while Bluetooth is switched on, and runs as long as
its standard input stays open. It reads one JSON object per line:

    {"status": {...}, "channels": [...]}    the detector's state and channel rows, every 2 s
    {"matches": [...]}                      new matches, the moment the publisher sees them
    {"state": "<base64>"}                   everything the server gets, packed (1.6.0), every 2 s
    {"file": {"path": "..."}}               a packed file to send in pieces, then delete (1.6.0)
    {"reload": true}                        the settings changed: switched off, or renamed

and writes one JSON line about itself to standard output when that changes, and every STATUS_EVERY_S.
A phone can also switch the link off or rename it over the link itself (a signed control frame, see
bluetooth_frames.py); the line then says `"settings_changed": true`, so the publisher reads them again.
From 1.6.0 a phone can send any operation it would send the server; the link checks and puts it
together, and hands it to the publisher as `{"op": {...}}`, which carries it out and answers in the
next state. It never touches the receiver, runs at the lowest priority, and stops by itself when the
publisher does. A fault here can never hold up the publisher's pushes to the display server.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

import bluetooth_config
import bluetooth_frames as frames

STATUS_EVERY_S = 5.0
FAST_ADVERTISING_S = 30.0       # Apple: 20 ms for at least 30 s, after start and after a disconnect
FAST_MS = (20, 20)
NORMAL_MS = (152, 153)          # Apple's 152.5 ms, in whole milliseconds
CONNECTED_MS = (1022, 1023)     # Apple's 1022.5 ms while a phone is connected: a second phone can still join
RETRY_S = 30.0                  # after BlueZ refused a registration
# A file goes out a few pieces at a time, so a match never waits behind a whole Pi log: 4 every 50 ms is
# about 14 KB a second, well under what a phone takes, and a 15 KB log is there in about a second.
PUMP_MS = 50
PUMP_FRAMES = 4
FILE_LIMIT = 2 << 20            # a packed file the publisher hands over; a real Pi log packs to 20 to 60 KB
FILE_NAME = re.compile(r"slot17-bt-[0-9a-f]{8,32}\.z")
OP_ID = re.compile(r"[0-9a-f]{32}")


class Link:
    """What the link does, apart from BlueZ: turns commands into frames, and keeps its own state.

    `peripheral` is a bluetooth_bluez.Peripheral, or a stand-in in the tests. `out(dict)` writes a
    status line. States: starting, advertising, connected (a phone is subscribed), error, off.
    """

    def __init__(self, settings, peripheral, out, software="", now=time.monotonic, boot=None,
                 load=bluetooth_config.load, save=bluetooth_config.save):
        self.settings, self.peripheral, self.out, self.software, self.now = settings, peripheral, out, software, now
        self.load, self.save = load, save
        self.used = set()                       # counters a control frame has already named
        self.stop_requested = False             # switched off over Bluetooth: the process ends after its reply
        self.encoder = frames.Encoder(settings["key"], boot if boot is not None else secrets.randbits(32))
        self.state, self.since = "starting", now()
        self.subscribed, self.mtu, self.error = False, None, None
        self.sent_frames = self.sent_matches = 0
        self.advertising_ms, self.fast_until = None, now() + FAST_ADVERTISING_S
        self.retry = None                       # ("gatt" or "advertising", when it failed)
        self.reported, self.last_report = None, -1e9
        self.outbox = deque()                   # file pieces still to send, a few at a time (pump)
        self.wake = lambda: None                # serve() makes this start the pump
        self.op_parts = frames.Assembler(keep=4, limit=frames.OP_MAX_BYTES)
        self.cache = Path.home() / ".cache"     # where the publisher leaves files to send

    def info(self) -> bytes:
        """What the phone reads first: which car computer this is, the software it runs, and what it can do."""
        return json.dumps({"v": frames.VERSION, "name": self.settings["name"],
                           "key_id": frames.key_id(self.settings["key"]), "software": self.software,
                           "boot": f"{self.encoder.boot:08x}", "can": frames.CAN}, separators=(",", ":")).encode()

    # --- events from BlueZ -------------------------------------------------------------------------
    def event(self, kind, value):
        if kind == "registered":
            self._advertise()
        elif kind == "advertising":
            self.error = None
            if self.state in ("starting", "error"):
                self._set("connected" if self.subscribed else "advertising")
        elif kind in ("gatt_failed", "advertising_failed"):
            self.error = f"{'GATT' if kind == 'gatt_failed' else 'advertising'}: {value}"[:120]
            self.retry = ("gatt" if kind == "gatt_failed" else "advertising", self.now())
            self.advertising_ms = None
            self._set("error")
        elif kind == "subscribed":
            self.subscribed = bool(value)
            if not self.subscribed:
                self.fast_until = self.now() + FAST_ADVERTISING_S
                self.outbox.clear()             # the phone asks again; half a file is no use to it
            if self.state != "error":
                self._set("connected" if self.subscribed else "advertising")
                self._advertise()
        elif kind == "read":
            self.mtu = value
        elif kind == "control":
            self.control(value)
        self.report()

    def control(self, data):
        """A phone's signed command: switch the link off, or rename it. Answered with a reply frame."""
        try:
            c = frames.decode_control(data, self.settings["key"])
        except frames.FrameError as exc:
            return self._reply(0, False, f"refused: {exc}")
        action = {"rename": frames.RENAME, "off": frames.OFF, "op": frames.OP}[c["action"]]
        if c["boot"] != self.encoder.boot or not 0 <= self.encoder.counter - c["counter"] <= frames.CONTROL_WINDOW:
            return self._reply(action, False, "refused: too old, or from before a restart")
        if action == frames.OP:
            return self._operation(c)
        if c["counter"] in self.used:
            return self._reply(action, False, "refused: already used")
        self.used = {n for n in self.used if self.encoder.counter - n <= frames.CONTROL_WINDOW} | {c["counter"]}
        try:
            if action == frames.OFF:
                self.save(on=False)
            else:
                self.save(name=c["name"])
        except (OSError, ValueError) as exc:
            return self._reply(action, False, str(exc)[:frames.REPLY_TEXT_BYTES])
        if action == frames.OFF:
            self.stop_requested = True
            self._reply(action, True, "Bluetooth off")
        else:
            self.reload()
            self._reply(action, True, f"Bluetooth name: {c['name']}")
        self.out(dict(self.status(), settings_changed=True))

    def _operation(self, c):
        """A piece of a phone's operation. Once it is whole and looks like one, it goes to the publisher.

        Its pieces may name the same counter, so the counter is not used up here: the publisher carries an
        operation out once by its id, and the boot and counter window still stop one played back later.
        """
        try:
            whole = self.op_parts.add(c["message"], c["index"], c["count"], c["data"])
        except frames.FrameError as exc:
            return self._reply(frames.OP, False, f"refused: {exc}")
        if whole is None:
            return None
        try:
            op = json.loads(whole.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(frames.OP, False, "refused: not JSON")
        if not isinstance(op, dict) or not isinstance(op.get("kind"), str) or not OP_ID.fullmatch(str(op.get("id"))):
            return self._reply(frames.OP, False, "refused: not an operation")
        self.out({"op": op})
        return None

    def _reply(self, action, ok, text):
        if self.subscribed:
            self._send([self.encoder.reply(action, ok, text)], 0)

    def lost(self):
        """bluetoothd stopped: the phone is gone and nothing is registered until it comes back."""
        self.subscribed, self.advertising_ms, self.retry = False, None, None
        self.error = "bluetoothd stopped"
        self._set("starting")
        self.report()

    def restart(self):
        """bluetoothd came back after a restart, and has forgotten the service and the advertisement."""
        self.subscribed, self.advertising_ms, self.retry, self.error = False, None, None, None
        self.fast_until = self.now() + FAST_ADVERTISING_S
        self._set("starting")
        self.peripheral.restart()
        self.report()

    # --- commands from the publisher ---------------------------------------------------------------
    def command(self, obj) -> bool:
        """Carry out one command. Returns False when the link was switched off and should end."""
        if not isinstance(obj, dict):
            return True
        if obj.get("reload") and not self.reload():
            return False
        if isinstance(obj.get("matches"), list) and obj["matches"] and self.subscribed:
            self._send(self.encoder.matches(obj["matches"]), len(obj["matches"]))
        if isinstance(obj.get("status"), dict) and self.subscribed:
            sent = [self.encoder.status(obj["status"])]
            if isinstance(obj.get("channels"), list):
                sent.append(self.encoder.channels(obj["channels"]))
            self._send(sent, 0)
        if isinstance(obj.get("state"), str) and self.subscribed:
            try:
                packed = base64.b64decode(obj["state"], validate=True)
            except (binascii.Error, ValueError):
                packed = None
            if packed:
                self._send(self.encoder.pieces(frames.STATE, packed), 0)
        if isinstance(obj.get("file"), dict):
            self._file(obj["file"].get("path"))
        return True

    def _file(self, path):
        """A packed file from the publisher, sent in pieces by the pump. Only its own files in its cache."""
        if not isinstance(path, str):
            return
        p = Path(path)
        if p.parent != self.cache or not FILE_NAME.fullmatch(p.name):
            return
        try:
            data = p.read_bytes()[:FILE_LIMIT + 1]
        except OSError:
            return
        finally:
            try:
                p.unlink()
            except OSError:
                pass
        if not self.subscribed or not data or len(data) > FILE_LIMIT:
            return
        message, slices = self.encoder.slices(data)
        self.outbox.extend((message, i, len(slices), s) for i, s in enumerate(slices))
        self.wake()

    def pump(self, n=PUMP_FRAMES) -> bool:
        """Send up to `n` waiting file pieces, each signed as it goes. True while more are waiting."""
        for _ in range(n):
            if not self.outbox:
                break
            if not self.subscribed:
                self.outbox.clear()
                break
            message, index, count, data = self.outbox.popleft()
            self._send([self.encoder.piece(frames.FILE, message, index, count, data)], 0)
        return bool(self.outbox)

    def reload(self) -> bool:
        """Read the settings again. Returns False when the link was switched off."""
        fresh = self.load()
        if not fresh["on"]:
            return False
        if fresh["name"] != self.settings["name"]:
            self.settings = dict(self.settings, name=fresh["name"])
            self.peripheral.rename(fresh["name"])
            self.advertising_ms = None
            self._advertise()
            self.report()
        return True

    def _send(self, frame_list, matches):
        delivered = sum(1 for f in frame_list if self.peripheral.notify(f))
        self.sent_frames += delivered
        if delivered:
            self.sent_matches += matches

    # --- timing ------------------------------------------------------------------------------------
    def tick(self):
        """Once a second: retry what BlueZ refused, slow the advertising down when due, report."""
        if self.state == "error" and self.retry and self.now() - self.retry[1] >= RETRY_S:
            part, self.retry = self.retry[0], None
            self._set("starting")
            if part == "gatt":
                self.peripheral.register()
            else:
                self._advertise()
        elif self.state in ("advertising", "connected"):
            self._advertise()
        self.report()

    def _advertise(self):
        wanted = CONNECTED_MS if self.subscribed else FAST_MS if self.now() < self.fast_until else NORMAL_MS
        if wanted != self.advertising_ms:
            self.advertising_ms = wanted
            self.peripheral.advertise(self.settings["name"], wanted)

    def _set(self, state):
        if state != self.state:
            self.state, self.since = state, self.now()

    def status(self) -> dict:
        return {"state": self.state, "since": round(self.since, 3), "name": self.settings["name"],
                "key_id": frames.key_id(self.settings["key"]), "subscribed": self.subscribed,
                "frames": self.sent_frames, "matches": self.sent_matches, "mtu": self.mtu, "error": self.error}

    def report(self, force=False):
        """A status line when something other than the counters changed, and every STATUS_EVERY_S."""
        now, current = self.now(), self.status()
        steady = {k: v for k, v in current.items() if k not in ("frames", "matches")}
        if force or steady != self.reported or now - self.last_report >= STATUS_EVERY_S:
            self.reported, self.last_report = steady, now
            self.out(current)


def _write_line(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _log(message):
    print(f"bluetooth: {message}", file=sys.stderr, flush=True)


def _lowest_priority():
    try:
        os.nice(19 - os.nice(0))
    except (AttributeError, OSError):
        pass                                    # not on Linux, or not allowed: run as it is


def serve() -> int:
    settings = bluetooth_config.load()
    if not settings["on"]:
        _log("switched off; not starting")
        return 0
    _lowest_priority()
    try:
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib

        import bluetooth_bluez as bluez
    except ImportError as exc:
        _write_line({"state": "unavailable", "error": f"missing {exc.name}: sudo apt install python3-dbus python3-gi"})
        return 3
    try:
        import version
        software = str(version.SOFTWARE)
    except Exception:                           # noqa: BLE001 - an older p0: the version is only shown
        software = ""
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    try:
        adapter_path, adapter = bluez.find_adapter(bus)
    except dbus.exceptions.DBusException as exc:
        _write_line({"state": "unavailable", "error": f"BlueZ is not running ({exc.get_dbus_name()})"})
        return 3
    if adapter_path is None:
        _write_line({"state": "unavailable", "error": "no Bluetooth adapter that can advertise"})
        return 3
    if not adapter["powered"]:
        _write_line({"state": "unavailable", "error": "the Bluetooth adapter is switched off"})
        return 3

    loop = GLib.MainLoop()
    holder = {}
    def events(kind, value):
        holder["link"].event(kind, value)
        if holder["link"].stop_requested:
            GLib.timeout_add(500, loop.quit)    # time for the reply to go out first
            holder["link"].stop_requested = False
    peripheral = bluez.Peripheral(bus, adapter_path, frames.SERVICE_UUID, frames.FRAMES_UUID, frames.INFO_UUID,
                                  info=lambda: holder["link"].info(), events=events, control_uuid=frames.CONTROL_UUID)
    link = holder["link"] = Link(settings, peripheral, _write_line, software=software)
    if adapter["alias"] != settings["name"]:
        try:
            peripheral.rename(settings["name"])
        except dbus.exceptions.DBusException as exc:
            _log(f"could not rename the adapter: {exc.get_dbus_message()}")
    _log(f"starting on {adapter['address']} as {settings['name']!r}")
    link.report(force=True)

    owner = {"name": None}

    def owner_changed(name):
        if name and owner["name"] is None:
            peripheral.register()               # bluetoothd is there: the first registration
        elif name and owner["name"] == "":
            _log("bluetoothd is back; registering again")
            link.restart()
        elif not name and owner["name"]:
            _log("bluetoothd stopped")
            link.lost()
        owner["name"] = name
    bus.watch_name_owner(bluez.BLUEZ, owner_changed)

    pending = b""

    def on_input(fd, condition):
        nonlocal pending
        data = os.read(fd, 65536) if condition & GLib.IO_IN else b""
        if not data:
            loop.quit()                         # the publisher stopped
            return False
        pending += data
        *lines, pending = pending.split(b"\n")
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not link.command(obj):
                _log("switched off")
                loop.quit()
                return False
        return True

    pumping = {"on": False}

    def pump():
        more = link.pump(PUMP_FRAMES)
        pumping["on"] = more
        return more                             # False ends this timer until the next file

    def wake():
        if not pumping["on"]:
            pumping["on"] = True
            GLib.timeout_add(PUMP_MS, pump)
    link.wake = wake

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_input)
    GLib.timeout_add(1000, lambda: (link.tick(), True)[1])
    try:
        loop.run()
    finally:
        peripheral.close()
        link._set("off")
        link.report(force=True)
    return 0


def adapter(name=None) -> int:
    """For setup and the doctor: is there an adapter, and what is it called. Renames it when asked."""
    if name is not None and not bluetooth_config.valid_name(name):
        print(json.dumps({"present": None, "error": "not a usable name"}))
        return 2
    try:
        import dbus

        import bluetooth_bluez as bluez
        bus = dbus.SystemBus()
        path, props = bluez.find_adapter(bus)
        if path is not None and name is not None:
            bluez.set_alias(bus, path, name)
            # BlueZ takes the new name once the controller confirms it, a moment after the call returns.
            for _ in range(20):
                path, props = bluez.find_adapter(bus)
                if props is None or props["alias"] == name:
                    break
                time.sleep(0.1)
    except ImportError as exc:
        print(json.dumps({"present": None, "error": f"missing {exc.name}"}))
        return 3
    except Exception as exc:                    # noqa: BLE001 - BlueZ absent or refusing: say so, as JSON
        print(json.dumps({"present": None, "error": f"{type(exc).__name__}: {exc}"[:160]}))
        return 3
    print(json.dumps({"present": path is not None, **(props or {})}))
    return 0 if path is not None else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The Bluetooth link to the phone apps.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the link; started by the publisher")
    show = sub.add_parser("adapter", help="show the Bluetooth adapter as JSON")
    show.add_argument("--name", help="rename it, as phones will show it")
    args = ap.parse_args(argv)
    return serve() if args.command == "serve" else adapter(args.name)


if __name__ == "__main__":
    sys.exit(main())
