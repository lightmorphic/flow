"""The machine that receives the mouse and keyboard."""
from __future__ import annotations

import socket
import ssl
import threading
import time

from . import config, discovery, net, protocol, screen
from .clipboard import Clipboard
from .linux_input import (EV_KEY, EV_REL, InputError, REL_X, REL_Y, VirtualDevice)

RECONNECT_SECONDS = 3.0
SLAM = 40000            # far enough to pin the cursor into a corner


class Client:
    def __init__(self, cfg=None, log=print):
        self.cfg = cfg or config.load()
        self.log = log
        self.running = False

        size = screen.detect()
        self.width = size[0] if size else int(self.cfg["screen_width"])
        self.height = size[1] if size else int(self.cfg["screen_height"])

        self.id = discovery.machine_id()
        self.name = discovery.machine_name()
        self.listener = None
        self.pointer = None
        self.keyboard = None
        self.conn = None
        self._lock = threading.Lock()
        self.clipboard = Clipboard(self._send_clipboard,
                                   self.cfg["clipboard_poll_ms"], log=self.log)

    # ---------------------------------------------------------------- devices
    def _open_devices(self):
        if self.pointer is None:
            self.pointer = VirtualDevice("Lightmorphic Flow pointer", pointer=True)
            self.keyboard = VirtualDevice("Lightmorphic Flow keyboard", pointer=False)
            time.sleep(0.3)        # let the desktop notice the new devices

    def _close_devices(self):
        for dev in (self.pointer, self.keyboard):
            if dev:
                dev.close()
        self.pointer = self.keyboard = None

    # ---------------------------------------------------------------- running
    def run(self):
        self.running = True
        self._open_devices()
        if self.cfg["share_clipboard"]:
            self.clipboard.start()
        if self.cfg["discovery"]:
            self.listener = discovery.Listener(ignore_id=self.id)
            self.listener.start()
        self.log(f"screen {self.width}x{self.height}; looking for the other machine")
        while self.running:
            try:
                self._session()
            except (OSError, ssl.SSLError, ValueError) as exc:
                self.log(f"not connected ({exc}); retrying")
            if self.running:
                time.sleep(RECONNECT_SECONDS)
        self.stop()

    def stop(self):
        self.running = False
        self.clipboard.stop()
        if self.listener is not None:
            self.listener.stop()
        self._close_devices()

    def _find_host(self):
        """Prefer whatever is announcing itself; fall back to the saved address."""
        if self.listener is not None:
            servers = self.listener.found(role="server")
            wanted = self.cfg.get("server_id")
            for entry in servers:
                if wanted and entry["id"] == wanted:
                    return entry["host"], int(entry.get("port", self.cfg["port"]))
            if servers and not wanted:
                entry = servers[0]
                return entry["host"], int(entry.get("port", self.cfg["port"]))
        if self.cfg.get("server_host"):
            return self.cfg["server_host"], int(self.cfg["port"])
        return None, None

    def _session(self):
        host, port = self._find_host()
        if not host:
            raise ValueError("no other machine found yet")
        raw = socket.create_connection((host, port), timeout=10)
        net.tune(raw)
        conn = net.client_context().wrap_socket(raw, server_hostname="lmflow")

        pinned = self.cfg.get("server_fingerprint", "")
        actual = net.peer_fingerprint(conn)
        if pinned and pinned.lower() != actual:
            conn.close()
            raise ValueError("that machine's certificate does not match the saved one")

        conn.sendall(protocol.pack_json(protocol.HELLO, {
            "id": self.id, "name": self.name, "token": self.cfg["token"],
            "width": self.width, "height": self.height,
        }))
        framer = protocol.Framer()
        conn.settimeout(10)
        while True:
            data = conn.recv(65536)
            if not data:
                conn.close()
                raise ValueError(
                    "the other machine has not been told to accept us - press "
                    "'Allow a new computer' over there")
            messages = framer.feed(data)
            hello = next((b for k, b in messages if k == protocol.HELLO), None)
            if hello is not None:
                self._accept_hello(hello, host, actual)
                for kind, body in messages:
                    if kind != protocol.HELLO:
                        self._dispatch(kind, body)
                break
        conn.settimeout(None)
        with self._lock:
            self.conn = conn
        self.log("connected")

        try:
            while self.running:
                data = conn.recv(65536)
                if not data:
                    break
                for kind, body in framer.feed(data):
                    self._dispatch(kind, body)
        finally:
            with self._lock:
                self.conn = None
            try:
                conn.close()
            except OSError:
                pass
            self.log("disconnected")

    def _accept_hello(self, body, host, fingerprint):
        """Only now, once the other machine has accepted us, is it worth remembering."""
        import json
        info = json.loads(body.decode("utf-8"))
        changed = {}
        if info.get("token") and info["token"] != self.cfg.get("token"):
            changed["token"] = info["token"]
        if self.cfg.get("server_host") != host:
            changed["server_host"] = host
        if self.cfg.get("server_fingerprint") != fingerprint:
            changed["server_fingerprint"] = fingerprint
        if info.get("id") and self.cfg.get("server_id") != info["id"]:
            changed["server_id"] = info["id"]
        for key, value in changed.items():
            self.cfg[key] = value
            config.set_value(key, value)
        if changed.get("token"):
            self.log("paired with " + (info.get("name") or host))

    # -------------------------------------------------------------- messages
    def _dispatch(self, kind, body):
        if kind == protocol.INPUT:
            self._replay(protocol.unpack_events(body))
        elif kind == protocol.ENTER:
            import json
            pos = json.loads(body.decode("utf-8"))
            self._place(int(pos["x"]), int(pos["y"]))
        elif kind == protocol.CLIPBOARD and self.cfg["share_clipboard"]:
            self.clipboard.apply(body.decode("utf-8", "replace"))
        elif kind == protocol.HELLO:
            pass

    def _replay(self, events):
        pointer_events = [e for e in events if e[0] == EV_REL or
                          (e[0] == EV_KEY and 0x110 <= e[1] <= 0x117)]
        key_events = [e for e in events if e[0] == EV_KEY and e[1] < 0x100]
        if pointer_events:
            self.pointer.emit(pointer_events)
        if key_events:
            self.keyboard.emit(key_events)

    def _place(self, x, y):
        """No absolute pointer here, so pin to the top-left then step out."""
        self.pointer.emit([(EV_REL, REL_X, -SLAM), (EV_REL, REL_Y, -SLAM)])
        time.sleep(0.01)
        self.pointer.emit([(EV_REL, REL_X, max(0, x)), (EV_REL, REL_Y, max(0, y))])

    def _send_clipboard(self, text):
        with self._lock:
            conn = self.conn
        if conn is None:
            return
        try:
            conn.sendall(protocol.pack(protocol.CLIPBOARD, text.encode("utf-8")))
        except OSError:
            pass
