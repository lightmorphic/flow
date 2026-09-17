"""Can this session actually read the mouse and keyboard, and if not, why not."""
from __future__ import annotations

import glob
import grp
import os

GROUP = "input"

READY = "ready"
NEEDS_RELOGIN = "relogin"       # in the group, but this session predates it
NEEDS_SETUP = "setup"           # not in the group at all


def _in_group_on_disk() -> bool:
    try:
        if os.getlogin() in grp.getgrnam(GROUP).gr_mem:
            return True
    except (OSError, KeyError):
        pass
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        return bool(user) and user in grp.getgrnam(GROUP).gr_mem
    except KeyError:
        return False


def _in_group_now() -> bool:
    try:
        return grp.getgrnam(GROUP).gr_gid in os.getgroups()
    except KeyError:
        return False


def _can_touch() -> bool:
    try:
        os.close(os.open("/dev/uinput", os.O_WRONLY))
    except OSError:
        return False
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            os.close(os.open(path, os.O_RDONLY))
            return True
        except OSError:
            continue
    return False


def state() -> str:
    if _can_touch():
        return READY
    if _in_group_on_disk() and not _in_group_now():
        return NEEDS_RELOGIN
    if _in_group_on_disk():
        return NEEDS_RELOGIN            # group is there; the session is stale
    return NEEDS_SETUP


HEADLINE = {
    NEEDS_RELOGIN: "Log out and back in to finish",
    NEEDS_SETUP: "One more step before this can work",
}

BODY = {
    NEEDS_RELOGIN: (
        "Lightmorphic Flow cannot read your mouse and keyboard yet.\n\n"
        "It normally takes effect the moment it is installed. On this computer "
        "that has not happened — which usually means this is a remote or "
        "unusual session, rather than someone sitting at the machine.\n\n"
        "Log out and log back in — or restart the computer — and it will work. "
        "You will not have to do this again."
    ),
    NEEDS_SETUP: (
        "Lightmorphic Flow cannot read your mouse and keyboard.\n\n"
        "It was not installed from the package, so the permission it needs was "
        "never set up. Open a terminal and run:\n\n"
        "    lmflow setup\n\n"
        "then log out and log back in."
    ),
}


def message():
    """(headline, body) for whatever is wrong, or None when all is well."""
    current = state()
    if current == READY:
        return None
    return HEADLINE[current], BODY[current]
