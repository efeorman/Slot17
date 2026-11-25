#!/usr/bin/env python3
"""Keep a ZTE 4G USB stick's data session up, rank its route, and say how good the link is.

Tested with a ZTE MF833N on a Raspberry Pi 5. The stick needs the separate USB
fix described in docs/operations/MOBILE-MODEM.md before Linux can use it at
all; this script handles what is left after that. Every 30 seconds it:

1. Keeps data roaming switched on. With a SIM whose network counts as roaming
   (Lycamobile in the Netherlands reports "Internal" roaming), the stick will
   not open a data session with roaming off.
2. Opens the data session when it is down, asks again if it stays down, and if
   it stays down for minutes asks the stick to restart itself. On 14 November
   2026 the stick sat in "connecting" for seven minutes and only a replug by hand
   brought it back; the restart does the same without hands.
3. Ranks the route: wired network first, the stick second, Wi-Fi (for example a
   phone hotspot) third. While the stick has no data session its route is moved
   behind Wi-Fi, so a hotspot takes over instead of traffic going nowhere.
4. Logs what the link is like: network, band, cell, signal (RSRP, RSRQ, SNR),
   and the round trip to the display server in milliseconds. A line every five
   minutes, and at once when the cell changes, the session drops or comes back,
   or packets are lost.

Run as root from slot17-modem.timer. It does nothing when the stick is absent.
Lines start with "modem:" so `journalctl -u slot17-modem.service | grep modem:` shows them.

Settings (environment):
  SLOT17_MODEM_PROFILE      NetworkManager connection name     (zte-mf79n)
  SLOT17_MODEM_METRIC_UP    route metric with a data session   (200)
  SLOT17_MODEM_METRIC_DOWN  route metric without one           (700)
  SLOT17_MODEM_ROAMING      "on" keeps roaming on; "leave" never touches it (on)
  SLOT17_MODEM_PING_HOST    host for the round-trip measurement (the display server)
NetworkManager's defaults are 100 for wired and 600 for Wi-Fi.
"""
import argparse
import glob
import http.client
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

PROFILE = os.environ.get("SLOT17_MODEM_PROFILE", "zte-mf79n")
METRIC_UP = int(os.environ.get("SLOT17_MODEM_METRIC_UP", "200"))
METRIC_DOWN = int(os.environ.get("SLOT17_MODEM_METRIC_DOWN", "700"))
ROAMING = os.environ.get("SLOT17_MODEM_ROAMING", "on").strip().lower()
CONNECT_WAIT_S = 20.0
STATE_FILE = os.environ.get("SLOT17_MODEM_STATE", "/run/slot17-modem.json")   # tmpfs: starts empty at every boot
REPORT_EVERY_S = 300.0       # a status line at least this often while the session is up
REDIAL_AFTER_S = 120.0       # down this long: drop and redial, as the stick's own page would
RESTART_AFTER_S = 300.0      # down this long: ask the stick to restart itself, like a replug
RESTART_GAP_S = 900.0        # and not more often than this
STATUS_FIELDS = ("ppp_status,network_type,signalbar,lte_rsrp,lte_rsrq,lte_snr,cell_id,lte_pci,lte_band,"
                 "wan_lte_ca,roam_setting_option,simcard_roam,network_provider,modem_main_state,"
                 "realtime_time,ppp_dial_conn_fail_counter")


def nmcli(*args):
    return subprocess.check_output(("nmcli",) + args, text=True, timeout=30).strip()


def active_device():
    """Interface that carries the modem profile right now, or None."""
    for line in nmcli("-t", "-f", "NAME,DEVICE", "connection", "show", "--active").splitlines():
        name, _, device = line.rpartition(":")
        if name == PROFILE and device:
            return device
    return None


def request(gateway, source, method, path, body=None):
    """One HTTP request to the stick, sent from the stick's own interface."""
    connection = http.client.HTTPConnection(gateway, 80, timeout=6, source_address=(source, 0))
    try:
        headers = {"Referer": "http://%s/index.html" % gateway}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=body, headers=headers)
        return json.loads(connection.getresponse().read().decode("utf-8", "replace"))
    finally:
        connection.close()


def command(gateway, source, goform_id, extra=""):
    """The same request the stick's own settings page sends for this action."""
    return request(gateway, source, "POST", "/goform/goform_set_cmd_process",
                   "isTest=false&notCallback=true&goformId=%s%s" % (goform_id, extra))


def status(gateway, source):
    reply = request(gateway, source, "GET",
                    "/goform/goform_get_cmd_process?isTest=false&multi_data=1&cmd=" + STATUS_FIELDS)
    return reply if isinstance(reply, dict) else {}


def session_state(gateway, source):
    return str(status(gateway, source).get("ppp_status", ""))


def is_connected(state):
    # The stick reports ppp_connected, or ipv6_connected / ipv4_ipv6_connected.
    return state.endswith("_connected")


def describe(info):
    """One short line about the radio link, from the stick's status page. Empty if it says nothing."""
    if not isinstance(info, dict) or not info:
        return ""
    parts = [str(info.get("network_type") or "network not reported")[:24]]
    band = str(info.get("lte_band") or "").upper().replace("LTE BAND", "").replace("BAND", "").strip().lstrip("B")[:8]
    if band:
        parts.append("band " + band)
    bars = str(info.get("signalbar") or "")[:4]
    if bars:
        parts.append("%s bars" % bars)
    for key, label, unit in (("lte_rsrp", "RSRP", " dBm"), ("lte_rsrq", "RSRQ", " dB"), ("lte_snr", "SNR", " dB")):
        value = str(info.get(key) or "")[:8]
        if value:
            parts.append("%s %s%s" % (label, value, unit))
    cell = str(info.get("cell_id") or "")[:12]
    if cell:
        parts.append("cell %s/%s" % (cell, str(info.get("lte_pci") or "-")[:6]))
    state = str(info.get("modem_main_state") or "")[:32]
    if state and state != "modem_init_complete":
        parts.append(state)
    return ", ".join(parts)


def reception(gateway, source):
    try:
        return describe(status(gateway, source))
    except (OSError, ValueError, http.client.HTTPException):
        return ""


def ensure_roaming(gateway, source, info):
    """Keep data roaming on, as the operator decided. Returns True when it had to be switched on."""
    if ROAMING != "on" or not isinstance(info, dict) or str(info.get("roam_setting_option", "")) in ("on", ""):
        return False
    command(gateway, source, "SET_CONNECTION_MODE", "&ConnectionMode=auto_dial&roam_setting_option=on")
    print("modem: data roaming was off; switched it on (operator's setting)", flush=True)
    return True


def ensure_session(gateway, source):
    """Return True when the stick has a mobile data session."""
    state = session_state(gateway, source)
    if is_connected(state):
        return True
    if state == "ppp_disconnected":
        command(gateway, source, "CONNECT_NETWORK")
        print("modem: data session was down, asked the stick to connect", flush=True)
    started = time.monotonic()
    deadline = started + CONNECT_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(2)
        state = session_state(gateway, source)
        if is_connected(state):
            print("modem: data session up after %.0f s (%s)"
                  % (time.monotonic() - started, reception(gateway, source) or "reception not reported"), flush=True)
            return True
    print("modem: no data session (%s; %s)"
          % (state or "no answer", reception(gateway, source) or "reception not reported"), flush=True)
    return False


def recover(gateway, source, state, now):
    """Escalate while the session stays down: redial, then a restart of the stick itself.

    `state` carries `down_since`, `redialed` and `restarted` (monotonic seconds) between runs.
    Returns what was done, or None.
    """
    down = now - state.get("down_since", now)
    if down >= RESTART_AFTER_S and now - state.get("restarted", -1e9) >= RESTART_GAP_S:
        state["restarted"] = state["redialed"] = now
        print("modem: no data session for %.0f s; asked the stick to restart itself "
              "(it leaves USB for about 20 s, as after a replug)" % down, flush=True)
        command(gateway, source, "REBOOT_DEVICE")
        return "restart"
    if down >= REDIAL_AFTER_S and now - state.get("redialed", -1e9) >= REDIAL_AFTER_S:
        state["redialed"] = now
        print("modem: no data session for %.0f s; dropping and redialling" % down, flush=True)
        command(gateway, source, "DISCONNECT_NETWORK")
        time.sleep(3)
        command(gateway, source, "CONNECT_NETWORK")
        return "redial"
    return None


def ping_host():
    """The display server, from the car computer's own publisher settings, unless one is set."""
    host = os.environ.get("SLOT17_MODEM_PING_HOST", "").strip()
    if host:
        return host
    for path in sorted(glob.glob("/home/*/.config/slot17-display.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                url = json.load(fh).get("url", "")
            host = urlsplit(url).hostname
            if host:
                return host
        except (OSError, ValueError, AttributeError):
            continue
    return None


def round_trip(device, host, count=5):
    """(sent, received, median ms, max ms) for pings to `host` through `device`, or None."""
    if not host:
        return None
    try:
        out = subprocess.run(["ping", "-n", "-q", "-I", device, "-c", str(count), "-i", "0.3", "-W", "2", host],
                             capture_output=True, text=True, timeout=count * 2 + 5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    got = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", out)
    if not got:
        return None
    sent, received = int(got[1]), int(got[2])
    times = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/", out)
    return (sent, received, float(times[2]) if times else None, float(times[3]) if times else None)


def describe_trip(trip, host):
    if trip is None:
        return "round trip not measured"
    sent, received, avg, worst = trip
    if not received:
        return "round trip to %s: %d of %d lost" % (host, sent, sent)
    text = "round trip to %s %.0f ms (worst %.0f ms)" % (host, avg, worst)
    return text + (", %d of %d lost" % (sent - received, sent) if received < sent else "")


def load_state(path=None):
    try:
        with open(path or STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    """Written world-readable: the car computer's publisher (not root) shows the reading in the app."""
    path = path or STATE_FILE
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.chmod(path + ".tmp", 0o644)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def set_route(device, metric):
    current = nmcli("-g", "ipv4.route-metric,ipv4.dns-priority", "connection", "show", PROFILE)
    if current.split() == [str(metric), "0"]:
        return
    # --temporary: the USB fix keeps this profile in memory only, and so do we.
    nmcli("connection", "modify", "--temporary", PROFILE,
          "ipv4.route-metric", str(metric), "ipv4.dns-priority", "0")
    nmcli("device", "reapply", device)
    print("modem: route metric set to %d on %s" % (metric, device), flush=True)


def check(device, gateway, address, state, now):
    """One run: session, roaming, recovery and the report. Returns True when the session is up."""
    try:
        info = status(gateway, address)
        ensure_roaming(gateway, address, info)
        connected = ensure_session(gateway, address)
    except (OSError, ValueError, http.client.HTTPException) as error:
        print("modem: status page not reachable: %s" % error, flush=True)
        info, connected = {}, False
    keep = ("network_type", "signalbar", "lte_rsrp", "lte_rsrq", "lte_snr", "cell_id", "lte_pci", "lte_band",
            "roam_setting_option", "network_provider", "ppp_status")
    state["reading"] = {k: str(info.get(k))[:24] for k in keep if isinstance(info, dict) and info.get(k) not in (None, "")}
    state["reading_at"] = now
    state["device"] = device
    state["connected"] = connected
    if not connected:
        state["trip"] = None
        state.setdefault("down_since", now)
        try:
            recover(gateway, address, state, now)
        except (OSError, ValueError, http.client.HTTPException) as error:
            print("modem: recovery request not answered: %s" % error, flush=True)
        return False
    was_down = state.pop("down_since", None)
    state.pop("redialed", None)
    try:
        info = status(gateway, address) or info
    except (OSError, ValueError, http.client.HTTPException):
        pass
    host = ping_host()
    trip = round_trip(device, host)
    state["trip"] = list(trip) if trip else None
    cell = "%s/%s" % (info.get("cell_id", ""), info.get("lte_pci", ""))
    reasons = []
    if was_down is not None:
        reasons.append("back after %.0f s without a session" % (now - was_down))
    if state.get("cell") not in (None, cell):
        reasons.append("cell changed from %s" % state.get("cell"))
    if trip is not None and trip[1] < trip[0]:
        reasons.append("packets lost")
    if now - state.get("reported", -1e9) >= REPORT_EVERY_S:
        reasons.append("regular report")
    if reasons:
        state["reported"] = now
        print("modem: link up (%s); %s [%s]" % (describe(info) or "reception not reported",
                                                describe_trip(trip, host), "; ".join(reasons)), flush=True)
    state["cell"] = cell
    return True


def main(argv=()):
    # No options: the settings come from the environment. Anything else, --help included, only prints the
    # description above or refuses, so a probe by hand never touches the stick or the routes (25 November 2026).
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(list(argv))
    device = active_device()
    state = load_state()
    now = time.monotonic()
    if device is None:
        if state.get("present"):
            print("modem: the stick's network interface is gone (unplugged, or restarting)", flush=True)
        save_state({k: v for k, v in state.items() if k in ("restarted", "down_since")} | {"present": False})
        return 0
    state["present"] = True
    gateway = nmcli("-g", "IP4.GATEWAY", "device", "show", device)
    address = nmcli("-g", "IP4.ADDRESS", "device", "show", device).split("/")[0]
    if not gateway or not address:
        save_state(state)
        return 0
    connected = check(device, gateway, address, state, now)
    save_state(state)
    set_route(device, METRIC_UP if connected else METRIC_DOWN)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
