"""Finding the other computers.

The computer being controlled does the asking, and the one with the keyboard
answers it directly. That matters: a reply to a question you asked is let back
through a firewall as a matter of course, where something shouted at you
unasked is dropped. Deskflow never hit this because you type its address in.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import socket
import struct
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


SIOCGIFBRDADDR = 0x8919


def _broadcast_addresses():
    """Ask the kernel for each interface's real broadcast address.

    Guessing a.b.c.255 is wrong on any network that is not a /24, and a home
    network on a /22 - which is common - never hears a word. That one wrong
    assumption is why two machines could sit side by side and find nothing.
    """
    # 127.0.0.1 as well, so two copies on one machine can still see each other.
    addrs = {"255.255.255.255", "127.0.0.1"}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(sock.fileno(), SIOCGIFBRDADDR,
                                     struct.pack("256s", name.encode()[:15]))
                address = socket.inet_ntoa(packed[20:24])
            except OSError:
                continue
            if address not in ("0.0.0.0", "255.255.255.255"):
                addrs.add(address)
    finally:
        sock.close()
    return sorted(addrs)


class Responder:
    """On the computer with the keyboard: answer anyone who asks."""

    def __init__(self, payload_fn, log=print):
        self._payload_fn = payload_fn
        self._log = log
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", PORT))
        except OSError as exc:
            self._log(f"discovery: cannot listen for questions ({exc})")
            return
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

    def _payload(self):
        return json.dumps(self._payload_fn()).encode("utf-8")

    def _loop(self):
        last_shout = 0.0
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                data = None
            except OSError:
                return
            if data:
                try:
                    msg = json.loads(data.decode("utf-8"))
                except ValueError:
                    msg = {}
                if msg.get("app") == MAGIC and msg.get("q"):
                    try:
                        self._sock.sendto(self._payload(), addr)
                    except OSError:
                        pass

            # Also say so unprompted, which reaches anyone without a firewall.
            now = time.monotonic()
            if now - last_shout > INTERVAL:
                last_shout = now
                blob = self._payload()
                for target in _broadcast_addresses():
                    try:
                        self._sock.sendto(blob, (target, PORT))
                    except OSError:
                        pass


class Seeker:
    """On the computer being controlled: ask, and collect the answers.

    Its socket is on a port of its own, so the replies arrive as answers to a
    question this machine asked, and a firewall lets them through.
    """

    def __init__(self, ignore_id=None):
        self.ignore_id = ignore_id or machine_id()
        self._seen = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
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

    def _ask(self):
        question = json.dumps({"app": MAGIC, "q": 1}).encode("utf-8")
        for target in _broadcast_addresses():
            try:
                self._sock.sendto(question, (target, PORT))
            except OSError:
                pass

    def _loop(self):
        last_ask = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_ask > INTERVAL:
                last_ask = now
                self._ask()
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
            if msg.get("app") != MAGIC or not msg.get("id") or msg.get("q"):
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


# Old names, so nothing else has to care which way round it works.
Announcer = Responder
Listener = Seeker


def scan(seconds=3.0, role=None):
    """One-shot: ask, wait a few seconds, and return who answered."""
    seeker = Seeker()
    seeker.start()
    time.sleep(seconds)
    found = seeker.found(role=role)
    seeker.stop()
    return found
