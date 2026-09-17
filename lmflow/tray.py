"""The tray icon: where the pointer is, and how to send it somewhere else."""
from __future__ import annotations

import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk               # noqa: E402

from . import __version__, config, status         # noqa: E402

ICON_HOME = "uk.lightmorph.Flow"
ICON_AWAY = "uk.lightmorph.Flow-away"
POLL_MS = 900

EDGE_SAY = {"right": "to the right", "left": "to the left",
            "top": "above", "bottom": "below"}


def _indicator_module():
    """Ayatana first, then the older name. Either is fine."""
    for namespace, version in (("AyatanaAppIndicator3", "0.1"),
                               ("AppIndicator3", "0.1")):
        try:
            gi.require_version(namespace, version)
            return __import__("gi.repository", fromlist=[namespace]).__dict__[namespace]
        except (ValueError, ImportError, KeyError):
            continue
    return None


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


class Tray:
    def __init__(self, log=print):
        self.log = log
        module = _indicator_module()
        if module is None:
            raise RuntimeError(
                "no tray library found - install gir1.2-ayatanaappindicator3-0.1")
        self.module = module
        self.indicator = module.Indicator.new(
            "lightmorphic-flow", ICON_HOME, module.IndicatorCategory.HARDWARE)
        self.indicator.set_status(module.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Lightmorphic Flow")

        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._signature = None
        self._rebuild(status.read())
        GLib.timeout_add(POLL_MS, self._tick)

    # ----------------------------------------------------------------- menu
    def _tick(self):
        state = status.read()
        signature = (state["running"], bool(state["active"]),
                     (state["active"] or {}).get("id"),
                     tuple((p["id"], p["name"], p["edge"]) for p in state["peers"]),
                     self._service_active())
        if signature != self._signature:
            self._signature = signature
            self._rebuild(state)
        return True

    def _rebuild(self, state):
        for child in self.menu.get_children():
            self.menu.remove(child)

        active = state.get("active")
        running = self._service_active()
        if not running:
            headline = "Sharing is off"
        elif active:
            headline = f"Pointer on {active['name']}"
        else:
            headline = "Pointer on this computer"
        self.indicator.set_icon_full(ICON_AWAY if active else ICON_HOME, headline)

        head = Gtk.MenuItem(label=headline)
        head.set_sensitive(False)
        self.menu.append(head)
        self.menu.append(Gtk.SeparatorMenuItem())

        peers = state.get("peers", [])
        if running and peers:
            for peer in peers:
                where = EDGE_SAY.get(peer.get("edge"), "")
                item = Gtk.MenuItem(label=f"Go to {peer['name']} ({where})")
                item.connect("activate", self._goto, peer["id"])
                item.set_sensitive(not (active and active["id"] == peer["id"]))
                self.menu.append(item)
            home = Gtk.MenuItem(label="Bring the pointer back here")
            home.connect("activate", lambda _w: status.send("home"))
            home.set_sensitive(bool(active))
            self.menu.append(home)
            self.menu.append(Gtk.SeparatorMenuItem())
        elif running:
            none = Gtk.MenuItem(label="No other computer connected")
            none.set_sensitive(False)
            self.menu.append(none)
            self.menu.append(Gtk.SeparatorMenuItem())

        toggle = Gtk.CheckMenuItem(label="Sharing")
        toggle.set_active(running)
        toggle.connect("toggled", self._toggle)
        self.menu.append(toggle)

        settings = Gtk.MenuItem(label="Settings…")
        settings.connect("activate", self._open_settings)
        self.menu.append(settings)

        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Hide this icon")
        quit_item.connect("activate", lambda _w: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()

    # -------------------------------------------------------------- actions
    def _goto(self, _widget, peer_id):
        status.send(f"goto:{peer_id}")

    def _unit(self):
        role = "server" if config.load().get("role") == "server" else "client"
        return f"lmflow-{role}.service"

    def _service_active(self):
        result = _systemctl("is-active", self._unit())
        return bool(result) and result.stdout.strip() == "active"

    def _toggle(self, widget):
        if widget.get_active() == self._service_active():
            return
        if widget.get_active():
            _systemctl("start", self._unit())
        else:
            _systemctl("stop", self._unit())
        self._signature = None

    def _open_settings(self, _widget):
        binary = "/usr/bin/lmflow"
        command = [binary, "gui"] if __import__("os").path.exists(binary) \
            else [sys.executable, "-m", "lmflow", "gui"]
        try:
            subprocess.Popen(command)
        except OSError as exc:
            self.log(f"could not open the settings window: {exc}")


def main():
    try:
        Tray()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Lightmorphic Flow {__version__}: tray icon running")
    Gtk.main()
    return 0
