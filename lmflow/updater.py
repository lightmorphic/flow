"""The single update dot: checking, downloading and installing.

Five states, one control, no other update UI anywhere in the app.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

from . import __version__

REPO = "lightmorphic/flow"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
WEBSITE = "https://flow.lightmorphic.com"
CACHE = os.path.expanduser("~/.cache/lmflow")
CHECK_SECONDS = 30 * 60

UP_TO_DATE = "green"
AVAILABLE = "yellow"
DOWNLOADING = "ring"
READY = "blue"
OFFLINE = "red"

TOOLTIPS = {
    UP_TO_DATE: "up to date",
    AVAILABLE: "update available - click to download",
    DOWNLOADING: "downloading",
    READY: "click to restart",
    OFFLINE: "cannot reach GitHub to check for updates",
}


def parse_version(text):
    parts = []
    for chunk in str(text).lstrip("vV").split(".")[:4]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def newer(remote, local=__version__):
    return parse_version(remote) > parse_version(local)


def newest(releases):
    """The newest published release, pre-release or not."""
    published = [r for r in releases if isinstance(r, dict) and not r.get("draft")]
    if not published:
        return None
    return max(published, key=lambda r: parse_version(r.get("tag_name") or ""))


def _package_kind():
    if shutil.which("dpkg"):
        return ".deb"
    if shutil.which("rpm"):
        return ".rpm"
    return None


class Updater:
    """Runs the checking and downloading off the main thread.

    on_state(state, progress) is called back on whatever thread we are on;
    the window wraps it so GTK is only touched from the main loop.
    """

    def __init__(self, on_state, log=print):
        self.on_state = on_state
        self.log = log
        self.state = UP_TO_DATE
        self.release = None
        self.downloaded = None
        self.progress = 0.0
        self._busy = threading.Lock()

    # ------------------------------------------------------------- checking
    def check(self, manual=False):
        if not self._busy.acquire(blocking=False):
            return
        threading.Thread(target=self._check, args=(manual,), daemon=True).start()

    def _check(self, manual):
        try:
            if self.state in (DOWNLOADING, READY):
                return
            try:
                request = urllib.request.Request(
                    API, headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": f"lmflow/{__version__}"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    releases = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
                self.log(f"update check failed: {exc}")
                self._set(OFFLINE)
                return

            # Not /releases/latest: that endpoint hides pre-releases, and while
            # this is beta every release is one.
            release = newest(releases)
            if release is None:
                self._set(UP_TO_DATE, note="no update available" if manual else None)
                return
            tag = release.get("tag_name") or release.get("name") or ""
            if newer(tag):
                self.release = release
                self._set(AVAILABLE)
            else:
                self.release = None
                self._set(UP_TO_DATE, note="no update available" if manual else None)
        finally:
            self._busy.release()

    # ------------------------------------------------------------ download
    def download(self):
        if self.release is None or not self._busy.acquire(blocking=False):
            return
        threading.Thread(target=self._download, daemon=True).start()

    def _asset(self):
        """The package for this machine, and for this release rather than an
        older one that happens to be attached to it as well."""
        want = _package_kind()
        assets = self.release.get("assets", []) if self.release else []
        tag = (self.release.get("tag_name") or "").lstrip("vV")
        matching = [a for a in assets if want and a.get("name", "").endswith(want)]
        for asset in matching:
            if tag and tag in asset.get("name", ""):
                return asset
        if matching:
            return matching[0]
        return assets[0] if assets else None

    def _download(self):
        try:
            asset = self._asset()
            if asset is None:
                self.log("update: that release has no package attached")
                self._set(OFFLINE)
                return
            os.makedirs(CACHE, exist_ok=True)
            target = os.path.join(CACHE, asset["name"])
            self.progress = 0.0
            self._set(DOWNLOADING)
            try:
                request = urllib.request.Request(
                    asset["browser_download_url"],
                    headers={"User-Agent": f"lmflow/{__version__}"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    total = int(response.headers.get("Content-Length") or 0)
                    done = 0
                    with open(target + ".part", "wb") as fh:
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            fh.write(chunk)
                            done += len(chunk)
                            self.progress = done / total if total else 0.0
                            self._set(DOWNLOADING)
                os.replace(target + ".part", target)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self.log(f"download failed: {exc}")
                self._set(OFFLINE)
                return
            self.downloaded = target
            self.progress = 1.0
            self._set(READY)
        finally:
            self._busy.release()

    # ------------------------------------------------------------- install
    def install_command(self):
        if not self.downloaded:
            return None
        if self.downloaded.endswith(".deb"):
            return ["pkexec", "apt-get", "install", "-y", "--allow-downgrades",
                    self.downloaded]
        if self.downloaded.endswith(".rpm"):
            tool = "dnf" if shutil.which("dnf") else "rpm"
            if tool == "dnf":
                return ["pkexec", "dnf", "install", "-y", self.downloaded]
            return ["pkexec", "rpm", "-Uvh", "--force", self.downloaded]
        return None

    def install_and_restart(self):
        command = self.install_command()
        if command is None:
            return False
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"install failed: {exc}")
            return False
        if result.returncode != 0:
            self.log(f"install failed: {result.stderr.strip()[:300]}")
            return False
        for role in ("server", "client"):
            subprocess.run(["systemctl", "--user", "try-restart", f"lmflow-{role}.service"],
                           capture_output=True)
        return True

    # --------------------------------------------------------------- state
    def _set(self, state, note=None):
        self.state = state
        self.on_state(state, self.progress, note)
