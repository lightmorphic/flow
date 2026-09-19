"""Command line front end."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys

from . import __version__, config, discovery, pairing, screen
from .linux_input import list_devices

UDEV_RULE = """# Installed by Lightmorphic Flow.
#
# "uaccess" hands the device to whoever is signed in at this computer, the
# moment the rule is installed - no group membership and no logging out. The
# group is kept as a fallback for a machine where that does not apply.
#
# This does mean any program you run can read what you type. On a computer with
# one user that is the trade for not having to log out; on a shared machine,
# delete the second line and use the group instead.
KERNEL=="uinput", MODE="0660", GROUP="input", TAG+="uaccess", OPTIONS+="static_node=uinput"
SUBSYSTEM=="input", KERNEL=="event*", MODE="0660", GROUP="input", TAG+="uaccess"
"""
UDEV_PATH = "/etc/udev/rules.d/60-lmflow.rules"


def cmd_setup(_args):
    import tempfile
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as fh:
        fh.write(UDEV_RULE)
        rule_tmp = fh.name
    script = f"""set -e
install -m 0644 {rule_tmp} {UDEV_PATH}
getent group input >/dev/null || groupadd input
usermod -aG input {user}
modprobe uinput || true
printf 'uinput\\n' > /etc/modules-load.d/lmflow-uinput.conf
udevadm control --reload-rules
udevadm trigger --subsystem-match=input --subsystem-match=misc
"""
    print("This asks for your password once, to allow reading the mouse and keyboard.")
    result = subprocess.run(["sudo", "bash", "-c", script])
    os.unlink(rule_tmp)
    if result.returncode != 0:
        print("Setup did not finish.", file=sys.stderr)
        return 1
    print("\nDone. Open Lightmorphic Flow, or run:  lmflow server")
    print("If it says it cannot read your mouse, log out and back in once.")
    return 0


UNIT = """[Unit]
Description=Lightmorphic Flow ({role})
After=graphical-session.target

[Service]
Type=simple
ExecStart={exe} {role}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=Lightmorphic Flow
Comment=Share one mouse, keyboard and clipboard
Exec={exe} gui
Icon=input-mouse
Terminal=false
Categories=Utility;Settings;
"""


def cmd_install_services(_args):
    exe = _launcher()
    units = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(units, exist_ok=True)
    for role in ("server", "client"):
        path = os.path.join(units, f"lmflow-{role}.service")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(UNIT.format(role=role, exe=exe))
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    with open(os.path.join(apps, "lmflow.desktop"), "w", encoding="utf-8") as fh:
        fh.write(DESKTOP.format(exe=exe))
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print(f"Installed background services and a menu entry (using {exe}).")
    print("Start it with:  systemctl --user start lmflow-server")
    return 0


def _launcher():
    local = os.path.expanduser("~/.local/bin/lmflow")
    if os.path.exists(local):
        return local
    return f"{sys.executable} -m lmflow"


def cmd_devices(_args):
    for info in list_devices():
        print(f"{info.kind:9} {info.name}   ({info.path})")
    return 0


def cmd_pair(args):
    if args.code:
        cfg = pairing.apply_code(args.code)
        print(f"Paired with {cfg['server_host']}. Now run:  lmflow client")
    else:
        print("Run this on the other machine:\n")
        print(f"  lmflow pair {pairing.make_code()}\n")
    return 0


EDGE_WORDS = {"right": "right", "left": "left", "above": "top", "top": "top",
              "below": "bottom", "bottom": "bottom", "under": "bottom", "up": "top"}
EDGE_SAY = {"right": "to the right", "left": "to the left",
            "top": "above", "bottom": "below"}


def cmd_scan(args):
    print(f"Listening for {args.seconds:.0f} seconds...")
    found = discovery.scan(args.seconds)
    if not found:
        print("Nothing found. Lightmorphic Flow needs to be running on the other computer.")
        return 1
    for entry in found:
        extra = " - ready to accept a new computer" if entry.get("pairing") else ""
        print(f"  {entry.get('name', '?'):20} {entry['host']:15} "
              f"{entry.get('role', '?')}{extra}")
    return 0


def cmd_allow(args):
    config.open_pairing(args.seconds)
    print(f"Open for {args.seconds:.0f} seconds. Start Lightmorphic Flow on the other computer now.")
    return 0


def cmd_peers(_args):
    peers = config.load().get("peers", {})
    if not peers:
        print("No computers yet. Run 'lmflow allow', then start it on the other one.")
        return 0
    for ident, entry in sorted(peers.items(), key=lambda kv: kv[1].get("name", "")):
        print(f"  {entry.get('name', ident):20} {EDGE_SAY.get(entry.get('edge'), '?')}")
    return 0


def cmd_where(args):
    edge = EDGE_WORDS.get(args.position.lower())
    if edge is None:
        print("Say right, left, above or below.", file=sys.stderr)
        return 1
    cfg = config.load()
    peers = cfg.get("peers", {})
    matches = [i for i, e in peers.items()
               if args.name.lower() in (e.get("name", "") + " " + i).lower()]
    if not matches:
        print(f"No computer called '{args.name}'. Try: lmflow peers", file=sys.stderr)
        return 1
    ident = matches[0]
    for other, entry in peers.items():
        if other != ident and entry.get("edge") == edge:
            free = [e for e in ("right", "left", "top", "bottom")
                    if e not in {p.get("edge") for p in peers.values()}]
            entry["edge"] = free[0] if free else "right"
            print(f"moved {entry.get('name', other)} {EDGE_SAY[entry['edge']]}")
    peers[ident]["edge"] = edge
    config.save(cfg)
    print(f"{peers[ident].get('name', ident)} is now {EDGE_SAY[edge]}")
    return 0


def cmd_forget(args):
    cfg = config.load()
    peers = cfg.get("peers", {})
    matches = [i for i, e in peers.items()
               if args.name.lower() in (e.get("name", "") + " " + i).lower()]
    if not matches:
        print(f"No computer called '{args.name}'.", file=sys.stderr)
        return 1
    for ident in matches:
        print(f"forgot {peers.pop(ident).get('name', ident)}")
    config.save(cfg)
    return 0


def _say(*parts):
    print(*parts, flush=True)


def cmd_server(args):
    from .linux_input import InputError
    from .server import Server
    try:
        server = Server(config.load(), log=_say)
        _install_stop(server)
        if args.test_seconds:
            _auto_stop(server, args.test_seconds)
        server.run()
    except InputError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_client(args):
    from .client import Client
    from .linux_input import InputError
    try:
        client = Client(config.load(), log=_say)
        _install_stop(client)
        if args.test_seconds:
            _auto_stop(client, args.test_seconds)
        client.run()
    except InputError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_config(args):
    cfg = config.load()
    if args.key is None:
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0
    if args.value is None:
        print(cfg.get(args.key))
        return 0
    current = cfg.get(args.key)
    value = args.value
    if isinstance(current, bool):
        value = value.lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        value = int(value)
    elif isinstance(current, float):
        value = float(value)
    config.set_value(args.key, value)
    print(f"{args.key} = {value}")
    return 0


def _typelib(namespace, version):
    """Is <Namespace>-<version>.typelib installed anywhere it would be found?"""
    import glob
    roots = [d for d in os.environ.get("GI_TYPELIB_PATH", "").split(os.pathsep) if d]
    roots += glob.glob("/usr/lib*/girepository-*") + glob.glob("/usr/lib/*/girepository-*")
    wanted = f"{namespace}-{version}.typelib"
    return any(os.path.exists(os.path.join(root, wanted)) for root in roots)


def cmd_doctor(_args):
    """Everything worth knowing when something is missing or broken."""
    import platform
    from . import __version__, discovery
    print(f"Lightmorphic Flow {__version__}")
    print(f"python           {sys.version.split()[0]}  ({sys.executable})")
    print(f"system           {platform.platform()}")
    print(f"session          {os.environ.get('XDG_SESSION_TYPE', '?')} / "
          f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    print(f"displays         wayland={os.environ.get('WAYLAND_DISPLAY') or '-'} "
          f"x11={os.environ.get('DISPLAY') or '-'}")
    print(f"machine          {discovery.machine_name()}  {discovery.machine_id()}")

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        print(f"gtk4             {Gtk.get_major_version()}.{Gtk.get_minor_version()}"
              f".{Gtk.get_micro_version()}")
    except Exception as exc:
        print(f"gtk4             MISSING ({exc})")
    # Look on disk rather than importing: the tray library is built against
    # GTK 3, and GTK 4 is already loaded in this process, so importing it here
    # would fail even when it is perfectly well installed.
    for namespace, version, label in (("Adw", "1", "libadwaita"),
                                      ("AyatanaAppIndicator3", "0.1", "tray (ayatana)"),
                                      ("AppIndicator3", "0.1", "tray (older)")):
        print(f"{label:16} {'yes' if _typelib(namespace, version) else 'no'}")

    try:
        import gi._gi_cairo            # noqa: F401 - the probe is the import
        import cairo                   # noqa: F401
        print("cairo drawing    yes")
    except ImportError:
        print("cairo drawing    NO - the update dot cannot be drawn. Install "
              "python3-gi-cairo (Debian) or python3-cairo (Fedora).")

    from . import firewall
    kind = firewall.running()
    if kind:
        print(f"firewall         {kind} is running. If the computers cannot see "
              f"each other:\n                 {firewall.spoken(kind)}")
    else:
        print("firewall         none running")

    cfg = config.load()
    print(f"role             {cfg['role']}")
    print(f"computers known  {len(cfg.get('peers', {}))}")

    for path, mode, what in (("/dev/uinput", os.O_WRONLY, "write"),
                             ("/dev/input/event0", os.O_RDONLY, "read")):
        try:
            os.close(os.open(path, mode))
            print(f"{path:16} {what} ok")
        except OSError as exc:
            print(f"{path:16} {what} FAILED - {exc.strerror}")
    from . import permissions
    state = permissions.state()
    if state == permissions.READY:
        print("input access     ok")
    else:
        print(f"input access     NOT WORKING - {permissions.HEADLINE[state].lower()}")
    groups = subprocess.run(["id", "-nG"], capture_output=True, text=True).stdout.split()
    print(f"input group      {'yes' if 'input' in groups else 'no (not needed if access is ok)'}")
    return 0


REPORT_DIRS = ("~/8-Claude-Sync", "~/Desktop", "~")


def cmd_report(_args):
    """Everything about this machine, written to a file that can be sent on."""
    import datetime
    import io
    from . import discovery

    target = None
    for folder in REPORT_DIRS:
        folder = os.path.expanduser(folder)
        if os.path.isdir(folder):
            target = os.path.join(
                folder, f"lmflow-report-{discovery.machine_name()}.txt")
            break
    if target is None:
        target = os.path.expanduser("~/lmflow-report.txt")

    buffer = io.StringIO()
    saved, sys.stdout = sys.stdout, buffer
    try:
        cmd_doctor(None)
    finally:
        sys.stdout = saved
    lines = [f"Lightmorphic Flow report, {datetime.datetime.now():%Y-%m-%d %H:%M}",
             "", buffer.getvalue()]

    # Settings, with the secrets blanked: this file is made to be sent on.
    shown = dict(config.load())
    for secret in ("token", "server_fingerprint"):
        if shown.get(secret):
            shown[secret] = "(hidden)"
    lines.append("\n===== settings =====")
    lines.append(json.dumps(shown, indent=2, sort_keys=True))

    for title, argv in (
        ("services", ["systemctl", "--user", "--no-pager", "--all",
                      "list-units", "lmflow*"]),
        ("recent log", ["journalctl", "--user", "-u", "lmflow-server.service",
                        "-u", "lmflow-client.service", "-u", "lmflow-tray.service",
                        "-n", "200", "--no-pager"]),
        ("what it is doing", ["cat", os.path.expanduser("~/.config/lmflow/status.json")]),
        ("input devices", ["cat", "/proc/bus/input/devices"]),
    ):
        lines.append(f"\n===== {title} =====")
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            lines.append(result.stdout or result.stderr or "(nothing)")
        except (OSError, subprocess.SubprocessError) as exc:
            lines.append(f"(could not read: {exc})")

    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.chmod(target, 0o600)
    print(f"Written to {target}")
    return 0


def cmd_screen(_args):
    size = screen.detect()
    print(f"{size[0]}x{size[1]}" if size else "could not detect; set it in the settings")
    return 0


def cmd_gui(_args):
    from .gui import main
    return main()


def cmd_tray(_args):
    from .tray import main
    return main()


def cmd_clipwatch(_args):
    from .clipwatch import main
    return main()


def _install_stop(obj):
    def handler(_sig, _frame):
        obj.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _auto_stop(obj, seconds):
    import threading
    def die():
        print(f"\ntest run over after {seconds}s")
        obj.stop()
        os._exit(0)
    threading.Timer(seconds, die).start()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lmflow",
                                     description="Share one mouse, keyboard and clipboard.")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="cmd", required=True)

    subs.add_parser("setup", help="grant input access (asks for your password once)").set_defaults(func=cmd_setup)
    subs.add_parser("devices", help="list the mice and keyboards found").set_defaults(func=cmd_devices)
    subs.add_parser("screen", help="show the detected screen size").set_defaults(func=cmd_screen)
    subs.add_parser("doctor", help="report everything about this machine").set_defaults(func=cmd_doctor)
    subs.add_parser("report", help="write a full report to a file you can send on").set_defaults(func=cmd_report)
    subs.add_parser("gui", help="open the settings window").set_defaults(func=cmd_gui)
    subs.add_parser("tray", help="show the tray icon").set_defaults(func=cmd_tray)
    subs.add_parser("clipwatch", help=argparse.SUPPRESS).set_defaults(func=cmd_clipwatch)
    subs.add_parser("install-services",
                    help="add the background service and menu entry").set_defaults(func=cmd_install_services)

    scan = subs.add_parser("scan", help="list computers running this on the network")
    scan.add_argument("--seconds", type=float, default=3.0)
    scan.set_defaults(func=cmd_scan)

    allow = subs.add_parser("allow", help="let a new computer join for a couple of minutes")
    allow.add_argument("--seconds", type=float, default=120)
    allow.set_defaults(func=cmd_allow)

    subs.add_parser("peers", help="list the computers this one controls").set_defaults(func=cmd_peers)

    where = subs.add_parser("where", help="say where a computer sits: right, left, above, below")
    where.add_argument("name")
    where.add_argument("position")
    where.set_defaults(func=cmd_where)

    forget = subs.add_parser("forget", help="remove a computer")
    forget.add_argument("name")
    forget.set_defaults(func=cmd_forget)

    pair = subs.add_parser("pair", help="show or use a pairing code")
    pair.add_argument("code", nargs="?")
    pair.set_defaults(func=cmd_pair)

    for name, func, help_text in (("server", cmd_server, "share this machine's mouse and keyboard"),
                                  ("client", cmd_client, "receive the other machine's mouse and keyboard")):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("--test-seconds", type=float, default=0,
                         help="stop automatically after this many seconds")
        sub.set_defaults(func=func)

    conf = subs.add_parser("config", help="show or change a setting")
    conf.add_argument("key", nargs="?")
    conf.add_argument("value", nargs="?")
    conf.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    return args.func(args)
