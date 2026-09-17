"""The tray icon: where the pointer is, and how to send it somewhere else."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk               # noqa: E402

# Tell the desktop which application this is. Without it the menu's own
# window shows up in the dock as an unnamed extra with a placeholder icon,
# appearing and disappearing as the menu comes and goes.
APP_ID = "uk.lightmorph.Flow"
GLib.set_prgname(APP_ID)
GLib.set_application_name("Lightmorphic Flow")

from . import __version__, config, permissions, status   # noqa: E402

ICON_HOME = "uk.lightmorph.Flow"
ICON_AWAY = "uk.lightmorph.Flow-away"
POLL_MS = 2000
SERVICE_CACHE_SECONDS = 5.0

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


def _only_one():
    """Hold a lock for as long as we run, so two ways of starting the tray
    cannot leave two icons in the bar. Returns the open file, or None.

    Opened without truncating, and retried once: two copies started in the
    same instant should not both give up.
    """
    path = os.path.join(config.CONFIG_DIR, "tray.lock")
    os.makedirs(config.CONFIG_DIR, mode=0o700, exist_ok=True)
    handle = open(path, "a+", encoding="ascii")
    for attempt in range(2):
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if attempt == 0:
                time.sleep(0.6)
                continue
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
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
        try:
            self.indicator.set_icon_theme_path("/usr/share/icons/hicolor")
        except (AttributeError, TypeError):
            pass

        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._signature = None
        self._icon_now = None
        self._service_checked = 0.0
        self._service_was = False
        self._rebuild(status.read())
        GLib.timeout_add(POLL_MS, self._tick)

    # ----------------------------------------------------------------- menu
    def _tick(self):
        state = status.read()
        signature = (state["running"], bool(state["active"]),
                     permissions.state(),
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
        trouble = permissions.message()
        if trouble is not None:
            headline = trouble[0]
        elif not running:
            headline = "Sharing is off"
        elif active:
            headline = f"Pointer on {active['name']}"
        else:
            headline = "Pointer on this computer"
        wanted = ICON_AWAY if active else ICON_HOME
        if (wanted, headline) != self._icon_now:
            self._icon_now = (wanted, headline)
            self.indicator.set_icon_full(wanted, headline)

        head = Gtk.MenuItem(label=headline)
        head.set_sensitive(False)
        self.menu.append(head)
        self.menu.append(Gtk.SeparatorMenuItem())

        if trouble is not None:
            why = Gtk.MenuItem(label="What do I need to do?")
            why.connect("activate", self._open_settings)
            self.menu.append(why)
            self.menu.append(Gtk.SeparatorMenuItem())

        peers = state.get("peers", [])
        if trouble is not None:
            pass
        elif running and peers:
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

            for peer in peers:
                drop = Gtk.MenuItem(label=f"Disconnect {peer['name']}")
                drop.connect("activate", self._drop, peer["id"])
                self.menu.append(drop)
            self.menu.append(Gtk.SeparatorMenuItem())
        elif running:
            none = Gtk.MenuItem(label="No other computer connected")
            none.set_sensitive(False)
            self.menu.append(none)
            self.menu.append(Gtk.SeparatorMenuItem())

        connect = Gtk.MenuItem(label="Connect")
        connect.connect("activate", lambda _w: self._set_running(True))
        connect.set_sensitive(not running and trouble is None)
        self.menu.append(connect)

        disconnect = Gtk.MenuItem(label="Disconnect")
        disconnect.connect("activate", lambda _w: self._set_running(False))
        disconnect.set_sensitive(running)
        self.menu.append(disconnect)

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

    def _service_active(self, fresh=False):
        now = time.monotonic()
        if not fresh and now - self._service_checked < SERVICE_CACHE_SECONDS:
            return self._service_was
        self._service_checked = now
        result = _systemctl("is-active", self._unit())
        self._service_was = bool(result) and result.stdout.strip() == "active"
        return self._service_was

    def _drop(self, _widget, peer_id):
        status.send(f"drop:{peer_id}")

    def _set_running(self, wanted):
        if wanted == self._service_active(fresh=True):
            return
        _systemctl("start" if wanted else "stop", self._unit())
        self._service_checked = 0.0
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
    lock = _only_one()
    if lock is None:
        print("The tray icon is already running.", file=sys.stderr)
        return 0
    try:
        # Held in a name on purpose: dropped on the floor, Python collects it
        # and the icon disappears the instant it is made.
        tray = Tray()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Lightmorphic Flow {__version__}: tray icon running", flush=True)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    del tray
    lock.close()
    return 0
