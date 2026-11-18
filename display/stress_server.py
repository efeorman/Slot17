#!/usr/bin/env python3
"""Attack and load test for the display server. LOCAL ONLY: it starts its own throwaway server on loopback,
with made-up keys and temporary folders, and refuses any other target. Never point this at a real host.

    python display/stress_server.py [--seconds 20] [--out report.json]

Each scenario runs while a legitimate phone and Pi keep working, because what matters is whether real
traffic survives. A scenario passes only if real traffic did, and the server stayed up and logged no crash.
"""
import argparse
import http.client
import json
from pathlib import Path
import random
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
ATTACKER = "127.0.0.2"  # a different source address, like a different machine on the internet
SAFE_TARGETS = {"127.0.0.1"}


def request(port, method, path, body=None, headers=None, timeout=4):
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


class Legitimate(threading.Thread):
    """A phone asking for state and a Pi asking for its ticket, as often as in real use."""

    def __init__(self, port, key, token):
        super().__init__(daemon=True)
        self.port, self.key, self.token = port, key, token
        self.ok, self.failed, self.latency = 0, 0, []
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            for path, headers in (("/api/state", {"Cookie": f"s17={self.key}"}),
                                  ("/api/clock", {"Authorization": f"Bearer {self.token}", "X-Display-Source": "stress-pi"})):
                started = time.perf_counter()
                try:
                    status, _ = request(self.port, "GET", path, headers=headers)
                    if status == 200:
                        self.ok += 1
                        self.latency.append(time.perf_counter() - started)
                    else:
                        self.failed += 1
                except OSError:
                    self.failed += 1
            self.stopping.wait(0.25)

    def result(self):
        self.stopping.set()
        self.join(8)
        lat = sorted(self.latency)
        return {"ok": self.ok, "failed": self.failed,
                "p50_ms": round(lat[len(lat) // 2] * 1000, 1) if lat else None,
                "p95_ms": round(lat[int(len(lat) * 0.95)] * 1000, 1) if lat else None}


def attacker_socket(port):
    s = socket.socket()
    s.settimeout(4)
    s.bind((ATTACKER, 0))
    s.connect((HOST, port))
    return s


def slow_headers(port, seconds, stop, count=80):
    """Keep connections busy by sending a header byte now and then, faster than the idle limit."""
    socks = []
    while not stop.is_set():
        while len(socks) < count and not stop.is_set():
            try:
                s = attacker_socket(port)
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
                socks.append(s)
            except OSError:
                break
        for s in list(socks):
            try:
                s.sendall(b"X-a: b\r\n")
            except OSError:
                socks.remove(s)
        stop.wait(1)
    for s in socks:
        s.close()


def connection_flood(port, seconds, stop):
    while not stop.is_set():
        try:
            s = attacker_socket(port)
            s.close()
        except OSError:
            time.sleep(0.005)


def slow_body(port, seconds, stop, count=40):
    socks = []
    while not stop.is_set():
        while len(socks) < count and not stop.is_set():
            try:
                s = attacker_socket(port)
                s.sendall(b"POST /api/push HTTP/1.1\r\nHost: x\r\nContent-Length: 60000\r\n\r\n")
                socks.append(s)
            except OSError:
                break
        for s in list(socks):
            try:
                s.sendall(b"x")
            except OSError:
                socks.remove(s)
        stop.wait(0.5)
    for s in socks:
        s.close()


def request_flood(port, seconds, stop, threads=12):
    def worker():
        while not stop.is_set():
            try:
                s = attacker_socket(port)
                s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                s.recv(200)
                s.close()
            except OSError:
                time.sleep(0.005)
    workers = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for w in workers:
        w.start()
    stop.wait(seconds + 1)


def garbage(port, seconds, stop):
    """Malformed requests of every kind. The server must answer or close, and never crash a handler."""
    rng = random.Random(17)
    samples = [
        b"\x00\xff\xfe" * 200, b"GET", b"GET / HTTP/9.9\r\n\r\n", b"GET " + b"/" * 70000 + b" HTTP/1.1\r\n\r\n",
        b"POST /api/op HTTP/1.1\r\nContent-Length: -5\r\n\r\n", b"POST /api/op HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
        b"POST /api/push HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n", b"GET /api/logs/../../config.json HTTP/1.1\r\n\r\n",
        b"GET /api/sounds/..%2f..%2fconfig.json HTTP/1.1\r\n\r\n", b"GET /api/logs/%2e%2e%2fconfig.json HTTP/1.1\r\n\r\n",
        b"GET /?key=\xff\xfe HTTP/1.1\r\n\r\n", b"GET /api/pi-on?token=\xff HTTP/1.1\r\n\r\n",
        b"GET / HTTP/1.1\r\nCookie: " + b";" * 8000 + b"\r\n\r\n", b"GET / HTTP/1.1\r\nCookie: s17=\xff\xfe\r\n\r\n",
        b"GET / HTTP/1.1\r\n" + b"X: y\r\n" * 200 + b"\r\n", b"TRACE / HTTP/1.1\r\n\r\n", b"OPTIONS * HTTP/1.1\r\n\r\n",
    ]
    while not stop.is_set():
        data = rng.choice(samples) if rng.random() < 0.8 else bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 400)))
        try:
            s = attacker_socket(port)
            s.sendall(data)
            s.settimeout(0.3)
            try:
                s.recv(200)
            except OSError:
                pass
            s.close()
        except OSError:
            time.sleep(0.01)


def authenticated_garbage(port, key, token, stop):
    """Well-formed requests from someone holding a key, with wrong types in every field."""
    values = [None, True, 1, -1, 1e308, "", "x" * 600, [], {}, [[]], {"a": 1}, "police", "0" * 32]
    cookie = {"Cookie": f"s17={key}", "Content-Type": "application/json"}
    bearer = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    rng = random.Random(23)
    while not stop.is_set():
        op = {f: rng.choice(values) for f in ("kind", "id", "label", "note", "age_s", "phone_utc")}
        cmd = {"action": rng.choice(values + ["stop"]), "note": rng.choice(values)}
        state = {"v": 3, "detector": {"state": rng.choice(values + ["running"])}, "channels": rng.choice(values), "matches": rng.choice(values),
                 "source": rng.choice(values), "ticket": rng.choice(values), "seq": rng.choice(values), "acks": rng.choice(values), "pi": rng.choice(values)}
        deep = b"[" * 20000
        for path, headers, body in (("/api/op", cookie, json.dumps(op)), ("/api/command", cookie, json.dumps(cmd)),
                                    ("/api/push", bearer, json.dumps(state)), ("/api/op", cookie, deep), ("/api/push", bearer, b"\xff" * 200)):
            try:
                request(port, "POST", path, body=body, headers=headers)
            except OSError:
                pass


def guessing(port, stop, results):
    """Every endpoint with wrong, empty and nearly right keys. None of these may ever succeed."""
    while not stop.is_set():
        wrong = secrets.token_urlsafe(18)
        for method, path, headers in (("GET", "/api/state", {"Cookie": f"s17={wrong}"}), ("GET", "/api/logs", {"Cookie": f"s17={wrong}"}),
                                      ("GET", "/api/config", {}), ("GET", f"/api/pi-on?token={wrong}", {}), ("GET", "/api/stream", {"Cookie": "s17="}),
                                      ("GET", "/api/clock", {"Authorization": f"Bearer {wrong}", "X-Display-Source": "a"}),
                                      ("POST", "/api/command", {"Cookie": f"s17={wrong}"}), ("POST", "/api/push", {"Authorization": "Bearer "})):
            try:
                status, _ = request(port, method, path, body=b'{"action":"stop"}' if method == "POST" else None, headers=headers)
                results["tried"] += 1
                if status < 300:
                    results["accepted"] += 1
            except OSError:
                pass


def stalled_streams(port, key, stop):
    """Holders of a key who open event streams and never read them."""
    socks = []
    while not stop.is_set():
        try:
            s = attacker_socket(port)
            s.sendall(f"GET /api/stream HTTP/1.1\r\nHost: x\r\nCookie: s17={key}\r\n\r\n".encode())
            socks.append(s)
        except OSError:
            pass
        stop.wait(0.2)
    for s in socks:
        s.close()


def memory_mb(pid):
    try:
        import psutil
        return round(psutil.Process(pid).memory_info().rss / 1e6, 1)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--out")
    args = ap.parse_args()
    if HOST not in SAFE_TARGETS:
        raise SystemExit("this test only runs against its own local server")
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]
    key, token, status_key = secrets.token_urlsafe(18), secrets.token_urlsafe(32), secrets.token_urlsafe(18)
    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "config.json"
    cfg.write_text(json.dumps({"host": HOST, "port": port, "pi_token": token, "viewer_key": key, "pi_status_key": status_key}))
    errlog = open(Path(tmp.name) / "stderr.txt", "w+", encoding="utf-8")
    import os
    env = dict(os.environ, SLOT17_DISPLAY_CONFIG=str(cfg), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=errlog, cwd=tmp.name)
    for _ in range(100):
        try:
            request(port, "GET", "/healthz")
            break
        except OSError:
            time.sleep(0.1)
    guesses = {"tried": 0, "accepted": 0}
    scenarios = {
        "no attack": lambda s, stop: stop.wait(s),
        "slow headers, 80 connections": lambda s, stop: slow_headers(port, s, stop),
        "slow bodies, 40 connections": lambda s, stop: slow_body(port, s, stop),
        "connection flood": lambda s, stop: connection_flood(port, s, stop),
        "request flood": lambda s, stop: request_flood(port, s, stop),
        "malformed requests": lambda s, stop: garbage(port, s, stop),
        "wrong types, holding a key": lambda s, stop: authenticated_garbage(port, key, token, stop),
        "guessing keys": lambda s, stop: guessing(port, stop, guesses),
        "stalled streams, holding a key": lambda s, stop: stalled_streams(port, key, stop),
    }
    report = []
    for name, attack in scenarios.items():
        legit = Legitimate(port, key, token)
        stop = threading.Event()
        t = threading.Thread(target=attack, args=(args.seconds, stop), daemon=True)
        before = errlog.tell()
        t.start()
        time.sleep(min(3, args.seconds / 4))  # let the attack build up first, then measure real traffic
        legit.start()
        time.sleep(args.seconds)
        stop.set()
        row = dict(scenario=name, **legit.result())
        total = row["ok"] + row["failed"]
        row["served_pct"] = round(100 * row["ok"] / total, 1) if total else 0
        errlog.flush()
        errlog.seek(before)
        text = errlog.read()
        row["handler_crashes"] = text.count("Traceback")
        if row["handler_crashes"]:
            row["first_crash"] = [line for line in text.splitlines() if line and not line.startswith(" ")][-1][:120]
        row["server_alive"] = proc.poll() is None
        row["memory_mb"] = memory_mb(proc.pid)
        row["pass"] = row["server_alive"] and row["served_pct"] >= 99 and row["handler_crashes"] == 0
        report.append(row)
        print(f"{'PASS' if row['pass'] else 'FAIL'}  {name:34s} real traffic served {row['served_pct']:5.1f}%  "
              f"p95 {row['p95_ms']} ms  crashes {row['handler_crashes']}  mem {row['memory_mb']} MB"
              f"{'  last: ' + row['first_crash'] if row.get('first_crash') else ''}", flush=True)
        time.sleep(17)  # the server recovers between scenarios
    print(f"key guesses tried {guesses['tried']}, accepted {guesses['accepted']}")
    report.append({"key_guesses": guesses, "pass": guesses["accepted"] == 0})
    alive = proc.poll() is None
    proc.terminate()
    proc.wait(10)
    errlog.close()
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    tmp.cleanup()
    print("server still running at the end:", alive)
    return 0 if alive and all(r["pass"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
