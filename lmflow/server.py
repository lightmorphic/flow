"""The machine whose mouse and keyboard are being shared.

One machine may sit on each edge of this screen, so up to four at once.
"""
from __future__ import annotations

import errno
import json
import queue
import selectors
import socket
import struct
import ssl
import threading
import time

from . import config, discovery, net, protocol, screen, status
from .clipboard import Clipboard
from .hotkeys import HotkeyWatcher
from .linux_input import (EV_KEY, EV_REL, InputError, InputReader, REL_X, REL_Y,
                          list_devices)
from .touchpad import TouchpadTranslator

RESCAN_SECONDS = 3.0
HEARTBEAT_SECONDS = 2.0
WATCHDOG_SECONDS = 4.0
SILENCE_SECONDS = 8.0
EDGES = ("right", "left", "top", "bottom")
OPPOSITE = {"right": "left", "left": "right", "top": "bottom", "bottom": "top"}


class Peer:
    """One connected machine.

    Everything is handed to a thread of its own to write. The loop that reads
    your mouse must never wait on the network: if the other machine stops
    reading, a direct send blocks for ever, and it blocks while your keyboard
    and mouse are held - which locks the computer you are sitting at.
    """

    OUTBOX = 512            # frames; a few seconds of furious mousing

    def __init__(self, ident, name, conn, size, edge):
        self.id = ident
        self.name = name
        self.conn = conn
        self.size = size
        self.edge = edge
        self.heard = time.monotonic()
        self.alive = True
        self.outbox = queue.Queue(maxsize=self.OUTBOX)
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def send(self, blob):
        """Never waits. False means this machine is not keeping up."""
        if not self.alive:
            return False
        try:
            self.outbox.put_nowait(blob)
        except queue.Full:
            self.alive = False
            return False
        return True

    def _write_loop(self):
        while True:
            blob = self.outbox.get()
            if blob is None:
                return
            try:
                self.conn.sendall(blob)
            except (OSError, ValueError):
                self.alive = False
                return

    def close(self):
        self.alive = False
        try:
            self.outbox.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


class Server:
    def __init__(self, cfg=None, log=print):
        self.cfg = cfg or config.load()
        self.log = log
        self.running = False
        self._cfg_mtime = config.mtime()

        size = screen.detect()
        self.width = size[0] if size else int(self.cfg["screen_width"])
        self.height = size[1] if size else int(self.cfg["screen_height"])

        self.x = self.width // 2
        self.y = self.height // 2
        self.active = None                     # None = this machine, else a Peer

        self._readers = {}
        self._pads = {}
        self._selector = selectors.DefaultSelector()
        self._batch = []
        self._push = 0.0
        self._push_edge = None
        self._push_started = 0.0

        self.peers = {}                        # id -> Peer
        self._peers_lock = threading.Lock()
        self._alive_at = time.monotonic()

        self.clipboard = Clipboard(self._broadcast_clipboard,
                                   self.cfg["clipboard_poll_ms"], log=self.log)
        self.announcer = discovery.Announcer(self._announcement, log=self.log)

        self.hotkeys = HotkeyWatcher()
        self.hotkeys.bind(self.cfg["hotkey_switch"], self.cycle)
        self.hotkeys.bind(self.cfg["hotkey_panic"], self.panic)
        self.hotkeys.bind_panic(self.panic)
        self._last_ping = 0.0

    # ------------------------------------------------------------- discovery
    def _announcement(self):
        return {
            "app": discovery.MAGIC, "v": 1, "role": "server",
            "id": discovery.machine_id(), "name": discovery.machine_name(),
            "port": int(self.cfg["port"]), "pairing": config.pairing_open(),
            "connected": len(self.peers),
        }

    # ------------------------------------------------------------ devices
    def _wanted(self, info):
        if info.name in self.cfg.get("ignore_devices", []):
            return False
        if info.kind == "touchpad":
            return bool(self.cfg["grab_touchpads"])
        return info.kind in ("mouse", "keyboard")

    def _scan_devices(self):
        seen = set()
        for info in list_devices():
            if not self._wanted(info):
                continue
            seen.add(info.path)
            if info.path in self._readers:
                continue
            try:
                reader = InputReader(info)
            except OSError as exc:
                if exc.errno == errno.EACCES:
                    raise InputError(
                        f"no permission to read {info.path}. If you have just "
                        "installed Lightmorphic Flow, log out and back in once - "
                        "your session is still carrying the old group list."
                    ) from exc
                continue
            self._readers[info.path] = reader
            self._selector.register(reader.fd, selectors.EVENT_READ, reader)
            if info.kind == "touchpad":
                self._pads[info.path] = TouchpadTranslator()
            if self.active is not None:
                reader.grab()
            self.log(f"device: {info.kind} {info.name}")

        for path in [p for p in self._readers if p not in seen]:
            reader = self._readers.pop(path)
            try:
                self._selector.unregister(reader.fd)
            except (KeyError, ValueError):
                pass
            reader.close()
            self._pads.pop(path, None)
            self.log(f"device gone: {reader.info.name}")

    def _grab_all(self, grab):
        done, failed = [], []
        for reader in self._readers.values():
            try:
                reader.grab() if grab else reader.ungrab()
                done.append(reader.info.name)
            except OSError as exc:
                failed.append(f"{reader.info.name} ({exc})")
        for pad in self._pads.values():
            pad.reset()
        word = "took" if grab else "let go of"
        self.log(f"{word} {len(done)} device(s)"
                 + (f"; FAILED on {', '.join(failed)}" if failed else ""))
        still = [r.info.name for r in self._readers.values() if r.grabbed]
        if not grab and still:
            self.log(f"WARNING still holding: {', '.join(still)}")

    # ------------------------------------------------------------ switching
    def publish(self):
        with self._peers_lock:
            peers = [{"id": p.id, "name": p.name, "edge": p.edge} for p in
                     sorted(self.peers.values(), key=lambda p: EDGES.index(p.edge))]
        active = self.active
        status.write({
            "role": "server",
            "running": self.running,
            "active": {"id": active.id, "name": active.name, "edge": active.edge}
            if active is not None else None,
            "peers": peers,
        })

    def peer_on(self, edge):
        with self._peers_lock:
            for peer in self.peers.values():
                if peer.edge == edge:
                    return peer
        return None

    def cycle(self):
        """Hotkey: step through this machine and each connected one in turn."""
        with self._peers_lock:
            order = sorted(self.peers.values(), key=lambda p: EDGES.index(p.edge))
        if not order:
            self.log("no other machine connected yet")
            return
        if self.active is None:
            self.go_to(order[0])
            return
        ids = [p.id for p in order]
        try:
            nxt = ids.index(self.active.id) + 1
        except ValueError:
            nxt = len(ids)
        self.go_local() if nxt >= len(ids) else self.go_to(order[nxt])

    def panic(self):
        self.go_local()
        self.log("panic hotkey: control returned to this machine")

    def go_to(self, peer):
        if peer is None or self.active is peer:
            return
        if self.active is not None:
            self.active.send(protocol.pack(protocol.LEAVE, b""))
        rw, rh = peer.size
        if peer.edge == "right":
            nx, ny = 2, int(self._frac_y() * rh)
        elif peer.edge == "left":
            nx, ny = rw - 3, int(self._frac_y() * rh)
        elif peer.edge == "top":
            nx, ny = int(self._frac_x() * rw), rh - 3
        else:
            nx, ny = int(self._frac_x() * rw), 2
        self.x, self.y = nx, ny
        was_local = self.active is None
        peer.heard = time.monotonic()      # it has not gone quiet: we just arrived
        self.active = peer
        self._push = 0.0
        if was_local:
            self._grab_all(True)
        peer.send(protocol.pack_json(protocol.ENTER, {"x": nx, "y": ny}))
        self.log(f"pointer moved to {peer.name}")
        self.publish()

    def go_local(self):
        if self.active is None:
            return
        peer = self.active
        self.log(f"coming back from {peer.name}")
        edge = peer.edge
        if edge == "right":
            self.x, self.y = self.width - 3, int(self._frac_y() * self.height)
        elif edge == "left":
            self.x, self.y = 2, int(self._frac_y() * self.height)
        elif edge == "top":
            self.x, self.y = int(self._frac_x() * self.width), 2
        else:
            self.x, self.y = int(self._frac_x() * self.width), self.height - 3
        self.active = None
        self._push = 0.0
        self._grab_all(False)
        peer.send(protocol.pack(protocol.LEAVE, b""))
        self.log("pointer back on this machine")
        self.publish()

    def _frac_x(self):
        w = self.width if self.active is None else self.active.size[0]
        return self.x / max(1, w)

    def _frac_y(self):
        h = self.height if self.active is None else self.active.size[1]
        return self.y / max(1, h)

    # ------------------------------------------------------- motion + edges
    def _screen(self):
        return (self.width, self.height) if self.active is None else self.active.size

    def _move(self, dx, dy):
        speed = float(self.cfg["pointer_speed"])
        dx *= speed
        dy *= speed
        w, h = self._screen()
        self.x = min(w - 1, max(0, self.x + dx))
        self.y = min(h - 1, max(0, self.y + dy))

        pressing = {"right": dx, "left": -dx, "bottom": dy, "top": -dy}
        touching = {"right": self.x >= w - 1, "left": self.x <= 0,
                    "bottom": self.y >= h - 1, "top": self.y <= 0}

        if self.active is None:
            wanted = [e for e in EDGES
                      if touching[e] and pressing[e] > 0 and self.peer_on(e)]
        else:
            # Any edge brings you home, not only the one you came in by. Being
            # stuck on another machine is far worse than crossing back early.
            wanted = [e for e in EDGES if touching[e] and pressing[e] > 0]

        if not wanted:
            self._push = 0.0
            self._push_edge = None
            return
        edge = wanted[0]

        if self.active is None:
            guard = float(self.cfg["corner_guard_px"])
            along, span = (self.y, h) if edge in ("left", "right") else (self.x, w)
            if along < guard or along > span - guard:
                self._push = 0.0               # hot corners belong to the desktop
                return

        now = time.monotonic()
        if edge != self._push_edge or now - self._push_started > self.cfg["push_ms"] / 1000.0:
            self._push = 0.0
            self._push_edge = edge
            self._push_started = now
        self._push += pressing[edge]
        if self._push < float(self.cfg["push_px"]):
            return
        self._push = 0.0
        if self.cfg["edge_only_with_hotkey"]:
            return
        where = "home" if self.active is not None else f"the {edge}"
        self.log(f"pushed off the {edge} edge - crossing to {where}")
        self.go_local() if self.active is not None else self.go_to(self.peer_on(edge))

    # ------------------------------------------------------------ the loop
    def _handle(self, reader):
        events = reader.read()
        if events is None:
            return
        pad = self._pads.get(reader.info.path)
        for etype, code, value in events:
            if pad is not None:
                for out in pad.feed(etype, code, value):
                    self._consume(*out)
                continue
            self._consume(etype, code, value)
        self._flush()

    def _consume(self, etype, code, value):
        if etype == EV_KEY and code < 0x100 and self.hotkeys.feed(code, value):
            return
        if etype == EV_REL and code in (REL_X, REL_Y):
            self._move(value, 0) if code == REL_X else self._move(0, value)
        if self.active is not None:
            self._batch.append((etype, code, value))

    def _flush(self):
        if self._batch and self.active is not None:
            self.active.send(protocol.pack_events(self._batch))
        self._batch.clear()

    def _watchdog(self):
        """Whatever else goes wrong, the computer you are sitting at gets its
        mouse and keyboard back. If the loop that reads them stops turning for
        a few seconds, let go of everything."""
        released = False
        while self.running:
            time.sleep(1.0)
            stalled = time.monotonic() - self._alive_at > WATCHDOG_SECONDS
            if stalled and not released and any(r.grabbed for r in self._readers.values()):
                released = True
                self.log("stopped responding - letting go of the mouse and keyboard")
                for reader in list(self._readers.values()):
                    reader.ungrab()
            elif not stalled:
                released = False

    def run(self):
        self.running = True
        self._scan_devices()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if self.cfg["share_clipboard"]:
            self.clipboard.start()
        if self.cfg["discovery"]:
            self.announcer.start()
        self.publish()
        self.log(f"ready - screen {self.width}x{self.height}")
        last_scan = last_cfg = last_command = time.monotonic()
        try:
            while self.running:
                for key, _ in self._selector.select(timeout=0.2):
                    self._handle(key.data)
                now = time.monotonic()
                self._alive_at = now
                if now - last_scan > RESCAN_SECONDS:
                    last_scan = now
                    self._scan_devices()
                if now - last_cfg > 1.0:
                    last_cfg = now
                    self._reload_config()
                if now - last_command > 0.2:
                    last_command = now
                    self._take_command()
                self._watch_the_peer(now)
        finally:
            self.stop()

    def _watch_the_peer(self, now):
        """While your mouse is on another machine, that machine must keep
        answering. If it stops, the mouse comes back here rather than being
        typed into nothing."""
        if now - self._last_ping > HEARTBEAT_SECONDS:
            self._last_ping = now
            ping = protocol.pack(protocol.PING, b"")
            with self._peers_lock:
                everyone = list(self.peers.values())
            for other in everyone:
                other.send(ping)

        peer = self.active
        if peer is None:
            return
        if not peer.alive:
            self.log(f"{peer.name} is not keeping up - bringing the pointer back")
            self.go_local()
            self._drop(peer)
            return
        if now - peer.heard > SILENCE_SECONDS:
            self.log(f"{peer.name} has gone quiet - bringing the pointer back")
            self.go_local()
            self._drop(peer)

    def _take_command(self):
        command = status.take()
        if command is None:
            return
        if command == "home":
            self.go_local()
        elif command.startswith("goto:"):
            with self._peers_lock:
                peer = self.peers.get(command[5:])
            self.go_to(peer)
        elif command == "next":
            self.cycle()
        elif command.startswith("drop:"):
            with self._peers_lock:
                peer = self.peers.get(command[5:])
            if peer is not None:
                self._drop(peer)

    def _reload_config(self):
        mtime = config.mtime()
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        fresh = config.load()
        self.cfg.update(fresh)
        with self._peers_lock:
            for peer in self.peers.values():
                entry = fresh.get("peers", {}).get(peer.id)
                if entry and entry.get("edge") in EDGES:
                    peer.edge = entry["edge"]

    def stop(self):
        self.running = False
        status.clear()
        self.clipboard.stop()
        self.announcer.stop()
        self._grab_all(False)
        for reader in list(self._readers.values()):
            reader.close()
        self._readers.clear()

    # --------------------------------------------------------------- network
    def _broadcast_clipboard(self, text, skip=None):
        blob = protocol.pack(protocol.CLIPBOARD, text.encode("utf-8"))
        with self._peers_lock:
            targets = [p for p in self.peers.values() if p.id != skip]
        for peer in targets:
            peer.send(blob)

    def _free_edge(self):
        taken = {p.edge for p in self.peers.values()}
        taken |= {e.get("edge") for e in self.cfg.get("peers", {}).values()}
        for edge in (self.cfg["peer_edge"],) + EDGES:
            if edge not in taken:
                return edge
        return self.cfg["peer_edge"]

    def _drop(self, peer):
        with self._peers_lock:
            if self.peers.get(peer.id) is not peer:
                return
            del self.peers[peer.id]
        if self.active is peer:
            self.active = None
            self._grab_all(False)
        peer.close()
        self.log(f"{peer.name} disconnected")
        self.publish()

    def _accept_loop(self):
        ctx = net.server_context()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", int(self.cfg["port"])))
        listener.listen(8)
        self.log(f"listening on port {self.cfg['port']}")
        while self.running:
            try:
                raw, addr = listener.accept()
            except OSError:
                return
            net.tune(raw)
            threading.Thread(target=self._greet, args=(ctx, raw, addr), daemon=True).start()

    def _greet(self, ctx, raw, addr):
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                           struct.pack("ll", 5, 0))
            conn = ctx.wrap_socket(raw, server_side=True)
            hello = self._read_hello(conn)
        except (OSError, ssl.SSLError) as exc:
            self.log(f"rejected connection from {addr[0]}: {exc}")
            return
        if hello is None:
            try:
                conn.close()
            except OSError:
                pass
            return

        ident = hello["id"]
        name = hello.get("name") or addr[0]
        known = self.cfg.setdefault("peers", {})
        entry = known.get(ident)
        if entry is None:
            entry = {"name": name, "edge": self._free_edge(), "enabled": True}
            known[ident] = entry
            config.save(self.cfg)
            self._cfg_mtime = config.mtime()
            self.log(f"new machine allowed: {name} (on the {entry['edge']})")
        elif entry.get("name") != name:
            entry["name"] = name
            config.save(self.cfg)
            self._cfg_mtime = config.mtime()

        peer = Peer(ident, name, conn, (int(hello.get("width", 1920)),
                                        int(hello.get("height", 1080))), entry["edge"])
        with self._peers_lock:
            old = self.peers.get(ident)
            self.peers[ident] = peer
        if old is not None:
            old.close()
        conn.sendall(protocol.pack_json(protocol.HELLO, {
            "width": self.width, "height": self.height,
            "token": self.cfg["token"], "name": discovery.machine_name(),
            "id": discovery.machine_id(),
        }))
        self.log(f"connected: {name} at {addr[0]} on the {peer.edge}")
        self.publish()
        self._read_loop(peer)

    def _read_hello(self, conn):
        conn.settimeout(10)
        framer = protocol.Framer()
        while True:
            data = conn.recv(4096)
            if not data:
                return None
            for kind, body in framer.feed(data):
                if kind != protocol.HELLO:
                    return None
                hello = json.loads(body.decode("utf-8"))
                if not hello.get("id"):
                    return None
                ok = hello.get("token") == self.cfg["token"]
                if not ok and config.pairing_open():
                    ok = True
                    self.log(f"pairing window: letting {hello.get('name')} in")
                if not ok:
                    self.log(f"refused {hello.get('name')}: not paired with this machine")
                    return None
                conn.settimeout(None)
                return hello

    def _read_loop(self, peer):
        framer = protocol.Framer()
        while self.running:
            try:
                data = peer.conn.recv(65536)
            except OSError:
                data = b""
            if not data:
                self._drop(peer)
                return
            peer.heard = time.monotonic()
            for kind, body in framer.feed(data):
                if kind == protocol.CLIPBOARD and self.cfg["share_clipboard"]:
                    text = body.decode("utf-8", "replace")
                    self.clipboard.apply(text)
                    self._broadcast_clipboard(text, skip=peer.id)
                elif kind == protocol.LEAVE and self.active is peer:
                    self.go_local()
