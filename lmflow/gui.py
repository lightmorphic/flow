"""Settings window. Every field saves itself; there is no save button."""
from __future__ import annotations

import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk          # noqa: E402

from . import config, discovery, pairing, screen        # noqa: E402
from .updatedot import UpdateDot                        # noqa: E402

EDGES = ["right", "left", "top", "bottom"]
EDGE_LABELS = ["To my right", "To my left", "Above me", "Below me"]
PAIR_SECONDS = 120
CSS = b"""
toast > widget { background: #1f7a3d; color: #ffffff; }
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

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=""))
        header.pack_start(self._brand())
        header.pack_end(UpdateDot())
        box.append(header)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.page = Adw.PreferencesPage()
        scroller.set_child(self.page)
        box.append(scroller)
        self.toasts.set_child(box)

        self.page.add(self._group_role())
        self.machines = Adw.PreferencesGroup()
        self.page.add(self.machines)
        self.page.add(self._group_edges())
        self.page.add(self._group_extras())

        self.listener = discovery.Listener()
        self.listener.start()

        self._rebuild_machines()
        self._refresh_power()
        GLib.timeout_add_seconds(2, self._tick)

    # ---------------------------------------------------------------- groups
    def _brand(self):
        """Logo and name, top left."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        display = Gdk.Display.get_default()
        theme = Gtk.IconTheme.get_for_display(display) if display else None
        name = "lmflow" if theme and theme.has_icon("lmflow") else "input-mouse-symbolic"
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

            allow = Adw.ActionRow(
                title="Allow a new computer",
                subtitle="Opens the door for two minutes")
            button = Gtk.Button(label="Allow", valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", self._allow)
            allow.add_suffix(button)
            allow.set_activatable_widget(button)
            self._add_row(allow)
        else:
            self.machines.set_title("Computer controlling me")
            self.machines.set_description(
                "Pick the one with the mouse and keyboard, then press Allow over there.")
            found = self.listener.found(role="server")
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
        copy.connect("clicked", lambda _b: self._copy(inner.get_title()))
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
        self.tick()

    def tick(self, text="✓"):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=1))

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
        self.tick()
        if clash:
            GLib.idle_add(self._rebuild_machines)

    def _forget(self, ident):
        self.cfg.get("peers", {}).pop(ident, None)
        config.save(self.cfg)
        self._rebuild_machines()
        self.tick()

    def _allow(self, _button):
        config.open_pairing(PAIR_SECONDS)
        self.tick("✓ open for two minutes")

    def _choose_server(self, entry):
        self.cfg["server_id"] = entry["id"]
        self.cfg["server_host"] = entry["host"]
        self.cfg["server_fingerprint"] = ""
        self.cfg["token"] = ""
        config.save(self.cfg)
        self._rebuild_machines()
        self.tick()

    def _copy(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.tick()

    def _apply_code(self, entry):
        try:
            self.cfg = pairing.apply_code(entry.get_text())
        except Exception:
            self.tick("That code did not look right")
            return
        entry.set_text("")
        self.role.set_selected(1)
        self._rebuild_machines()
        self.tick()

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
                self.tick("Could not start it")
                return
        else:
            _systemctl("stop", self._unit())
        self.tick()

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
            signature = [(f["id"], f.get("pairing")) for f in self.listener.found(role="server")]
            if signature != self._found_signature:
                self._found_signature = signature
                changed = True
        if changed:
            self._rebuild_machines()
        self._refresh_power()
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
