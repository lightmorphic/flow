"""The machine that receives the mouse and keyboard."""
from __future__ import annotations

import socket
import ssl
import threading
import time

from . import config, discovery, net, protocol, screen, status
from .clipboard import Clipboard
from .linux_input import (ABS_RANGE, ABS_X, ABS_Y, EV_ABS, EV_KEY, EV_REL,
                          REL_X, REL_Y, VirtualDevice)

RECONNECT_SECONDS = 3.0


class Client:
    def __init__(self, cfg=None, log=print):
        self.cfg = cfg or config.load()
        self.log = log
        self.running = False

        size = screen.detect()
        self.width = size[0] if size else int(self.cfg["screen_width"])
        self.height = size[1] if size else int(self.cfg["screen_height"])

        self.protocol = protocol.VERSION
        self._applied = 0
        self._noted = 0.0
        self.id = discovery.machine_id()
        self.name = discovery.machine_name()
        self.listener = None
        self.pointer = None
        self.presser = None
        self.keyboard = None
        self.conn = None
        self._lock = threading.Lock()
        self.clipboard = Clipboard(self._send_clipboard,
                                   self.cfg["clipboard_poll_ms"], log=self.log)

    # ---------------------------------------------------------------- devices
    @property
    def wants_positions(self):
        return self.cfg.get("pointer_mode", "position") != "movement"

    def _open_devices(self):
        if self.pointer is None:
            self.pointer = VirtualDevice("Lightmorphic Flow pointer",
                                         pointer=True,
                                         absolute=self.wants_positions)
            # A pointer that is placed never presses against anything, and
            # GNOME opens the Overview only on pressure at a hot corner. This
            # second one carries that pressure when you push into an edge.
            self.presser = (VirtualDevice("Lightmorphic Flow pointer pressure",
                                          pointer=True)
                            if self.wants_positions else None)
            self.keyboard = VirtualDevice("Lightmorphic Flow keyboard", pointer=False)
            time.sleep(0.3)        # let the desktop notice the new devices

    def _close_devices(self):
        for dev in (self.pointer, getattr(self, "presser", None), self.keyboard):
            if dev:
                dev.close()
        self.pointer = self.keyboard = self.presser = None

    # ---------------------------------------------------------------- running
    def publish(self):
        found = self.listener.found(role="server") if self.listener else []
        status.write({
            "role": "client",
            "running": self.running,
            "connected": self.conn is not None,
            "server": self.cfg.get("server_host") or "",
            "server_name": self.cfg.get("server_name") or "",
            "identity_changed": False,
            "active": None,
            "peers": [],
            "found": [{"id": f["id"], "name": f.get("name", f["host"]),
                       "host": f["host"], "pairing": bool(f.get("pairing"))}
                      for f in found],
        })

    def _keep_publishing(self):
        """The settings window reads this file. Left to the moments when
        something happened, it could sit there saying 'nothing found' while
        perfectly well connected."""
        while self.running:
            self.publish()
            time.sleep(2.0)

    def run(self):
        self.running = True
        self._open_devices()
        threading.Thread(target=self._keep_publishing, daemon=True).start()
        if self.cfg["share_clipboard"]:
            self.clipboard.start()
        if self.cfg["discovery"]:
            self.listener = discovery.Listener(ignore_id=self.id)
            self.listener.start()
        from . import __version__
        how = "told where to be" if self.wants_positions else "told how far to move"
        self.log(f"Lightmorphic Flow {__version__}; screen "
                 f"{self.width}x{self.height}; pointer is {how}; "
                 "looking for the other machine")
        while self.running:
            try:
                self.publish()
                self._session()
            except (OSError, ssl.SSLError, ValueError) as exc:
                self.log(f"not connected ({exc}); retrying")
            self.publish()
            if self.running:
                time.sleep(RECONNECT_SECONDS)
        self.stop()

    def stop(self):
        self.running = False
        status.clear()
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
            status.write({"role": "client", "running": True, "connected": False,
                          "server": host, "server_name": self.cfg.get("server_name", ""),
                          "identity_changed": True, "active": None,
                          "peers": [], "found": []})
            raise ValueError(
                f"{self.cfg.get('server_name') or host} is not the computer it "
                "was: its identity has changed, usually because Lightmorphic "
                "Flow was reinstalled there. Press 'Trust it again' in the "
                "settings window, or 'Allow a new computer' over there.")

        from . import __version__
        conn.sendall(protocol.pack_json(protocol.HELLO, {
            "id": self.id, "name": self.name, "token": self.cfg["token"],
            "width": self.width, "height": self.height,
            "protocol": self.protocol, "version": __version__,
            "wants": "position" if self.wants_positions else "movement",
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
        self.publish()

        try:
            while self.running:
                data = conn.recv(65536)
                if not data:
                    break
                for kind, body in framer.feed(data):     # ValueError ends the session
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
        if info.get("name") and self.cfg.get("server_name") != info["name"]:
            changed["server_name"] = info["name"]
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
        elif kind == protocol.PING:
            with self._lock:
                conn = self.conn
            if conn is not None:
                try:
                    conn.sendall(protocol.pack(protocol.PONG, b""))
                except OSError:
                    pass
        elif kind == protocol.HELLO:
            pass

    def _note(self, events):
        self._applied += len(events)
        now = time.monotonic()
        if now - self._noted > 5.0:
            self._noted = now
            self.log(f"applied {self._applied} pointer events so far")

    def _replay(self, events):
        presser = getattr(self, "presser", None)
        pressure = [e for e in events
                    if presser is not None and e[0] == EV_REL and e[1] in (REL_X, REL_Y)]
        pointer_events = [e for e in events
                          if (e[0] in (EV_REL, EV_ABS) and e not in pressure)
                          or (e[0] == EV_KEY and 0x110 <= e[1] <= 0x117)]
        key_events = [e for e in events if e[0] == EV_KEY and e[1] < 0x100]
        if pointer_events:
            self.pointer.emit(pointer_events)
            self._note(pointer_events)
        if pressure:
            presser.emit(pressure)
        if key_events:
            self.keyboard.emit(key_events)

    def _place(self, x, y):
        """Told where to be, in the 0..32767 scale the device was set up with."""
        sx = max(0, min(ABS_RANGE, int(x * ABS_RANGE / max(1, self.width - 1))))
        sy = max(0, min(ABS_RANGE, int(y * ABS_RANGE / max(1, self.height - 1))))
        self.pointer.emit([(EV_ABS, ABS_X, sx), (EV_ABS, ABS_Y, sy)])

    def _send_clipboard(self, text):
        with self._lock:
            conn = self.conn
        if conn is None:
            return
        try:
            conn.sendall(protocol.pack(protocol.CLIPBOARD, text.encode("utf-8")))
        except OSError:
            pass
