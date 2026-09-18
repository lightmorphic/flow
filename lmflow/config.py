"""Settings file. Everything the user can change lives here, not in code."""
from __future__ import annotations

import json
import os
import secrets

CONFIG_DIR = os.path.expanduser("~/.config/lmflow")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CERT_PATH = os.path.join(CONFIG_DIR, "cert.pem")
PAIR_PATH = os.path.join(CONFIG_DIR, "pairing-open-until")
KEY_PATH = os.path.join(CONFIG_DIR, "key.pem")

DEFAULTS = {
    "role": "server",            # server = this machine owns the mouse+keyboard
    "port": 24810,
    "server_host": "",           # client only: where the server is
    "server_fingerprint": "",    # client only: pinned certificate
    "server_id": "",             # client only: which machine we paired with
    "server_name": "",           # client only: what it calls itself
    "token": "",

    "screen_width": 1920,
    "screen_height": 1080,

    # Other machines ---------------------------------------------------------
    # id -> {"name": ..., "edge": "right"|"left"|"top"|"bottom", "enabled": true}
    "peers": {},
    "discovery": True,

    # Crossing behaviour -----------------------------------------------------
    "peer_edge": "right",        # the edge offered to a newly added machine
    "corner_guard_px": 140,      # no crossing this close to a corner (hot corners stay yours)
    "push_px": 90,               # how far you must keep pushing past the edge to cross
    "push_ms": 450,              # ...within this long, or the push resets
    "return_push_px": 20,        # coming home is easy: a nudge, not a shove
    "return_push_ms": 2000,      # ...and you may take your time over it
    "edge_only_with_hotkey": False,

    "hotkey_switch": "ctrl+alt+s",
    "hotkey_panic": "ctrl+alt+shift+k",

    "grab_touchpads": True,
    "share_clipboard": True,
    "clipboard_poll_ms": 700,
    "pointer_speed": 1.0,
    # On your own screen the desktop speeds the pointer up, and Flow cannot
    # see where it really is. Counting movement generously means Flow reaches
    # an edge no later than the real pointer does, so pushing always crosses.
    "local_edge_speed": 2.5,
    # "position" is exact and cannot drift. "movement" is the older way, for a
    # desktop that will not accept a pointer told where to be.
    "pointer_mode": "position",

    "ignore_devices": [],        # device names never to grab
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def set_value(key, value):
    cfg = load()
    cfg[key] = value
    save(cfg)
    return cfg


def open_pairing(seconds=120):
    """Let a new machine join for the next couple of minutes."""
    import time
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    with open(PAIR_PATH, "w", encoding="ascii") as fh:
        fh.write(str(time.time() + seconds))
    return seconds


def pairing_open() -> bool:
    return pairing_seconds_left() > 0


def pairing_seconds_left() -> int:
    import time
    try:
        with open(PAIR_PATH, encoding="ascii") as fh:
            return max(0, int(float(fh.read().strip()) - time.time()))
    except (OSError, ValueError):
        return 0


def close_pairing():
    try:
        os.unlink(PAIR_PATH)
    except OSError:
        pass


def mtime() -> float:
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0
