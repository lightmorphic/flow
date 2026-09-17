"""Raw Linux input: reading /dev/input/event*, writing /dev/uinput.

Pure standard library on purpose - the other machine needs nothing but Python 3.
"""
from __future__ import annotations

import fcntl
import os
import re
import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------- event codes
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC = 0x00, 0x01, 0x02, 0x03, 0x04
SYN_REPORT = 0
REL_X, REL_Y = 0, 1
REL_HWHEEL, REL_WHEEL = 6, 8
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 11, 12
ABS_X, ABS_Y = 0, 1
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2f, 0x35, 0x36, 0x39
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BTN_TOUCH = 0x14a
BTN_TOOL_FINGER, BTN_TOOL_DOUBLETAP = 0x145, 0x14d

KEY_A, KEY_SPACE = 30, 57
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_LEFTALT = 29, 42, 54, 56
KEY_RIGHTCTRL, KEY_RIGHTALT = 97, 100
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126

EVENT_FMT = "llHHi"          # timeval + type + code + value
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# ------------------------------------------------------------------- ioctls
def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (typ << 8) | nr

_UI = ord("U")
UI_DEV_CREATE = _ioc(0, _UI, 1, 0)
UI_DEV_DESTROY = _ioc(0, _UI, 2, 0)
UI_DEV_SETUP = _ioc(1, _UI, 3, 92)
UI_ABS_SETUP = _ioc(1, _UI, 4, 28)
UI_SET_EVBIT = _ioc(1, _UI, 100, 4)
UI_SET_KEYBIT = _ioc(1, _UI, 101, 4)
UI_SET_RELBIT = _ioc(1, _UI, 102, 4)
UI_SET_ABSBIT = _ioc(1, _UI, 103, 4)
EVIOCGRAB = _ioc(1, ord("E"), 0x90, 4)


class InputError(RuntimeError):
    pass


# ------------------------------------------------------------ device listing
@dataclass
class DeviceInfo:
    path: str
    name: str
    kind: str                       # "mouse" | "keyboard" | "touchpad"
    handlers: list = field(default_factory=list)


def _bits(value: str) -> int:
    """/proc bitmask words ('0 7 ffff') -> one integer."""
    total = 0
    for i, word in enumerate(reversed(value.split())):
        total |= int(word or "0", 16) << (64 * i)
    return total


def list_devices() -> list:
    """Classify every input device from /proc/bus/input/devices."""
    out = []
    try:
        blob = open("/proc/bus/input/devices", encoding="utf-8", errors="replace").read()
    except OSError as exc:                                   # pragma: no cover
        raise InputError(f"cannot read /proc/bus/input/devices: {exc}") from exc

    for block in blob.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        handlers = []
        ev = key = rel = abs_ = 0
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
            elif line.startswith("B: EV="):
                ev = _bits(line.split("=", 1)[1])
            elif line.startswith("B: KEY="):
                key = _bits(line.split("=", 1)[1])
            elif line.startswith("B: REL="):
                rel = _bits(line.split("=", 1)[1])
            elif line.startswith("B: ABS="):
                abs_ = _bits(line.split("=", 1)[1])

        node = next((h for h in handlers if h.startswith("event")), None)
        if not node:
            continue
        path = "/dev/input/" + node
        has_rel_xy = bool(rel & (1 << REL_X)) and bool(rel & (1 << REL_Y))
        has_letters = bool(key & (1 << KEY_A)) and bool(key & (1 << KEY_SPACE))
        has_mt = bool(abs_ & (1 << ABS_MT_POSITION_X))

        if has_mt and (key & (1 << BTN_TOOL_FINGER)):
            kind = "touchpad"
        elif has_rel_xy and (key & (1 << BTN_LEFT)):
            kind = "mouse"
        elif has_letters and (ev & (1 << EV_KEY)):
            kind = "keyboard"
        else:
            continue
        out.append(DeviceInfo(path=path, name=name, kind=kind, handlers=handlers))
    return out


# -------------------------------------------------------------- reading them
class InputReader:
    """One opened /dev/input/eventN, optionally grabbed exclusively."""

    def __init__(self, info: DeviceInfo):
        self.info = info
        self.fd = os.open(info.path, os.O_RDONLY | os.O_NONBLOCK)
        self.grabbed = False
        self._buf = b""

    def grab(self):
        if not self.grabbed:
            fcntl.ioctl(self.fd, EVIOCGRAB, struct.pack("i", 1))
            self.grabbed = True

    def ungrab(self):
        if self.grabbed:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, struct.pack("i", 0))
            except OSError:
                pass
            self.grabbed = False

    def read(self):
        """Yield (type, code, value) tuples currently available."""
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return []
        except OSError:
            return None                       # device went away
        self._buf += data
        events = []
        while len(self._buf) >= EVENT_SIZE:
            chunk, self._buf = self._buf[:EVENT_SIZE], self._buf[EVENT_SIZE:]
            _s, _us, etype, code, value = struct.unpack(EVENT_FMT, chunk)
            events.append((etype, code, value))
        return events

    def close(self):
        self.ungrab()
        try:
            os.close(self.fd)
        except OSError:
            pass


# -------------------------------------------------------------- writing them
ABS_RANGE = 32767          # the scale absolute coordinates are sent on


class VirtualDevice:
    """A uinput device. Keyboard and pointer are kept separate so that
    libinput classifies each of them the way we want.

    An absolute pointer is the same shape as the mouse a virtual machine
    offers its guest: it reports where the pointer IS rather than how far it
    has moved. That matters because the desktop applies its own acceleration
    to relative movement, so the real pointer and our idea of where it is
    drift apart - and then the pointer cannot find its way back to the edge
    it came in by.
    """

    KEY_RANGES = ((1, 255), (352, 542))
    BUTTONS = tuple(range(0x110, 0x118))
    RELS = (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES)

    def __init__(self, name: str, pointer: bool, absolute: bool = False):
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise InputError(
                "no permission to use /dev/uinput. If you have just installed "
                "Lightmorphic Flow, log out and back in once - your session is "
                "still carrying the old group list."
            ) from exc

        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
        if pointer:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            # An absolute pointer must not also offer relative movement, or the
            # desktop treats it as an ordinary mouse and ignores the positions.
            # Wheels stay: that is the shape a virtual machine's mouse has.
            rels = (REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES) \
                if absolute else self.RELS
            for code in rels:
                fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
            for code in self.BUTTONS:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            if absolute:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                for code in (ABS_X, ABS_Y):
                    fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
                    # code, then value/min/max/fuzz/flat/resolution
                    setup = struct.pack("Hxx6i", code, 0, 0, ABS_RANGE, 0, 0, 0)
                    fcntl.ioctl(self.fd, UI_ABS_SETUP, setup)
        else:
            for lo, hi in self.KEY_RANGES:
                for code in range(lo, hi + 1):
                    fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)

        setup = struct.pack("HHHH80sI", 0x03, 0x1d1e, 0x0001, 0x0001,
                            name.encode()[:79], 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def emit(self, events):
        """events: iterable of (type, code, value); a SYN is appended."""
        blob = b""
        for etype, code, value in events:
            blob += struct.pack(EVENT_FMT, 0, 0, etype, code, value)
        blob += struct.pack(EVENT_FMT, 0, 0, EV_SYN, SYN_REPORT, 0)
        try:
            os.write(self.fd, blob)
        except OSError:
            pass

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
