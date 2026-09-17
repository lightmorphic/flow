"""Plain-text clipboard sharing. Works on Wayland (wl-clipboard) or X11 (xclip/xsel)."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time


def _tools():
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
    """Polls the local clipboard and calls on_change(text) when it changes."""

    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, on_change, poll_ms=700, log=print):
        self._read_cmd, self._write_cmd = _tools()
        self._on_change = on_change
        self._poll = max(0.1, poll_ms / 1000.0)
        self._log = log
        self._last = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def available(self) -> bool:
        return self._read_cmd is not None

    def start(self):
        if not self.available:
            self._log("clipboard: no wl-clipboard/xclip found, sharing disabled")
            return
        self._last = self._read()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self._poll):
            text = self._read()
            if text is not None and text != self._last:
                self._last = text
                try:
                    self._on_change(text)
                except Exception as exc:                      # pragma: no cover
                    self._log(f"clipboard: send failed: {exc}")

    def _read(self):
        try:
            res = subprocess.run(self._read_cmd, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        data = res.stdout[: self.MAX_BYTES]
        return data.decode("utf-8", "replace")

    def apply(self, text: str):
        """Put text the other machine copied onto this clipboard."""
        if not self.available:
            return
        self._last = text                      # do not echo it straight back
        try:
            subprocess.run(self._write_cmd, input=text.encode("utf-8"),
                           timeout=3, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(f"clipboard: paste failed: {exc}")
