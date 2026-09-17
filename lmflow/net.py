"""TLS transport. Self-signed certificate, pinned by fingerprint, plus a shared token."""
from __future__ import annotations

import hashlib
import os
import socket
import ssl
import subprocess

from . import config


def ensure_cert() -> str:
    """Create the server certificate if missing; return its SHA-256 fingerprint."""
    os.makedirs(config.CONFIG_DIR, mode=0o700, exist_ok=True)
    if not (os.path.exists(config.CERT_PATH) and os.path.exists(config.KEY_PATH)):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", config.KEY_PATH, "-out", config.CERT_PATH,
             "-days", "3650", "-subj", "/CN=lmflow"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.chmod(config.KEY_PATH, 0o600)
    return fingerprint_of_file(config.CERT_PATH)


def fingerprint_of_file(path: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(open(path, encoding="ascii").read())
    return hashlib.sha256(der).hexdigest()


def server_context() -> ssl.SSLContext:
    ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(config.CERT_PATH, config.KEY_PATH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # pinned by fingerprint instead
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def peer_fingerprint(sock: ssl.SSLSocket) -> str:
    return hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()


def tune(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
