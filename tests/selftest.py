"""Checks the parts that do not need input permissions."""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmflow import config                                # noqa: E402

config.CONFIG_DIR = tempfile.mkdtemp()
config.CONFIG_PATH = os.path.join(config.CONFIG_DIR, "config.json")
config.CERT_PATH = os.path.join(config.CONFIG_DIR, "cert.pem")
config.KEY_PATH = os.path.join(config.CONFIG_DIR, "key.pem")
config.PAIR_PATH = os.path.join(config.CONFIG_DIR, "pairing-open-until")

from lmflow import (client as client_mod, discovery, net, protocol,  # noqa: E402
                    screen, server as server_mod, status)

status.STATUS_PATH = os.path.join(config.CONFIG_DIR, "status.json")
status.COMMAND_PATH = os.path.join(config.CONFIG_DIR, "command")

# Its own port, so a real copy of the app running on this machine does not
# swallow half the test's announcements.
discovery.PORT = 24897

FAILS = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


class FakeConn:
    def __init__(self):
        self.sent = []

    def sendall(self, blob):
        self.sent.append(blob)

    def close(self):
        pass


def make_server(**over):
    cfg = dict(config.DEFAULTS)
    cfg.update({"token": "t", "screen_width": 1000, "screen_height": 800,
                "corner_guard_px": 100, "push_px": 50, "push_ms": 500,
                "discovery": False, "peers": {}})
    cfg.update(over)
    real = screen.detect
    screen.detect = lambda: (1000, 800)
    try:
        srv = server_mod.Server(cfg, log=lambda *_: None)
    finally:
        screen.detect = real
    return srv


def add_peer(srv, ident, edge, size=(1000, 800)):
    peer = server_mod.Peer(ident, ident, FakeConn(), size, edge)
    srv.peers[ident] = peer
    return peer


print("-- crossing rules --")
s = make_server()
right = add_peer(s, "desktop", "right")
s.x, s.y = 990, 400
for _ in range(3):
    s._move(5, 0)
check("light touch on the edge does not cross", s.active is None, f"x={s.x:.0f}")

for _ in range(20):
    s._move(5, 0)
check("a firm push crosses", s.active is right)

for _ in range(30):
    s._move(-5, 0)
check("pushing back returns control", s.active is None)

s = make_server()
add_peer(s, "desktop", "right")
s.x, s.y = 990, 20
for _ in range(40):
    s._move(5, 0)
check("top corner never crosses (hot corner is safe)", s.active is None)

s = make_server()
add_peer(s, "desktop", "right")
s.x, s.y = 990, 790
for _ in range(40):
    s._move(5, 0)
check("bottom corner never crosses", s.active is None)

s = make_server()
add_peer(s, "desktop", "right")
s.x, s.y = 500, 790
for _ in range(40):
    s._move(0, 5)
check("an edge with nobody on it is inert", s.active is None)

s = make_server(push_ms=120)
add_peer(s, "desktop", "right")
s.x, s.y = 990, 400
for _ in range(6):
    s._move(5, 0)
    time.sleep(0.05)
check("a slow drift does not cross", s.active is None)

s = make_server(edge_only_with_hotkey=True)
add_peer(s, "desktop", "right")
s.x, s.y = 990, 400
for _ in range(40):
    s._move(5, 0)
check("hotkey-only mode ignores edges", s.active is None)

print("-- several machines at once --")
s = make_server()
a = add_peer(s, "desktop", "right", (1920, 1080))
b = add_peer(s, "tv", "left", (1280, 720))
c = add_peer(s, "pi", "top", (1024, 768))

s.x, s.y = 990, 400
for _ in range(20):
    s._move(5, 0)
    if s.active is a:
        break
check("right edge reaches the machine on the right", s.active is a)
check("lands just inside its screen, at the same height",
      s.x == 2 and 0 <= s.y <= 1080, f"{s.x},{s.y:.0f} on a {a.size} screen")

for _ in range(30):
    s._move(-5, 0)
s.x, s.y = 10, 400
for _ in range(20):
    s._move(-5, 0)
check("left edge reaches the machine on the left", s.active is b)

for _ in range(40):
    s._move(5, 0)
s.x, s.y = 500, 10
for _ in range(20):
    s._move(0, -5)
check("top edge reaches the machine above", s.active is c)

s.go_local()
s.cycle()
first = s.active
s.cycle()
second = s.active
s.cycle()
third = s.active
s.cycle()
check("the hotkey steps through every machine and home",
      first is a and second is b and third is c and s.active is None)

clip_sent = []
for peer in (a, b, c):
    peer.conn.sent.clear()
s._broadcast_clipboard("shared text", skip="tv")
check("clipboard goes to every machine but the sender",
      len(a.conn.sent) == 1 and len(c.conn.sent) == 1 and len(b.conn.sent) == 0)

s = make_server()
edges = []
for i in range(4):
    edge = s._free_edge()
    edges.append(edge)
    s.cfg["peers"][f"m{i}"] = {"name": f"m{i}", "edge": edge, "enabled": True}
check("a new machine lands on a free edge each time", len(set(edges)) == 4, str(edges))

print("-- finding each other --")
responder = discovery.Responder(lambda: {"app": discovery.MAGIC, "v": 1, "role": "server",
                                         "id": "fake-server", "name": "Desk", "port": 24810,
                                         "pairing": True})
responder.start()
seeker = discovery.Seeker(ignore_id="me")
seeker.start()
time.sleep(3.0)
found = seeker.found(role="server")
check("asking finds the computer with the keyboard",
      any(f["id"] == "fake-server" and f["name"] == "Desk" for f in found), str(found))
check("and the answer carries its address back",
      bool(found and found[0].get("host")), str(found[:1]))

# The reply must come back to the asker's own port, which is what lets it
# through a firewall. Check the seeker never binds the well-known one.
asking_port = seeker._sock.getsockname()[1]
check("the asker listens on a port of its own, not the shared one",
      asking_port not in (0, discovery.PORT), str(asking_port))
seeker.stop()
responder.stop()

print("-- pairing and the encrypted link --")


class FakeDevice:
    def __init__(self):
        self.events = []

    def emit(self, events):
        self.events.extend(events)

    def close(self):
        pass


made = []
client_mod.VirtualDevice = lambda name, pointer: made.append(FakeDevice()) or made[-1]

srv = make_server(port=24899)
srv.running = True
threading.Thread(target=srv._accept_loop, daemon=True).start()
time.sleep(0.8)


def make_client(token, ident="laptop-2"):
    cfg = dict(config.DEFAULTS)
    cfg.update({"role": "client", "server_host": "127.0.0.1", "port": 24899,
                "token": token, "server_fingerprint": "", "discovery": False})
    real = screen.detect
    screen.detect = lambda: (1600, 900)
    cli = client_mod.Client(cfg, log=lambda *_: None)
    screen.detect = real
    cli.id = ident
    cli.name = ident
    cli.clipboard._read_cmd = None
    return cli


unknown = make_client("not-the-right-one")
unknown.running = True
refused = False
try:
    unknown._session()
except Exception:
    refused = True
check("an unknown machine is turned away", refused)

config.open_pairing(60)
joiner = make_client("not-the-right-one")
joiner._open_devices()
threading.Thread(target=joiner.run, daemon=True).start()
time.sleep(1.5)
check("during the pairing window it is let in", len(srv.peers) == 1, str(list(srv.peers)))
check("and it learns the code for next time", joiner.cfg["token"] == "t")
check("and remembers the certificate", len(joiner.cfg["server_fingerprint"]) == 64)
check("the new machine was given an edge",
      srv.cfg["peers"].get("laptop-2", {}).get("edge") in server_mod.EDGES,
      str(srv.cfg["peers"]))
config.close_pairing()

peer = srv.peers.get("laptop-2")
srv.x, srv.y = 999, 400
srv.go_to(peer)
time.sleep(0.4)
pointer = made[0] if made else None
check("cursor was placed on the other screen",
      pointer is not None and len(pointer.events) >= 4)

srv._batch = [(2, 0, 7), (2, 1, -4), (1, 30, 1), (1, 30, 0)]
srv._flush()
time.sleep(0.4)
keyboard = made[1] if len(made) > 1 else None
check("mouse movement arrived", pointer is not None and (2, 0, 7) in pointer.events)
check("key presses arrived", keyboard is not None and (1, 30, 1) in keyboard.events)

got = []
joiner.clipboard.apply = lambda text: got.append(text)
srv._broadcast_clipboard("hello from the laptop")
time.sleep(0.4)
check("clipboard text arrived", got == ["hello from the laptop"], str(got))

known = make_client("t", ident="laptop-3")
known._open_devices()
threading.Thread(target=known.run, daemon=True).start()
time.sleep(1.5)
check("a machine that already knows the code needs no window",
      len(srv.peers) == 2, str(sorted(srv.peers)))
check("two machines are driven at once, on different edges",
      len({p.edge for p in srv.peers.values()}) == 2,
      str({p.name: p.edge for p in srv.peers.values()}))
srv.stop()
known.stop()
joiner.stop()

print("-- what the tray is shown --")
t = make_server()
one = add_peer(t, "desktop", "right")
two = add_peer(t, "shelf", "top")
t.running = True
t.publish()
shown = status.read()
check("the tray is told about every computer",
      {p["name"] for p in shown["peers"]} == {"desktop", "shelf"}, str(shown["peers"]))
check("and that the pointer is at home", shown["active"] is None)

t.x, t.y = 999, 400
t.go_to(one)
shown = status.read()
check("and where the pointer has gone",
      (shown["active"] or {}).get("name") == "desktop", str(shown["active"]))

status.send("home")
t._take_command()
check("the tray can call the pointer home", t.active is None)

status.send(f"goto:{two.id}")
t._take_command()
check("and send it to a named computer", t.active is two)

status.send("next")
t._take_command()
check("and step to the next one", t.active is not two)

status.send(f"drop:{one.id}")
t._take_command()
check("the tray can disconnect one computer", one.id not in t.peers,
      str(sorted(t.peers)))

t.stop()
check("nothing is left behind when it stops", status.read()["peers"] == [])

print()
print("all good" if not FAILS else f"{len(FAILS)} failed: {FAILS}")
sys.exit(1 if FAILS else 0)
