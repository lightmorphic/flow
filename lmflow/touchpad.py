"""Turn touchpad absolute finger positions into ordinary mouse movement.

Only used while the pointer is on the other machine; locally the touchpad is
never touched, so GNOME keeps all of its gestures.
"""
from __future__ import annotations

from .linux_input import (ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_SLOT,
                          ABS_MT_TRACKING_ID, BTN_TOOL_DOUBLETAP, EV_ABS, EV_KEY,
                          EV_REL, REL_WHEEL, REL_X, REL_Y)

SCROLL_DIVISOR = 45.0


class TouchpadTranslator:
    def __init__(self, scale=1.0):
        self.scale = scale
        self.slot = 0
        self.pos = {}
        self.active = set()
        self.two_finger = False
        self._scroll_acc = 0.0
        self._pending = {}

    def feed(self, etype, code, value):
        """Returns a list of (type, code, value) mouse events, possibly empty."""
        if etype == EV_KEY:
            if code == BTN_TOOL_DOUBLETAP:
                self.two_finger = bool(value)
                self._scroll_acc = 0.0
                return []
            if 0x110 <= code <= 0x117:
                return [(EV_KEY, code, value)]
            return []

        if etype != EV_ABS:
            return []

        if code == ABS_MT_SLOT:
            self.slot = value
            return []
        if code == ABS_MT_TRACKING_ID:
            if value == -1:
                self.active.discard(self.slot)
                self.pos.pop(self.slot, None)
            else:
                self.active.add(self.slot)
            return []
        if code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
            axis = "x" if code == ABS_MT_POSITION_X else "y"
            prev = self.pos.get(self.slot, {})
            delta = value - prev[axis] if axis in prev else 0
            prev[axis] = value
            self.pos[self.slot] = prev
            if self.slot != min(self.active, default=self.slot):
                return []
            if self.two_finger:
                if axis == "y" and delta:
                    self._scroll_acc -= delta / SCROLL_DIVISOR
                    ticks = int(self._scroll_acc)
                    if ticks:
                        self._scroll_acc -= ticks
                        return [(EV_REL, REL_WHEEL, ticks)]
                return []
            if delta:
                rel = REL_X if axis == "x" else REL_Y
                moved = int(delta * self.scale)
                if moved:
                    return [(EV_REL, rel, moved)]
        return []

    def reset(self):
        self.pos.clear()
        self.active.clear()
        self.two_finger = False
        self._scroll_acc = 0.0
