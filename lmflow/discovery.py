"""Finding other machines on the same network, with a UDP announcement."""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time

PORT = 24811
MAGIC = "lmflow"
INTERVAL = 2.0
STALE_AFTER = 8.0


def machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = open(path, encoding="ascii").read().strip()
            if raw:
                return hashlib.sha256(raw.encode()).hexdigest()[:16]
        except OSError:
            continue
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]


def machine_name() -> str:
    return socket.gethostname()


def _broadcast_addresses():
    # 127.0.0.1 as well, so two copies on one machine can still see each other.
    addrs = {"255.255.255.255", "127.0.0.1"}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("198.51.100.1", 9))
        own = sock.getsockname()[0]
        sock.close()
        parts = own.split(".")
        addrs.add(".".join(parts[:3] + ["255"]))
    except OSError:
        pass
    return sorted(addrs)


class Announcer:
    """Says 'here I am' on the network every couple of seconds."""

    def __init__(self, payload_fn, log=print):
        self._payload_fn = payload_fn
        self._log = log
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self._stop.is_set():
            try:
                blob = json.dumps(self._payload_fn()).encode("utf-8")
                for addr in _broadcast_addresses():
                    try:
                        sock.sendto(blob, (addr, PORT))
                    except OSError:
                        pass
            except Exception as exc:                          # pragma: no cover
                self._log(f"discovery: {exc}")
            self._stop.wait(INTERVAL)
        sock.close()


class Listener:
    """Collects announcements. found() returns what has been heard recently."""

    def __init__(self, ignore_id=None):
        self.ignore_id = ignore_id or machine_id()
        self._seen = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("0.0.0.0", PORT))
        sock.settimeout(0.5)
        self._sock = sock
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("app") != MAGIC or not msg.get("id"):
                continue
            if msg["id"] == self.ignore_id:
                continue
            msg["host"] = addr[0]
            msg["seen"] = time.monotonic()
            with self._lock:
                self._seen[msg["id"]] = msg

    def found(self, role=None):
        now = time.monotonic()
        with self._lock:
            items = [dict(m) for m in self._seen.values()
                     if now - m["seen"] < STALE_AFTER]
        if role:
            items = [m for m in items if m.get("role") == role]
        return sorted(items, key=lambda m: m.get("name", ""))


def scan(seconds=3.0, role=None):
    """One-shot: listen for a few seconds and return what answered."""
    listener = Listener()
    listener.start()
    time.sleep(seconds)
    found = listener.found(role=role)
    listener.stop()
    return found
