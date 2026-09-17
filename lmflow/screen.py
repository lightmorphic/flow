"""Best-effort screen size detection, with the config file as the final word."""
from __future__ import annotations

import subprocess


def detect():
    for probe in (_gdk, _xrandr):
        try:
            size = probe()
        except Exception:
            size = None
        if size and size[0] > 0 and size[1] > 0:
            return size
    return None


_GDK_SNIPPET = """
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk
Gtk.init()
d = Gdk.Display.get_default()
w = h = 0
mons = d.get_monitors()
for i in range(mons.get_n_items()):
    g = mons.get_item(i).get_geometry()
    w = max(w, g.x + g.width)
    h = max(h, g.y + g.height)
print(w, h)
"""


def _gdk():
    # In its own process: GTK aborts hard if there is no display to talk to.
    import sys
    res = subprocess.run([sys.executable, "-c", _GDK_SNIPPET],
                         capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        return None
    w, h = res.stdout.split()
    return (int(w), int(h))


def _xrandr():
    out = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=3).stdout
    for line in out.splitlines():
        if " connected" in line and "primary" in line:
            for token in line.split():
                if "x" in token and "+" in token:
                    size = token.split("+")[0]
                    w, h = size.split("x")
                    return (int(w), int(h))
    return None
