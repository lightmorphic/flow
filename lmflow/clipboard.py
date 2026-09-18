"""Sharing the clipboard, with no outside tools and nothing polling.

A helper connects to the desktop once and is told when something is copied.
The old way - asking what is on the clipboard twice a second - meant starting
a small program twice a second, which a Wayland dock shows as an icon flashing
on and off, and which needed wl-clipboard or xclip to be installed at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading


def _fallback_tools():
    """Only used where the helper cannot run at all."""
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        return (["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
                ["wl-copy", "--type", "text/plain;charset=utf-8"])
    if shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard", "-o"],
                ["xclip", "-selection", "clipboard", "-i"])
    if shutil.which("xsel"):
        return (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"])
    return (None, None)


class Clipboard:
    """Calls on_change(text) when something is copied here."""

    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, on_change, poll_ms=1500, log=print):
        self._on_change = on_change
        self._poll = max(0.5, poll_ms / 1000.0)
        self._log = log
        self._last = None
        self._stop = threading.Event()
        self._thread = None
        self._helper = None
        self.route = "not started"
        self._read_cmd, self._write_cmd = _fallback_tools()

    @property
    def available(self) -> bool:
        return True                    # the helper works without anything installed

    # ------------------------------------------------------------- starting
    def _helper_command(self):
        binary = "/usr/bin/lmflow"
        if os.path.exists(binary):
            return [binary, "clipwatch"]
        return [sys.executable, "-m", "lmflow", "clipwatch"]

    def start(self):
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        helper = self._helper
        if helper is not None:
            try:
                helper.terminate()
            except OSError:
                pass

    # -------------------------------------------------------------- watching
    def _environment(self):
        """On Wayland, a program without a focused window may not see the
        clipboard at all - that is the rule, not a fault. GNOME keeps the
        clipboard of its X11 compatibility layer in step with the real one,
        and that one has no such rule, so the helper goes through there."""
        env = dict(os.environ)
        on_wayland = (os.environ.get("XDG_SESSION_TYPE") == "wayland"
                      or bool(os.environ.get("WAYLAND_DISPLAY")))
        if on_wayland and os.environ.get("DISPLAY"):
            env["GDK_BACKEND"] = "x11"
            self.route = "through X11 compatibility"
        elif on_wayland:
            self.route = "on Wayland without X11 compatibility - may not work"
        else:
            self.route = "on X11"
        return env

    def _watch(self):
        failures = 0
        env = self._environment()
        self._log(f"clipboard: watching {self.route}")
        while not self._stop.is_set():
            try:
                self._helper = subprocess.Popen(
                    self._helper_command(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            except OSError as exc:
                self._log(f"clipboard: cannot start the watcher ({exc})")
                self._fall_back()
                return

            buffer = b""
            while not self._stop.is_set():
                chunk = self._helper.stdout.read(1)
                if not chunk:
                    break
                if chunk == b"\0":
                    self._arrived(buffer.decode("utf-8", "replace"))
                    buffer = b""
                elif len(buffer) < self.MAX_BYTES:
                    buffer += chunk

            if self._stop.is_set():
                return
            failures += 1
            if failures >= 3:
                self._log("clipboard: the watcher will not stay up here")
                self._fall_back()
                return
            self._stop.wait(2.0)

    def _arrived(self, text):
        if text == self._last:
            return
        self._last = text
        try:
            self._on_change(text)
        except Exception as exc:                              # pragma: no cover
            self._log(f"clipboard: send failed: {exc}")

    # ------------------------------------------------------------- applying
    def apply(self, text: str):
        """Put text the other machine copied onto this clipboard."""
        self._last = text                  # do not echo it straight back
        helper = self._helper
        if helper is not None and helper.poll() is None:
            try:
                helper.stdin.write(text.encode("utf-8") + b"\0")
                helper.stdin.flush()
                return
            except (OSError, ValueError):
                pass
        if self._write_cmd:
            try:
                subprocess.run(self._write_cmd, input=text.encode("utf-8"),
                               timeout=3, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as exc:
                self._log(f"clipboard: paste failed: {exc}")

    # ---------------------------------- the old way, only if nothing else works
    def _fall_back(self):
        if not self._read_cmd:
            self._log("clipboard: sharing is not available on this desktop")
            return
        self._log("clipboard: checking now and then instead")
        while not self._stop.wait(self._poll):
            text = self._read()
            if text is not None:
                self._arrived(text)

    def _read(self):
        try:
            res = subprocess.run(self._read_cmd, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        return res.stdout[: self.MAX_BYTES].decode("utf-8", "replace")
