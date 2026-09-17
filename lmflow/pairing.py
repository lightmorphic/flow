"""One short code carries everything the second machine needs."""
from __future__ import annotations

import base64
import json
import socket

from . import config, net


def _lan_address() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("198.51.100.1", 9))
        addr = sock.getsockname()[0]
        sock.close()
        return addr
    except OSError:
        return socket.gethostname()


def make_code(cfg=None) -> str:
    cfg = cfg or config.load()
    payload = {
        "host": _lan_address(),
        "port": int(cfg["port"]),
        "token": cfg["token"],
        "fingerprint": net.ensure_cert(),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def apply_code(code: str) -> dict:
    padded = code.strip() + "=" * (-len(code.strip()) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    cfg = config.load()
    cfg.update({
        "role": "client",
        "server_host": data["host"],
        "port": int(data["port"]),
        "token": data["token"],
        "server_fingerprint": data["fingerprint"],
    })
    config.save(cfg)
    return cfg
