"""Is a firewall in the way, and what would open it."""
from __future__ import annotations

import os
import shutil
import subprocess

SBIN = ("/usr/sbin", "/sbin", "/usr/local/sbin")

from .config import DEFAULTS

TCP_PORT = DEFAULTS["port"]          # the machines talk over this
UDP_PORT = 24811                     # and announce themselves over this
PROFILE = "Lightmorphic-Flow"


def _found(name):
    """These live in sbin, which is not always on a normal user's PATH."""
    path = shutil.which(name)
    if path:
        return path
    for root in SBIN:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def _active(unit) -> bool:
    try:
        result = subprocess.run(["systemctl", "is-active", unit],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "active"


def running():
    """'ufw', 'firewalld', or None."""
    if _active("ufw") and _found("ufw"):
        return "ufw"
    if _active("firewalld") and _found("firewall-cmd"):
        return "firewalld"
    return None


def command(kind=None):
    """The command that opens the way, as a list, or None."""
    kind = kind or running()
    if kind == "ufw":
        return [_found("ufw") or "ufw", "allow", PROFILE]
    if kind == "firewalld":
        return [_found("firewall-cmd") or "firewall-cmd", "--permanent",
                "--add-service=lightmorphic-flow"]
    return None


def spoken(kind=None):
    """The same command, written out for a person to type."""
    kind = kind or running()
    if kind == "ufw":
        return f"sudo ufw allow {PROFILE}"
    if kind == "firewalld":
        return ("sudo firewall-cmd --permanent --add-service=lightmorphic-flow "
                "&& sudo firewall-cmd --reload")
    return None


def open_it(kind=None):
    """Ask for the password once and open it. True if it worked."""
    kind = kind or running()
    argv = command(kind)
    if argv is None or not shutil.which("pkexec"):
        return False
    try:
        if subprocess.run(["pkexec", *argv], capture_output=True,
                          timeout=120).returncode != 0:
            return False
        if kind == "firewalld":
            subprocess.run(["pkexec", _found("firewall-cmd") or "firewall-cmd",
                            "--reload"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
