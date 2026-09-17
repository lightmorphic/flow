"""Settings window. Every field saves itself; there is no save button."""
from __future__ import annotations

import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk          # noqa: E402

from . import (config, discovery, firewall, pairing, permissions,  # noqa: E402
               screen, status)
from .updatedot import UpdateDot                        # noqa: E402

EDGES = ["right", "left", "top", "bottom"]
EDGE_LABELS = ["To my right", "To my left", "Above me", "Below me"]
PAIR_SECONDS = 120
CSS = b"""
.dim { opacity: 0.65; }
"""


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Lightmorphic Flow",
                         default_width=580, default_height=780)
        self.cfg = config.load()
        self._rows = []
        self._code = None
        self._found_signature = None
        self._allow_row = None
        self._allow_button = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=""))
        header.pack_start(self._brand())
        try:
            header.pack_end(UpdateDot())   # opposite the logo
        except Exception as exc:           # noqa: BLE001
            print(f"update dot could not be created: {exc}", file=sys.stderr)
        box.append(header)

        # Adw.PreferencesPage scrolls itself; wrapping it in another scroller
        # nests two of them and neither ends up scrolling properly.
        self.page = Adw.PreferencesPage(vexpand=True)
        box.append(self.page)
        self.set_content(box)

        self.warning = Adw.PreferencesGroup()
        self._warning_rows = []
        self._permission_ok = True
        self._firewall_opened = False
        self.page.add(self.warning)
        self.page.add(self._group_role())
        self.machines = Adw.PreferencesGroup()
        self.page.add(self.machines)
        self.page.add(self._group_edges())
        self.page.add(self._group_extras())

        # If the background service is already listening, read what it found
        # rather than opening a second socket that would take half the traffic.
        self.listener = None
        self._own_listener()

        self._rebuild_machines()
        self._refresh_power()
        GLib.timeout_add_seconds(1, self._tick)
        GLib.idle_add(self._check_permission)
        GLib.idle_add(self._start_tray)

    def _start_tray(self):
        """The tray starts itself at login; if this is the first run since
        installing, nothing has started it yet."""
        result = _systemctl("is-active", "lmflow-tray.service")
        if result is not None and result.stdout.strip() == "active":
            return False
        started = _systemctl("start", "lmflow-tray.service")
        if started is not None and started.returncode == 0:
            return False
        try:                                  # no systemd session: run it directly
            binary = "/usr/bin/lmflow"
            command = [binary, "tray"] if os.path.exists(binary) else \
                [sys.executable, "-m", "lmflow", "tray"]
            subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            print(f"could not start the tray icon: {exc}", file=sys.stderr)
        return False

    # ------------------------------------------------------------ permission
    def _check_permission(self):
        """Nobody should meet this as a mystery. It is a dialog with an OK."""
        message = permissions.message()
        self._permission_ok = message is None
        self._rebuild_warning()
        if message is None:
            return False
        headline, body = message
        try:
            dialog = Adw.AlertDialog(heading=headline, body=body)
            dialog.add_response("ok", "OK")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present(self)
        except (AttributeError, TypeError):
            dialog = Adw.MessageDialog(transient_for=self, modal=True,
                                       heading=headline, body=body)
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present()
        return False

    def _rebuild_warning(self):
        """Whatever is standing in the way, said here until it is fixed."""
        for row in self._warning_rows:
            self.warning.remove(row)
        self._warning_rows = []

        if not getattr(self, "_permission_ok", True):
            headline, _body = permissions.message()
            row = Adw.ActionRow(title=headline,
                                subtitle="Until you do, it cannot read your mouse "
                                         "or keyboard. Click for the details.")
            row.set_subtitle_lines(3)
            button = Gtk.Button(label="Details", valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda _b: self._check_permission())
            row.add_suffix(button)
            row.set_activatable_widget(button)
            self.warning.add(row)
            self._warning_rows.append(row)

        kind = self._firewall_in_the_way()
        if kind:
            row = Adw.ActionRow(
                title="A firewall is in the way",
                subtitle=f"{kind} is running here, and this computer has to "
                         "accept the others reaching it. This opens the two "
                         "ports Lightmorphic Flow uses and nothing else. The "
                         "other computers need no change at all.")
            row.set_subtitle_lines(4)
            button = Gtk.Button(label="Allow it through", valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", self._open_firewall, kind)
            row.add_suffix(button)
            row.set_activatable_widget(button)
            self.warning.add(row)
            self._warning_rows.append(row)

        self.warning.set_title("Not working yet" if self._warning_rows else "")

    def _firewall_in_the_way(self):
        """Only the computer with the keyboard has to accept anything coming
        in. The controlled one only ever dials out, and a reply to its own
        question is let back through, so it never needs a firewall changed."""
        if self._firewall_opened or self.cfg["role"] != "server":
            return None
        if status.read().get("peers"):
            return None
        return firewall.running()

    def _open_firewall(self, button, kind):
        button.set_sensitive(False)
        button.set_label("Asking…")
        if firewall.open_it(kind):
            self._firewall_opened = True
        else:
            print(f"could not open the firewall; run: {firewall.spoken(kind)}",
                  file=sys.stderr)
        button.set_sensitive(True)
        button.set_label("Allow it through")
        self._rebuild_warning()

    def _own_listener(self):
        """Listen here only while the service is not."""
        daemon_listening = self.cfg["role"] == "client" and status.read().get("running")
        if daemon_listening:
            if self.listener is not None:
                self.listener.stop()
                self.listener = None
            return
        if self.listener is None:
            self.listener = discovery.Listener()
            self.listener.start()

    def _found(self):
        if self.listener is not None:
            return self.listener.found(role="server")
        return status.read().get("found", [])

    # ---------------------------------------------------------------- groups
    def _brand(self):
        """Logo and name, top left."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        display = Gdk.Display.get_default()
        theme = Gtk.IconTheme.get_for_display(display) if display else None
        name = "uk.lightmorph.Flow" if theme and theme.has_icon("uk.lightmorph.Flow") else "input-mouse-symbolic"
        icon = Gtk.Image.new_from_icon_name(name)
        icon.set_pixel_size(22)
        row.append(icon)
        title = Gtk.Label(label="Lightmorphic Flow", valign=Gtk.Align.CENTER)
        title.add_css_class("title-4")
        row.append(title)
        return row

    def _group_role(self):
        group = Adw.PreferencesGroup(title="This computer")
        self.power = Adw.SwitchRow(title="Sharing is on",
                                   subtitle="Runs in the background from now on")
        self.power.connect("notify::active", self._on_power)
        group.add(self.power)
        self.role = Adw.ComboRow(
            title="Role",
            subtitle="Share means the mouse and keyboard live here",
            model=Gtk.StringList.new(["Share my mouse and keyboard",
                                      "Be controlled by another computer"]))
        self.role.set_selected(0 if self.cfg["role"] == "server" else 1)
        self.role.connect("notify::selected", self._on_role)
        group.add(self.role)

        size = screen.detect()
        row = Adw.ActionRow(title=discovery.machine_name(),
                            subtitle=f"{size[0]} x {size[1]}" if size else "screen not detected")
        row.add_css_class("dim")
        group.add(row)
        return group

    def _group_edges(self):
        group = Adw.PreferencesGroup(
            title="Crossing over",
            description="A light touch on an edge or corner does nothing, so hot corners "
                        "and hot edges keep working. Push on and the pointer crosses.")
        self._spin(group, "Keep clear of corners", "corner_guard_px", 0, 400, 10,
                   "pixels at each end of an edge left alone")
        self._spin(group, "Push needed to cross", "push_px", 10, 400, 10,
                   "how far you keep pushing past the edge")
        self._spin(group, "Push time limit", "push_ms", 100, 2000, 50,
                   "milliseconds before a push is forgotten")

        row = Adw.SwitchRow(title="Only cross with the hotkey",
                            subtitle=f"{self.cfg['hotkey_switch']} steps through each computer",
                            active=bool(self.cfg["edge_only_with_hotkey"]))
        row.connect("notify::active",
                    lambda w, _p: self._save("edge_only_with_hotkey", w.get_active()))
        group.add(row)
        return group

    def _group_extras(self):
        group = Adw.PreferencesGroup(title="Sharing")
        for title, subtitle, key in (
            ("Share the clipboard", "Copy on one computer, paste on another", "share_clipboard"),
            ("Include the touchpad", "Only used once the pointer has left this screen",
             "grab_touchpads"),
            ("Find computers automatically", "Announce myself on this network", "discovery"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.cfg[key]))
            row.connect("notify::active", lambda w, _p, k=key: self._save(k, w.get_active()))
            group.add(row)

        speed = Adw.SpinRow.new_with_range(0.2, 3.0, 0.1)
        speed.set_title("Pointer speed elsewhere")
        speed.set_digits(1)
        speed.set_value(float(self.cfg["pointer_speed"]))
        speed.connect("notify::value",
                      lambda w, _p: self._save("pointer_speed", round(w.get_value(), 2)))
        group.add(speed)
        return group

    def _spin(self, group, title, key, lo, hi, step, subtitle):
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_value(float(self.cfg[key]))
        row.connect("notify::value", lambda w, _p: self._save(key, int(w.get_value())))
        group.add(row)
        return row

    # ------------------------------------------------------------- machines
    def _rebuild_machines(self):
        for row in self._rows:
            self.machines.remove(row)
        self._rows = []

        if self.cfg["role"] == "server":
            self.machines.set_title("Computers I control")
            self.machines.set_description(
                "Say where each one sits, and push the pointer off that edge to reach it.")
            peers = self.cfg.get("peers", {})
            for ident, entry in sorted(peers.items(), key=lambda kv: kv[1].get("name", "")):
                self._add_row(self._peer_row(ident, entry))
            if not peers:
                empty = Adw.ActionRow(title="No computers yet",
                                      subtitle="Press Allow, then turn on Lightmorphic Flow there")
                empty.add_css_class("dim")
                self._add_row(empty)

            self._allow_row = Adw.ActionRow(title="Allow a new computer")
            self._allow_button = Gtk.Button(valign=Gtk.Align.CENTER)
            self._allow_button.connect("clicked", self._allow)
            self._allow_row.add_suffix(self._allow_button)
            self._allow_row.set_activatable_widget(self._allow_button)
            self._add_row(self._allow_row)
            self._show_allow()
        else:
            self.machines.set_title("Computer controlling me")
            state = status.read()
            if state.get("connected"):
                who = state.get("server_name") or state.get("server") or "the other computer"
                self.machines.set_description("")
                row = Adw.ActionRow(
                    title=f"Connected to {who}",
                    subtitle="Its mouse and keyboard reach this screen when you "
                             "push the pointer off the matching edge over there.")
                row.set_subtitle_lines(2)
                dot = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                dot.add_css_class("success")
                row.add_prefix(dot)
                self._add_row(row)
                self._add_row(self._code_row())
                return
            self.machines.set_description(
                "Pick the one with the mouse and keyboard, then press Allow over there.")
            found = self._found()
            chosen = self.cfg.get("server_id")
            for entry in found:
                row = Adw.ActionRow(
                    title=entry.get("name", entry["host"]),
                    subtitle=("paired" if entry["id"] == chosen else entry["host"])
                    + (" - ready to accept" if entry.get("pairing") else ""))
                button = Gtk.Button(label="Use this one", valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda _b, e=entry: self._choose_server(e))
                row.add_suffix(button)
                self._add_row(row)
            if not found:
                empty = Adw.ActionRow(title="Nothing found on this network yet",
                                      subtitle="Both computers need Lightmorphic Flow running")
                empty.add_css_class("dim")
                self._add_row(empty)

        self._add_row(self._code_row())

    def _add_row(self, row):
        self.machines.add(row)
        self._rows.append(row)

    def _peer_row(self, ident, entry):
        row = Adw.ComboRow(title=entry.get("name", ident),
                           subtitle="where it sits next to this screen",
                           model=Gtk.StringList.new(EDGE_LABELS))
        edge = entry.get("edge", "right")
        row.set_selected(EDGES.index(edge) if edge in EDGES else 0)
        row.connect("notify::selected", self._on_peer_edge, ident)

        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Forget this computer")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda _b: self._forget(ident))
        row.add_suffix(remove)
        return row

    def _code_row(self):
        row = Adw.ExpanderRow(title="Pairing code",
                              subtitle="Only needed when the two are not on the same network")
        if self._code is None:
            try:
                self._code = pairing.make_code()
            except Exception as exc:
                self._code = f"could not build a code: {exc}"
        inner = Adw.ActionRow(title=self._code, subtitle="")
        inner.set_title_lines(3)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda b: self._copy(b, inner.get_title()))
        inner.add_suffix(copy)
        row.add_row(inner)

        entry = Adw.EntryRow(title="Paste a code from another computer")
        entry.set_show_apply_button(True)
        entry.connect("apply", self._apply_code)
        row.add_row(entry)
        return row

    # --------------------------------------------------------------- actions
    def _save(self, key, value):
        if self.cfg.get(key) == value:
            return
        self.cfg[key] = value
        config.save(self.cfg)

    def _on_role(self, widget, _param):
        self._save("role", "server" if widget.get_selected() == 0 else "client")
        self._rebuild_machines()
        self._refresh_power()

    def _on_peer_edge(self, widget, _param, ident):
        peers = self.cfg.setdefault("peers", {})
        entry = peers.setdefault(ident, {"name": ident, "enabled": True})
        edge = EDGES[widget.get_selected()]
        if entry.get("edge") == edge:
            return
        clash = [i for i, e in peers.items() if i != ident and e.get("edge") == edge]
        entry["edge"] = edge
        for other in clash:                      # two machines cannot share one edge
            free = [e for e in EDGES if e not in {p.get("edge") for p in peers.values()}]
            peers[other]["edge"] = free[0] if free else "right"
        config.save(self.cfg)
        if clash:
            GLib.idle_add(self._rebuild_machines)

    def _forget(self, ident):
        self.cfg.get("peers", {}).pop(ident, None)
        config.save(self.cfg)
        self._rebuild_machines()

    def _allow(self, _button):
        if config.pairing_open():
            config.close_pairing()
        else:
            config.open_pairing(PAIR_SECONDS)
        self._show_allow()

    def _show_allow(self):
        """The countdown lives in the row, not in a popup."""
        row, button = self._allow_row, self._allow_button
        if row is None or button is None:
            return
        left = config.pairing_seconds_left()
        if left > 0:
            row.set_subtitle(f"Open for {left // 60}:{left % 60:02d} — "
                             "turn on Lightmorphic Flow on the other computer now")
            button.set_label("Stop")
            button.remove_css_class("suggested-action")
            button.add_css_class("destructive-action")
        else:
            row.set_subtitle("Opens the door for two minutes")
            button.set_label("Allow")
            button.remove_css_class("destructive-action")
            button.add_css_class("suggested-action")

    def _choose_server(self, entry):
        self.cfg["server_id"] = entry["id"]
        self.cfg["server_host"] = entry["host"]
        self.cfg["server_fingerprint"] = ""
        self.cfg["token"] = ""
        config.save(self.cfg)
        self._rebuild_machines()

    def _copy(self, button, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        button.set_icon_name("object-select-symbolic")
        GLib.timeout_add_seconds(2, self._uncopy, button)

    def _uncopy(self, button):
        button.set_icon_name("edit-copy-symbolic")
        return False

    def _apply_code(self, entry):
        try:
            self.cfg = pairing.apply_code(entry.get_text())
        except Exception:
            entry.add_css_class("error")
            entry.set_title("That code did not look right")
            GLib.timeout_add_seconds(4, self._clear_code_error, entry)
            return
        entry.remove_css_class("error")
        entry.set_text("")
        self.role.set_selected(1)
        self._rebuild_machines()

    def _clear_code_error(self, entry):
        entry.remove_css_class("error")
        entry.set_title("Paste a code from another computer")
        return False

    # ---------------------------------------------------------------- daemon
    def _unit(self):
        role = "server" if self.cfg["role"] == "server" else "client"
        return f"lmflow-{role}.service"

    def _on_power(self, widget, _param):
        other = ("lmflow-client.service" if self.cfg["role"] == "server"
                 else "lmflow-server.service")
        if widget.get_active():
            _systemctl("stop", other)
            result = _systemctl("start", self._unit())
            if result is None or result.returncode != 0:
                print("could not start the background service", file=sys.stderr)
                return
        else:
            _systemctl("stop", self._unit())

    def _refresh_power(self):
        result = _systemctl("is-active", self._unit())
        running = bool(result) and result.stdout.strip() == "active"
        if self.power.get_active() != running:
            self.power.handler_block_by_func(self._on_power)
            self.power.set_active(running)
            self.power.handler_unblock_by_func(self._on_power)

    def _tick(self):
        fresh = config.load()
        changed = (fresh.get("peers") != self.cfg.get("peers")
                   or fresh["role"] != self.cfg["role"]
                   or fresh.get("server_id") != self.cfg.get("server_id"))
        if changed:
            self.cfg = fresh
        if self.cfg["role"] == "client":
            self._own_listener()
            signature = [status.read().get("connected")] + \
                [(f["id"], f.get("pairing")) for f in self._found()]
            if signature != self._found_signature:
                self._found_signature = signature
                changed = True
        if changed:
            self._rebuild_machines()
            self._rebuild_warning()
        self._show_allow()
        self._refresh_power()
        was_ok = getattr(self, "_permission_ok", True)
        self._permission_ok = permissions.message() is None
        if was_ok != self._permission_ok:
            self._rebuild_warning()
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="uk.lightmorph.Flow")

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Window(self).present()


def main():
    return App().run([])
