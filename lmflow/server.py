"""The machine whose mouse and keyboard are being shared.

One machine may sit on each edge of this screen, so up to four at once.
"""
from __future__ import annotations

import errno
import fcntl
import hmac
import json
import queue
import selectors
import socket
import struct
import ssl
import threading
import time

from . import accel, config, discovery, net, protocol, screen, status
from .clipboard import Clipboard
from .hotkeys import HotkeyWatcher
from .linux_input import (ABS_RANGE, ABS_X, ABS_Y, EVIOCGRAB, EV_ABS, EV_KEY,
                          EV_REL, EV_SYN, InputError, InputReader, REL_X, REL_Y,
                          SYN_REPORT, VirtualDevice, list_devices)
from .touchpad import TouchpadTranslator

RESCAN_SECONDS = 3.0
HEARTBEAT_SECONDS = 2.0
WATCHDOG_SECONDS = 4.0
SILENCE_SECONDS = 8.0
EDGES = ("right", "left", "top", "bottom")
OPPOSITE = {"right": "left", "left": "right", "top": "bottom", "bottom": "top"}


def _scaled(x, y, width, height):
    """A screen position on the 0..32767 scale an absolute pointer speaks."""
    return (max(0, min(ABS_RANGE, int(x * ABS_RANGE / max(1, width - 1)))),
            max(0, min(ABS_RANGE, int(y * ABS_RANGE / max(1, height - 1)))))


def _free(reader):
    """True if nothing holds a grab on this device: take one and give it back."""
    try:
        fcntl.ioctl(reader.fd, EVIOCGRAB, 1)
    except OSError:
        return False
    try:
        fcntl.ioctl(reader.fd, EVIOCGRAB, 0)
    except OSError:
        return False
    return True


class Peer:
    """One connected machine.

    Everything is handed to a thread of its own to write. The loop that reads
    your mouse must never wait on the network: if the other machine stops
    reading, a direct send blocks for ever, and it blocks while your keyboard
    and mouse are held - which locks the computer you are sitting at.
    """

    OUTBOX = 512            # frames; a few seconds of furious mousing

    def __init__(self, ident, name, conn, size, edge, positions=True):
        self.id = ident
        self.name = name
        self.conn = conn
        self.size = size
        self.edge = edge
        self.positions = positions
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
        while self.alive:
            try:
                blob = self.outbox.get(timeout=1.0)
            except queue.Empty:
                continue
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
        self.curve = accel.Curve(*accel.desktop_settings())
        self._trackers = {}                    # device path -> speed tracker
        self._frames = {}                      # device path -> [dx, dy] this report
        self.active = None                     # None = this machine, else a Peer

        self._readers = {}
        self._pads = {}
        self._selector = selectors.DefaultSelector()
        self._batch = []
        self._moved = False
        self._dx = self._dy = 0.0
        self._press_dx = self._press_dy = 0.0
        self._last_place = None
        # Driving this machine's own pointer. GNOME on Wayland will not say
        # where the pointer is, and counting movement to guess it drifts - a
        # crossing half a screen early. So the mice are held all the time and
        # Flow places the pointer itself: then it always knows where it is.
        self.local_ptr = None
        self.local_press = None
        self._local_place = None
        self._local_batch = []
        self._readers_lock = threading.Lock()   # the watchdog looks in from outside
        self._stalled = False
        self._settings_tick = 0
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
        if info.name.startswith("Lightmorphic Flow") or info.name.startswith("Flow "):
            return False                   # our own, made to receive - never to read
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
            if info.kind in ("mouse", "touchpad"):
                self._trackers[info.path] = accel.Tracker(accel.device_dpi(info.path))
            self._selector.register(reader.fd, selectors.EVENT_READ, reader)
            if info.kind == "touchpad":
                self._pads[info.path] = TouchpadTranslator()
            if self.active is not None or self._drives_kind(info.kind):
                try:
                    reader.grab()
                except OSError as exc:
                    self.log(f"could not take {info.name} ({exc}); its movement "
                             "will be followed, not driven")
            self.log(f"device: {info.kind} {info.name}")

        for path in [p for p in self._readers if p not in seen]:
            with self._readers_lock:
                reader = self._readers.pop(path)
            try:
                self._selector.unregister(reader.fd)
            except (KeyError, ValueError):
                pass
            reader.close()
            self._pads.pop(path, None)
            self._trackers.pop(path, None)
            self.log(f"device gone: {reader.info.name}")

    def _drives_kind(self, kind):
        """Could a device of this kind be driving this pointer at all."""
        if self.local_ptr is None:
            return False
        return kind == "mouse" or (kind == "touchpad" and self.cfg["grab_touchpads"])

    def _driving(self):
        """Is any device actually held and driving it right now."""
        return any(r.grabbed and self._drives_kind(r.info.kind)
                   for r in self._readers.values())

    def _regrab_drivers(self):
        for reader in self._readers.values():
            if self._drives_kind(reader.info.kind) and not reader.grabbed:
                try:
                    reader.grab()
                except OSError:
                    pass

    def _open_local(self):
        if not self.cfg.get("drive_local_pointer", True):
            return
        try:
            self.local_ptr = VirtualDevice("Lightmorphic Flow local pointer",
                                           pointer=True, absolute=True)
            self.local_press = VirtualDevice("Lightmorphic Flow local pressure",
                                             pointer=True)
            time.sleep(0.3)
            self._place_local(force=True)
            self.log("driving this computer's pointer, so its edges are exact")
        except (OSError, InputError) as exc:
            self.local_ptr = self.local_press = None
            self.log(f"cannot drive this pointer ({exc}); following it instead")

    def _place_local(self, force=False):
        place = _scaled(self.x, self.y, self.width, self.height)
        if force or place != self._local_place:
            self._local_place = place
            return [(EV_ABS, ABS_X, place[0]), (EV_ABS, ABS_Y, place[1])]
        return []

    def _release_everything(self):
        """Close every device and open it again, ungrabbed.

        Asking the kernel to release a grab can fail quietly; closing the file
        cannot. This is the one step that must never be half-done - if it is,
        the computer you are sitting at keeps a mouse nothing is reading.
        """
        for path, reader in list(self._readers.items()):
            if self._drives_kind(reader.info.kind):
                continue                   # this machine's own pointer: still ours
            try:
                self._selector.unregister(reader.fd)
            except (KeyError, ValueError):
                pass
            reader.close()
            with self._readers_lock:
                self._readers.pop(path, None)
            self._pads.pop(path, None)
            self._trackers.pop(path, None)
        self.hotkeys.reset()               # a key-up may have been lost in the swap
        self._scan_devices()
        stuck = [r.info.name for r in self._readers.values()
                 if not self._drives_kind(r.info.kind) and not _free(r)]
        if stuck:
            self.log(f"WARNING still held by something: {', '.join(stuck)}")
        else:
            self.log(f"let go of {len(self._readers)} device(s), checked")

    def _grab_all(self, grab):
        done, failed = [], []
        for reader in self._readers.values():
            try:
                reader.grab() if grab else reader.ungrab()
                done.append(reader.info.name)
            except OSError as exc:
                why = ("something else is already holding it"
                       if getattr(exc, "errno", 0) == errno.EBUSY else str(exc))
                failed.append(f"{reader.info.name} ({why})")
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
        # Land well inside, not two pixels from the edge: coming home only
        # takes a nudge, and a pointer parked against the edge would be
        # nudged straight back by the first twitch of the hand.
        ix = max(40, rw // 20)
        iy = max(40, rh // 20)
        if peer.edge == "right":
            nx, ny = ix, int(self._frac_y() * rh)
        elif peer.edge == "left":
            nx, ny = rw - 1 - ix, int(self._frac_y() * rh)
        elif peer.edge == "top":
            nx, ny = int(self._frac_x() * rw), rh - 1 - iy
        else:
            nx, ny = int(self._frac_x() * rw), iy
        self.x, self.y = nx, ny
        was_local = self.active is None
        peer.heard = time.monotonic()      # it has not gone quiet: we just arrived
        self._last_place = None
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
        self._local_place = None
        self._press_dx = self._press_dy = 0.0
        self._release_everything()
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

    def _move(self, dx, dy, raw_dx=None, raw_dy=None):
        """dx, dy: movement as the desktop shows it. raw_dx, raw_dy: the hand's
        own movement, which is what a push is measured in. Called with raw
        movement alone (a touchpad, the tests), a fixed factor stands in for
        the desktop's curve."""
        if raw_dx is None:
            raw_dx, raw_dy = dx, dy
            speed = float(self.cfg["pointer_speed"] if self.active is not None
                          else self.cfg.get("local_edge_speed", 1.5))
            dx *= speed
            dy *= speed
        w, h = self._screen()
        want_x, want_y = self.x + dx, self.y + dy
        self.x = min(w - 1, max(0, want_x))
        self.y = min(h - 1, max(0, want_y))
        # What the edge swallowed. Passed on as real movement it is pressure
        # against that edge, which is what makes GNOME's hot corners and hot
        # edges fire - on the other machine, and on this one when Flow is
        # driving its pointer.
        self._press_dx += want_x - self.x
        self._press_dy += want_y - self.y

        pressing = {"right": raw_dx, "left": -raw_dx, "bottom": raw_dy, "top": -raw_dy}
        touching = {"right": self.x >= w - 1, "left": self.x <= 0,
                    "bottom": self.y >= h - 1, "top": self.y <= 0}

        # A mouse reports sideways and up-and-down movement as separate events.
        # While you push into an edge, the up-and-down part is neither pushing
        # nor pulling away, so it must not count against you. Throwing the push
        # away on every wobble is what made crossing feel like a wall.
        edge_now = self._push_edge
        if (edge_now is not None and touching.get(edge_now)
                and pressing.get(edge_now, 0) == 0):
            return

        if self.active is None:
            wanted = [e for e in EDGES
                      if touching[e] and pressing[e] > 0 and self.peer_on(e)]
        else:
            # Only the edge facing home takes you home. The other edges and
            # corners belong to the machine you are using - its own hot corners
            # and panels live there. Escape five times still brings you back
            # from anywhere.
            back = OPPOSITE[self.active.edge]
            wanted = [back] if (touching[back] and pressing[back] > 0) else []

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

        # Leaving home is deliberately hard, so a hot corner is never mistaken
        # for a crossing. Coming home must be easy: there is nothing to protect
        # on a machine you are only borrowing, and being stranded is the worst
        # thing that can happen.
        going_home = self.active is not None
        needed = float(self.cfg["return_push_px" if going_home else "push_px"])
        window = self.cfg["return_push_ms" if going_home else "push_ms"] / 1000.0

        now = time.monotonic()
        if edge != self._push_edge or now - self._push_started > window:
            self._push = 0.0
            self._push_edge = edge
        self._push_started = now           # measured from the latest push
        self._push += pressing[edge]
        if self._push < needed:
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
        path = reader.info.path
        pad = self._pads.get(path)
        tracker = self._trackers.get(path)
        for etype, code, value, when in events:
            if pad is not None:
                theirs = self.local_ptr is not None and not reader.grabbed
                for otype, ocode, ovalue in pad.feed(etype, code, value):
                    if otype == EV_REL and ocode in (REL_X, REL_Y):
                        if theirs and self.active is None:
                            continue
                        self._motion(tracker, ovalue if ocode == REL_X else 0,
                                     ovalue if ocode == REL_Y else 0, when)
                    else:
                        self._consume(otype, ocode, ovalue)
                continue
            if tracker is not None and etype == EV_REL and code in (REL_X, REL_Y):
                frame = self._frames.setdefault(path, [0, 0])
                frame[0 if code == REL_X else 1] += value
                continue
            if tracker is not None and etype == EV_SYN and code == SYN_REPORT:
                frame = self._frames.pop(path, None)
                if frame and (frame[0] or frame[1]):
                    self._motion(tracker, frame[0], frame[1], when)
            self._consume(etype, code, value)
        self._flush()

    def _motion(self, tracker, dx, dy, when):
        """One report's worth of movement from a mouse."""
        seen_x, seen_y = tracker.move(self.curve, dx, dy, when)
        if self.active is not None:
            factor = float(self.cfg["pointer_speed"])
            seen_x, seen_y = seen_x * factor, seen_y * factor
        self._move(seen_x, seen_y, raw_dx=dx, raw_dy=dy)
        self._dx += dx
        self._dy += dy
        self._moved = True

    def _consume(self, etype, code, value):
        if etype == EV_KEY and code < 0x100 and self.hotkeys.feed(code, value):
            return
        if etype == EV_REL and code in (REL_X, REL_Y):
            if code == REL_X:
                self._move(value, 0)
                self._dx += value
            else:
                self._move(0, value)
                self._dy += value
            self._moved = True
            return                      # sent as a position, not a movement
        if self.active is not None:
            self._batch.append((etype, code, value))
        elif self.local_ptr is not None and (
                (etype == EV_KEY and 0x110 <= code <= 0x117)
                or (etype == EV_REL and code not in (REL_X, REL_Y))):
            self._local_batch.append((etype, code, value))   # buttons and wheels

    def _flush_local(self):
        events = self._place_local() if self._moved else []
        events.extend(self._local_batch)
        self._local_batch.clear()
        if events:
            self.local_ptr.emit(events)
        push_x, push_y = int(round(self._press_dx)), int(round(self._press_dy))
        if push_x or push_y:
            self.local_press.emit([(EV_REL, REL_X, push_x), (EV_REL, REL_Y, push_y)])
        self._press_dx -= push_x
        self._press_dy -= push_y

    def _flush(self):
        peer = self.active
        if peer is None:
            self._batch.clear()
            if self._driving():
                self._flush_local()
            else:
                self._local_batch.clear()
                self._press_dx = self._press_dy = 0.0
            self._moved = False
            self._dx = self._dy = 0.0
            return
        events = []
        if self._moved and not peer.positions:
            events.append((EV_REL, REL_X, int(round(self._dx))))
            events.append((EV_REL, REL_Y, int(round(self._dy))))
            self._dx = self._dy = 0.0
            self._moved = False
        elif self._moved:
            # Where the pointer is, on the 0..32767 scale, so the other
            # machine's own mouse acceleration cannot pull the two apart.
            # Only sent when it has actually changed: pinned in a corner, a
            # re-sent position counts as a fresh placement and wipes out the
            # pressure GNOME is adding up to open the Overview.
            place = _scaled(self.x, self.y, *peer.size)
            if place != self._last_place:
                events.append((EV_ABS, ABS_X, place[0]))
                events.append((EV_ABS, ABS_Y, place[1]))
                self._last_place = place
            self._moved = False
        if peer.positions and (self._press_dx or self._press_dy):
            push_x, push_y = int(round(self._press_dx)), int(round(self._press_dy))
            if push_x or push_y:
                events.append((EV_REL, REL_X, push_x))
                events.append((EV_REL, REL_Y, push_y))
            self._press_dx -= push_x
            self._press_dy -= push_y
        events.extend(self._batch)
        self._batch.clear()
        if events:
            peer.send(protocol.pack_events(events))

    def _watchdog(self):
        """Whatever else goes wrong, the computer you are sitting at gets its
        mouse and keyboard back. If the loop that reads them stops turning for
        a few seconds, let go of everything."""
        released = False
        while self.running:
            time.sleep(1.0)
            stalled = time.monotonic() - self._alive_at > WATCHDOG_SECONDS
            if stalled and not released:
                with self._readers_lock:
                    held = [r for r in self._readers.values() if r.grabbed]
                    if held:
                        released = True
                        self._stalled = True
                        self.log("stopped responding - letting go of the mouse and keyboard")
                        for reader in held:
                            reader.ungrab()
            elif not stalled:
                released = False

    def run(self):
        self.running = True
        self._open_local()
        self._scan_devices()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if self.cfg["share_clipboard"]:
            self.clipboard.start()
        if self.cfg["discovery"]:
            self.announcer.start()
        self.publish()
        from . import __version__
        self.log(f"ready - Lightmorphic Flow {__version__}, "
                 f"screen {self.width}x{self.height}")
        last_scan = last_cfg = last_command = time.monotonic()
        try:
            while self.running:
                for key, _ in self._selector.select(timeout=0.2):
                    self._handle(key.data)
                now = time.monotonic()
                self._alive_at = now
                if self._stalled:
                    self._stalled = False
                    self.log("responding again - tidying up after the stall")
                    if self.active is not None:
                        self.go_local()        # never feed two machines at once
                    self._regrab_drivers()
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
        self._settings_tick += 1
        if self._settings_tick % 60 == 0:      # once a minute is plenty
            self.curve.set(*accel.desktop_settings())
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
        for dev in (self.local_ptr, self.local_press):
            if dev is not None:
                dev.close()
        self.local_ptr = self.local_press = None

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
            self._release_everything()
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

        ident = "".join(c for c in str(hello["id"]) if c.isalnum() or c in "-_")[:32]
        name = "".join(c for c in str(hello.get("name") or addr[0])
                       if c.isprintable())[:48].strip() or addr[0]
        with self._peers_lock:
            fresh = config.load()      # not a copy that may be a second stale
        known = fresh.setdefault("peers", {})
        entry = known.get(ident)
        if entry is None:
            entry = {"name": name, "edge": self._free_edge(), "enabled": True}
            known[ident] = entry
            config.save(fresh)
            self.cfg["peers"] = known
            self._cfg_mtime = config.mtime()
            self.log(f"new machine allowed: {name} (on the {entry['edge']})")
        elif entry.get("name") != name:
            entry["name"] = name
            config.save(fresh)
            self.cfg["peers"] = known
            self._cfg_mtime = config.mtime()

        peer = Peer(ident, name, conn,
                    (int(hello.get("width", 1920)), int(hello.get("height", 1080))),
                    entry["edge"],
                    positions=hello.get("wants", "position") != "movement")
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
        self.log(f"connected: {name} ({hello.get('version', '?')}) at {addr[0]} "
                 f"on the {peer.edge}; pointer sent as "
                 f"{'positions' if peer.positions else 'movements'}")
        self.publish()
        self._read_loop(peer)

    def _read_hello(self, conn):
        conn.settimeout(10)
        framer = protocol.Framer()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = conn.recv(4096)
            if not data:
                return None
            try:
                frames = framer.feed(data)
            except ValueError:
                return None
            for kind, body in frames:
                if kind != protocol.HELLO:
                    return None
                hello = json.loads(body.decode("utf-8"))
                if not hello.get("id"):
                    return None
                theirs = int(hello.get("protocol", 1))
                if theirs != protocol.VERSION:
                    from . import __version__
                    self.log(
                        f"{hello.get('name')} is running Lightmorphic Flow "
                        f"{hello.get('version', 'an older version')} and this "
                        f"machine is running {__version__}. They cannot work "
                        "together: update the older one.")
                    return None
                ok = hmac.compare_digest(str(hello.get("token", "")), self.cfg["token"])
                if not ok and config.pairing_open():
                    ok = True
                    config.close_pairing()     # one new machine, not everyone
                    self.log("pairing window: letting a new machine in, and closing")
                if not ok:
                    self.log(f"refused {hello.get('name')}: not paired with this machine")
                    return None
                conn.settimeout(None)
                return hello
        return None

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
            try:
                frames = framer.feed(data)
            except ValueError as exc:
                self.log(f"{peer.name}: {exc}")
                self._drop(peer)
                return
            for kind, body in frames:
                if kind == protocol.CLIPBOARD and self.cfg["share_clipboard"]:
                    text = body.decode("utf-8", "replace")
                    self.clipboard.apply(text)
                    self._broadcast_clipboard(text, skip=peer.id)
                elif kind == protocol.LEAVE and self.active is peer:
                    self.go_local()
