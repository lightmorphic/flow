"""A small file the daemon writes and the tray reads, and one it reads back.

No socket and no service to keep alive: the daemon is already looking at the
settings file once a second, so it looks at these at the same time.
"""
from __future__ import annotations

import json
import os

from .config import CONFIG_DIR

STATUS_PATH = os.path.join(CONFIG_DIR, "status.json")
COMMAND_PATH = os.path.join(CONFIG_DIR, "command")

EMPTY = {"role": "server", "running": False, "active": None, "peers": []}


def write(state: dict) -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass


def read() -> dict:
    try:
        with open(STATUS_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return dict(EMPTY)
    merged = dict(EMPTY)
    merged.update(state if isinstance(state, dict) else {})
    return merged


def clear() -> None:
    for path in (STATUS_PATH, COMMAND_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass


def send(command: str) -> None:
    """'home', or 'goto:<peer id>'."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        with open(COMMAND_PATH, "w", encoding="ascii") as fh:
            fh.write(command)
    except OSError:
        pass


def take():
    """Read and remove the pending command, if there is one."""
    try:
        with open(COMMAND_PATH, encoding="ascii") as fh:
            command = fh.read().strip()
    except OSError:
        return None
    try:
        os.unlink(COMMAND_PATH)
    except OSError:
        pass
    return command or None
