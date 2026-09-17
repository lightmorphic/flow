"""A single long-lived helper that watches the clipboard and sets it.

Asking what is on the clipboard twice a second means starting a small program
twice a second, and on Wayland a desktop dock shows every one of those as an
icon flashing on and off. This connects once and waits to be told.

It also means no wl-clipboard and no xclip is needed: the desktop's own
toolkit does both jobs, on X11 and Wayland alike.

    out: each new clipboard text, followed by a zero byte
    in:  text to put on the clipboard, followed by a zero byte
"""
from __future__ import annotations

import sys
import threading


def main():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    GLib.set_prgname("uk.lightmorph.Flow")
    GLib.set_application_name("Lightmorphic Flow")
    Gtk.init()

    display = Gdk.Display.get_default()
    if display is None:
        print("no display to watch the clipboard on", file=sys.stderr)
        return 1
    clipboard = display.get_clipboard()
    out = sys.stdout.buffer
    ours = [None]                      # what we last put there ourselves

    def finished(board, result):
        try:
            text = board.read_text_finish(result)
        except GLib.Error:
            return
        if text is None or text == ours[0]:
            return
        try:
            out.write(text.encode("utf-8") + b"\0")
            out.flush()
        except (BrokenPipeError, OSError):
            loop.quit()

    def changed(board, *_args):
        board.read_text_async(None, finished)

    def quit_now():
        loop.quit()
        return False

    def put(text):
        ours[0] = text
        clipboard.set(text)
        return False

    def listen():
        buffer = b""
        while True:
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                break
            if chunk == b"\0":
                GLib.idle_add(put, buffer.decode("utf-8", "replace"))
                buffer = b""
            else:
                buffer += chunk
        GLib.idle_add(quit_now)

    loop = GLib.MainLoop()

    def seeded(board, result):
        """Whatever is already on the clipboard is not news: do not send it."""
        try:
            ours[0] = board.read_text_finish(result)
        except GLib.Error:
            ours[0] = None
        board.connect("changed", changed)

    clipboard.read_text_async(None, seeded)
    threading.Thread(target=listen, daemon=True).start()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    return 0
