"""Hotkey parsing and matching against raw key codes."""
from __future__ import annotations

from .linux_input import (KEY_LEFTALT, KEY_LEFTCTRL, KEY_LEFTMETA, KEY_LEFTSHIFT,
                          KEY_RIGHTALT, KEY_RIGHTCTRL, KEY_RIGHTMETA, KEY_RIGHTSHIFT)

MODIFIERS = {
    "ctrl": (KEY_LEFTCTRL, KEY_RIGHTCTRL),
    "shift": (KEY_LEFTSHIFT, KEY_RIGHTSHIFT),
    "alt": (KEY_LEFTALT, KEY_RIGHTALT),
    "super": (KEY_LEFTMETA, KEY_RIGHTMETA),
}

_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_LETTER_CODES = [30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50,
                 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44]
KEYS = {name: code for name, code in zip(_LETTERS, _LETTER_CODES)}
KEYS.update({str(d): c for d, c in zip("1234567890", range(2, 12))})
KEYS.update({f"f{i}": c for i, c in zip(range(1, 11), range(59, 69))})
KEYS.update({"f11": 87, "f12": 88, "esc": 1, "escape": 1, "space": 57,
             "tab": 15, "enter": 28, "scrolllock": 70, "pause": 119,
             "insert": 110, "delete": 111, "home": 102, "end": 107})


def parse(spec: str):
    """'ctrl+alt+s' -> (frozenset of modifier names, key code) or None."""
    if not spec:
        return None
    mods, key = set(), None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in MODIFIERS:
            mods.add(part)
        elif part in KEYS:
            key = KEYS[part]
        else:
            return None
    if key is None:
        return None
    return frozenset(mods), key


class HotkeyWatcher:
    """Tracks which modifiers are held and fires callbacks on matching presses."""

    def __init__(self):
        self._held = set()
        self._bindings = []

    def bind(self, spec, callback):
        parsed = parse(spec)
        if parsed:
            self._bindings.append((parsed[0], parsed[1], callback))
        return bool(parsed)

    def feed(self, code: int, value: int) -> bool:
        """Returns True if the event was a registered hotkey press (swallow it)."""
        for name, codes in MODIFIERS.items():
            if code in codes:
                if value:
                    self._held.add(name)
                else:
                    self._held.discard(name)
                return False
        if value != 1:
            return False
        for mods, key, callback in self._bindings:
            if key == code and mods == self._held:
                callback()
                return True
        return False
