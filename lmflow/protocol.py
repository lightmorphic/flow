"""Tiny framed protocol over one TLS socket."""
from __future__ import annotations

import json
import struct

VERSION = 2          # 2: the pointer is sent as a position, not a movement

HELLO = 1
INPUT = 2
CLIPBOARD = 3
ENTER = 4          # cursor has come onto your screen, at this position
LEAVE = 5          # cursor has gone back to the server
PING = 6
PONG = 7

_HDR = struct.Struct("!BI")
_EVT = struct.Struct("!BHi")


def pack(kind: int, body: bytes) -> bytes:
    return _HDR.pack(kind, len(body)) + body


def pack_json(kind: int, obj) -> bytes:
    return pack(kind, json.dumps(obj).encode("utf-8"))


def pack_events(events) -> bytes:
    body = b"".join(_EVT.pack(t, c, v) for t, c, v in events)
    return pack(INPUT, body)


def unpack_events(body: bytes):
    n = _EVT.size
    return [_EVT.unpack(body[i:i + n]) for i in range(0, len(body) - len(body) % n, n)]


MAX_FRAME = 8 * 1024 * 1024     # the clipboard cap, with room to spare


class Framer:
    """Feed bytes in, get (kind, body) messages out."""

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes):
        self._buf += data
        out = []
        while len(self._buf) >= _HDR.size:
            kind, length = _HDR.unpack_from(self._buf)
            if length > MAX_FRAME:
                raise ValueError(f"frame of {length} bytes refused")
            if len(self._buf) < _HDR.size + length:
                break
            body = self._buf[_HDR.size:_HDR.size + length]
            self._buf = self._buf[_HDR.size + length:]
            out.append((kind, body))
        return out
