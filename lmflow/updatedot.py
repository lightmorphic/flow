"""The update dot itself - the only update control in the app."""
from __future__ import annotations

import math
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk                   # noqa: E402

from . import __version__, updater as up              # noqa: E402

COLOURS = {
    up.UP_TO_DATE: (0.29, 0.87, 0.50),
    up.AVAILABLE: (0.98, 0.75, 0.14),
    up.READY: (0.38, 0.65, 0.98),
    up.OFFLINE: (0.97, 0.44, 0.44),
}
RING_TRACK = (1, 1, 1, 0.22)
TEXT_SIZE = 12


class UpdateDot(Gtk.Box):
    """Name and version on the left, one dot on the right. Nothing else."""

    def __init__(self, log=print):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_halign(Gtk.Align.END)
        self.set_margin_end(18)
        self.set_margin_bottom(14)
        self.set_margin_top(4)
        self.log = log

        self.label = Gtk.Label(valign=Gtk.Align.CENTER, use_markup=True)
        self.label.set_markup(
            f'<a href="{up.WEBSITE}"><span size="{TEXT_SIZE * 1000}" '
            f'underline="none">Lightmorphic Flow {__version__}</span></a>')
        self.label.add_css_class("dim")
        self.append(self.label)

        size = TEXT_SIZE * 2 + 6                     # dot is twice the text height
        self.area = Gtk.DrawingArea(content_width=size, content_height=size,
                                    valign=Gtk.Align.CENTER)
        self.area.set_draw_func(self._draw)
        self.append(self.area)

        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.area.add_controller(click)

        self._pulse = 0.0
        self._pulsing = False
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
        if state != up.DOWNLOADING:
            self._stop_pulse()
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
        if self.state == up.UP_TO_DATE or self.state == up.OFFLINE:
            self._start_pulse()
            self.updater.check(manual=True)
        elif self.state == up.AVAILABLE:
            self.updater.download()
        elif self.state == up.READY:
            self._install()

    def _install(self):
        self._say("Installing")
        if not self.updater.install_and_restart():
            self._apply(up.OFFLINE, 0.0, "Could not install the update")
            return
        binary = "/usr/bin/lmflow"
        try:
            if os.path.exists(binary):
                os.execv(binary, [binary, "gui"])
            os.execv(sys.executable, [sys.executable, "-m", "lmflow", "gui"])
        except OSError as exc:
            self.log(f"restart failed: {exc}")

    # ------------------------------------------------------------- the pulse
    def _start_pulse(self):
        if self._pulsing:
            return
        self._pulsing = True
        self._pulse = 0.0
        GLib.timeout_add(33, self._tick_pulse)

    def _stop_pulse(self):
        self._pulsing = False

    def _tick_pulse(self):
        if not self._pulsing:
            self._pulse = 0.0
            self.area.queue_draw()
            return False
        self._pulse += 0.09
        self.area.queue_draw()
        return True

    # ------------------------------------------------------------- the paint
    def _draw(self, _area, cr, width, height):
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 2

        if self.state == up.DOWNLOADING:
            cr.set_line_width(2.5)
            cr.set_source_rgba(*RING_TRACK)
            cr.arc(cx, cy, radius - 1, 0, 2 * math.pi)
            cr.stroke()
            if self.progress > 0:
                cr.set_source_rgb(1, 1, 1)
                start = -math.pi / 2
                cr.arc(cx, cy, radius - 1, start,
                       start + 2 * math.pi * min(1.0, self.progress))
                cr.stroke()
            return

        red, green, blue = COLOURS.get(self.state, COLOURS[up.UP_TO_DATE])
        alpha = 1.0
        if self._pulsing:
            alpha = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(self._pulse))
        cr.set_source_rgba(red, green, blue, alpha)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
