"""The same speed-up curve the desktop applies to a mouse.

On your own screen Flow cannot see where the pointer really is, so it keeps
its own count - and that count is only right if it speeds movement up exactly
as the desktop does. A flat multiplier is wrong both ways: too small and a
fast sweep never reaches the edge in Flow's count; too large and a slow,
careful approach reaches it a quarter of a screen early.

This follows libinput's curve for mice - slow movement is slowed down, a
middle band moves one to one, faster movement is sped up to a cap - tuned by
the user's own speed setting, as GNOME and Cinnamon pass it to libinput.
"""
from __future__ import annotations

import subprocess

DEFAULT_DPI = 1000


def _gsetting(schema, key):
    try:
        out = subprocess.run(["gsettings", "get", schema, key],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip().strip("'")


def desktop_settings():
    """(speed -1..1, profile 'adaptive' or 'flat') from the desktop, or defaults."""
    for schema in ("org.gnome.desktop.peripherals.mouse",
                   "org.cinnamon.desktop.peripherals.mouse"):
        speed = _gsetting(schema, "speed")
        if speed is None:
            continue
        profile = _gsetting(schema, "accel-profile") or "default"
        try:
            value = max(-1.0, min(1.0, float(speed)))
        except ValueError:
            value = 0.0
        return value, ("flat" if profile == "flat" else "adaptive")
    return 0.0, "adaptive"


def device_dpi(path):
    """The mouse's resolution if the system knows it, otherwise 1000."""
    try:
        out = subprocess.run(["udevadm", "info", "--query=property", "--name", path],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return DEFAULT_DPI
    for line in out.splitlines():
        if line.startswith("MOUSE_DPI="):
            first = line.split("=", 1)[1].split()[0]
            first = first.lstrip("*").split("@")[0]
            try:
                return max(100, int(first))
            except ValueError:
                break
    return DEFAULT_DPI


class Curve:
    def __init__(self, speed=0.0, profile="adaptive"):
        self.set(speed, profile)

    def set(self, speed, profile):
        self.speed = speed
        self.profile = profile
        self.threshold = max(0.2, 0.4 - 0.25 * speed)      # units per ms
        self.incline = 1.1 + 0.75 * speed
        self.cap = 2.0 + 1.5 * speed
        self.flat = max(0.005, 1.0 + speed)

    def factor(self, velocity):
        """velocity in 1000-dpi units per millisecond."""
        if self.profile == "flat":
            return self.flat
        if velocity < 0.07:
            factor = 10.0 * velocity + 0.3
        elif velocity < self.threshold:
            factor = 1.0
        else:
            factor = self.incline * (velocity - self.threshold) + 1.0
        return min(self.cap, factor)


class Tracker:
    """Speed of one mouse, smoothed across a few reports."""

    def __init__(self, dpi=DEFAULT_DPI):
        self.scale = DEFAULT_DPI / float(dpi)
        self.last = None
        self.velocity = 0.0

    def move(self, curve, dx, dy, when):
        """Returns the movement as the desktop would show it, in pixels."""
        ndx, ndy = dx * self.scale, dy * self.scale
        distance = (ndx * ndx + ndy * ndy) ** 0.5
        if self.last is None:
            gap = None
        else:
            gap = (when - self.last) * 1000.0            # milliseconds
        self.last = when
        if gap is None or gap > 100.0 or gap <= 0.0:
            speed = distance / 8.0                        # a fresh start: assume ~8 ms
            self.velocity = speed
        else:
            speed = distance / max(1.0, gap)
            self.velocity = 0.5 * self.velocity + 0.5 * speed
        k = curve.factor(self.velocity)
        return ndx * k, ndy * k
