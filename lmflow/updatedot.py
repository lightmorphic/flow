"""The update dot itself - the only update control in the app."""
from __future__ import annotations

import math
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk                   # noqa: E402

from . import __version__, updater as up              # noqa: E402

# The house palette: green #4BAE4F, amber #FFC006, blue #2295F1, red #F34236.
COLOURS = {
    up.UP_TO_DATE: (0.294, 0.682, 0.310),
    up.AVAILABLE: (1.000, 0.753, 0.024),
    up.READY: (0.133, 0.584, 0.945),
    up.OFFLINE: (0.953, 0.259, 0.212),
}
RING_TRACK = (0.631, 0.631, 0.667, 0.55)
TEXT_SIZE = 12
PAD = 4          # breathing room around the dot, not part of its diameter


class UpdateDot(Gtk.Box):
    """The version number, then the one dot. Nothing else."""

    def __init__(self, log=print):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_halign(Gtk.Align.END)
        self.set_margin_end(16)
        self.set_margin_bottom(12)
        self.set_margin_top(2)
        self._diameter = TEXT_SIZE * 2
        self.set_valign(Gtk.Align.CENTER)
        self.log = log

        self.label = Gtk.Label(valign=Gtk.Align.CENTER, use_markup=True)
        self.label.set_markup(
            f'<a href="{up.WEBSITE}"><span size="{TEXT_SIZE * 1000}" '
            f'underline="none">{__version__}</span></a>')
        # The name in the header bar links to the same place; see gui.py.
        self.label.add_css_class("dim")
        self.label.set_tooltip_text(up.WEBSITE)
        self.append(self.label)

        self.area = Gtk.DrawingArea(valign=Gtk.Align.CENTER)
        self.area.set_draw_func(self._draw)
        self.append(self.area)
        # The spec asks for a dot twice the height of the text next to it, so
        # measure the label rather than guessing from the font size.
        self.label.connect("realize", self._size_the_dot)
        self._size_the_dot(self.label)

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

    def _size_the_dot(self, label):
        _minimum, natural, _mb, _nb = label.measure(Gtk.Orientation.VERTICAL, -1)
        text_height = natural or TEXT_SIZE * 4 // 3
        self._diameter = text_height * 2
        box = self._diameter + PAD          # room for the antialiased edge
        self.area.set_content_width(box)
        self.area.set_content_height(box)

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
        radius = self._diameter / 2

        if self.state == up.DOWNLOADING:
            line = max(2.0, radius * 0.18)
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
        alpha = 1.0
        if self._pulsing:
            alpha = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(self._pulse))
        cr.set_source_rgba(red, green, blue, alpha)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
