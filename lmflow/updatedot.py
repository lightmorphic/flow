"""The update dot: the only update control in the app.

Sizes, position and pulse follow the house standard exactly - 12px version
number, a 16px dot, top right opposite the logo, one pulse a second.
"""
from __future__ import annotations

import math
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk               # noqa: E402

from . import __version__, updater as up               # noqa: E402

TEXT_PX = 12          # exact, not relative
DOT_PX = 16           # exact, never sized from the text
PAD = 4               # room for the antialiased edge, outside the diameter
PULSE_SECONDS = 1.0   # one pulse a second, like a heartbeat
PULSE_MINIMUM = 3.0   # a manual check pulses at least three times
FRAME_MS = 33

# The house palette: green #4BAE4F, amber #FFC006, blue #2295F1, red #F34236.
COLOURS = {
    up.UP_TO_DATE: (0.294, 0.682, 0.310),
    up.AVAILABLE: (1.000, 0.753, 0.024),
    up.READY: (0.133, 0.584, 0.945),
    up.OFFLINE: (0.953, 0.259, 0.212),
}
RING_TRACK = (0.631, 0.631, 0.667, 0.55)

CSS = b".flow-version { font-size: 12px; }"


class UpdateDot(Gtk.Box):
    """The version number, then the dot. Never the application name."""

    def __init__(self, log=print):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_valign(Gtk.Align.CENTER)
        self.log = log
        self.state = up.UP_TO_DATE
        self.progress = 0.0

        display = Gdk.Display.get_default()
        if display is not None:
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.label = Gtk.Label(valign=Gtk.Align.CENTER, use_markup=True)
        self.label.add_css_class("flow-version")
        # The app has a website, so the version number links to it.
        self.label.set_markup(
            f'<a href="{up.WEBSITE}" title="{up.WEBSITE}">{__version__}</a>')
        self.append(self.label)

        box = DOT_PX + PAD
        self.area = Gtk.DrawingArea(content_width=box, content_height=box,
                                    valign=Gtk.Align.CENTER)
        self.area.set_draw_func(self._draw)
        self.append(self.area)

        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.area.add_controller(click)

        self._pulse_from = None
        self._pulse_hold = False
        self._note_timer = None

        self.updater = up.Updater(self._on_state, log=log)
        self._apply(up.UP_TO_DATE, 0.0, None)
        GLib.timeout_add_seconds(2, self._first_check)
        GLib.timeout_add_seconds(up.CHECK_SECONDS, self._periodic)

    # ---------------------------------------------------------------- timing
    def _first_check(self):
        self.updater.check()
        return False

    def _periodic(self):
        self.updater.check()
        return True

    # ---------------------------------------------------------------- states
    def _on_state(self, state, progress, note):
        GLib.idle_add(self._apply, state, progress, note)

    def _apply(self, state, progress, note):
        self.state = state
        self.progress = progress
        if state == up.DOWNLOADING:
            self._stop_pulse()               # the tracing line shows progress
        else:
            self._pulse_hold = False         # let a minimum pulse finish
        if note:
            self._say(note, settle=True)
        else:
            self.area.set_tooltip_text(up.TOOLTIPS.get(state, ""))
        self.area.queue_draw()
        return False

    def _say(self, text, settle=False):
        self.area.set_tooltip_text(text)
        if self._note_timer:
            GLib.source_remove(self._note_timer)
            self._note_timer = None
        if settle:
            self._note_timer = GLib.timeout_add_seconds(3, self._settle)

    def _settle(self):
        self._note_timer = None
        self.area.set_tooltip_text(up.TOOLTIPS.get(self.state, ""))
        return False

    # ----------------------------------------------------------- the clicking
    def _clicked(self, _gesture, _n, _x, _y):
        if self.state in (up.UP_TO_DATE, up.OFFLINE):
            self._start_pulse(hold=True)
            self.updater.check(manual=True)
        elif self.state == up.AVAILABLE:
            self.updater.download()
        elif self.state == up.READY:
            self._install()

    def _install(self):
        self._say("installing")
        self._start_pulse(hold=True)         # until the app restarts
        GLib.idle_add(self._do_install)

    def _do_install(self):
        if not self.updater.install_and_restart():
            self._pulse_hold = False
            self._apply(up.OFFLINE, 0.0, "could not install the update")
            return False
        binary = "/usr/bin/lmflow"
        try:
            if os.path.exists(binary):
                os.execv(binary, [binary, "gui"])
            os.execv(sys.executable, [sys.executable, "-m", "lmflow", "gui"])
        except OSError as exc:
            self.log(f"restart failed: {exc}")
            self._pulse_hold = False
        return False

    # ------------------------------------------------------------- the pulse
    def _start_pulse(self, hold=False):
        self._pulse_hold = hold
        if self._pulse_from is not None:
            return
        self._pulse_from = time.monotonic()
        GLib.timeout_add(FRAME_MS, self._tick_pulse)

    def _stop_pulse(self):
        self._pulse_hold = False
        self._pulse_from = None
        self.area.queue_draw()

    def _tick_pulse(self):
        if self._pulse_from is None:
            return False
        elapsed = time.monotonic() - self._pulse_from
        if not self._pulse_hold and elapsed >= PULSE_MINIMUM:
            self._pulse_from = None
            self.area.queue_draw()
            return False
        self.area.queue_draw()
        return True

    def _alpha(self):
        if self._pulse_from is None:
            return 1.0
        turn = (time.monotonic() - self._pulse_from) / PULSE_SECONDS
        return 0.45 + 0.55 * (0.5 + 0.5 * math.cos(2 * math.pi * turn))

    # ------------------------------------------------------------- the paint
    def _draw(self, _area, cr, width, height):
        cx, cy = width / 2, height / 2
        radius = DOT_PX / 2

        if self.state == up.DOWNLOADING:
            line = max(2.0, radius * 0.22)
            edge = radius - line / 2
            cr.set_line_width(line)
            cr.set_source_rgba(*RING_TRACK)
            cr.arc(cx, cy, edge, 0, 2 * math.pi)
            cr.stroke()
            if self.progress > 0:
                cr.set_source_rgb(1, 1, 1)
                start = -math.pi / 2
                cr.arc(cx, cy, edge, start,
                       start + 2 * math.pi * min(1.0, self.progress))
                cr.stroke()
            return

        red, green, blue = COLOURS.get(self.state, COLOURS[up.UP_TO_DATE])
        cr.set_source_rgba(red, green, blue, self._alpha())
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
