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
from lmflow.hotkeys import HotkeyWatcher  # noqa: E402

server_mod.HotkeyWatcher = HotkeyWatcher

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
    srv._scan_devices = lambda: None
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
check("lands well inside its screen, not against the edge",
      40 <= s.x <= 200 and 0 <= s.y <= 1080, f"{s.x},{s.y:.0f} on a {a.size} screen")

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

time.sleep(0.3)                      # let the earlier traffic drain first
for peer in (a, b, c):
    peer.conn.sent.clear()
s._broadcast_clipboard("shared text", skip="tv")
time.sleep(0.3)                      # the writer threads deliver it
check("clipboard goes to every machine but the sender",
      len(a.conn.sent) == 1 and len(c.conn.sent) == 1 and len(b.conn.sent) == 0,
      f"{len(a.conn.sent)}/{len(b.conn.sent)}/{len(c.conn.sent)}")

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
client_mod.VirtualDevice = (lambda name, pointer, absolute=False:
                            made.append(FakeDevice()) or made[-1])

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


old = make_client("t", ident="ancient")
old.protocol = 1                       # pretend to be an older copy
old.running = True
refused_old = False
try:
    old._session()
except Exception:
    refused_old = True
old.running = False
check("a machine running an older version is turned away, not left broken",
      refused_old)

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
      pointer is not None and len(pointer.events) >= 2,
      str(pointer.events[-2:]) if pointer else "no device")

srv.x, srv.y = 800, 450
srv._moved = True
srv._batch = [(1, 30, 1), (1, 30, 0)]
srv._flush()
time.sleep(0.4)
keyboard = made[1] if len(made) > 1 else None
from lmflow.linux_input import ABS_RANGE, ABS_X, ABS_Y, EV_ABS
abs_sent = [e for e in (pointer.events if pointer else []) if e[0] == EV_ABS]
check("the pointer's position arrived, not its movement", bool(abs_sent),
      str(abs_sent[-2:]))
check("and it is on the agreed scale",
      all(0 <= v <= ABS_RANGE for _t, _c, v in abs_sent))
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

print("-- arriving does not bounce straight back --")
b = make_server()
twitchy = add_peer(b, "desktop", "right", (1128, 752))
b.x, b.y = 999, 400
for _ in range(40):
    b._move(5, 0)
    if b.active is twitchy:
        break
check("across", b.active is twitchy, f"landed at x={b.x:.0f}")
for _ in range(12):                    # the hand twitches back the way it came
    b._move(-2, 1)
check("a twitch on arrival does not send it straight home", b.active is twitchy,
      f"x={b.x:.0f}")

print("-- coming home is easier than leaving --")
e = make_server()
near = add_peer(e, "desktop", "right", (1000, 800))
e.x, e.y = 999, 400
nudges_out = 0
for _ in range(60):
    e._move(3, 0)
    nudges_out += 1
    if e.active is near:
        break
check("leaving takes a firm push", e.active is near and nudges_out > 10,
      f"{nudges_out} small movements")
e.x, e.y = 999, 400
nudges_home = 0
for _ in range(60):
    e._move(3, 0)
    nudges_home += 1
    if e.active is None:
        break
check("coming home takes only a nudge", e.active is None and nudges_home < nudges_out,
      f"{nudges_home} to come home against {nudges_out} to leave")

# And slowly, without the push being forgotten halfway.
e.x, e.y = 999, 400
for _ in range(20):
    e._move(3, 0)
    if e.active is near:
        break
check("across again", e.active is near)
e.x, e.y = 999, 400
for _ in range(10):
    e._move(3, 0)
    time.sleep(0.12)
    if e.active is None:
        break
check("and a slow, gentle nudge home still works", e.active is None)

print("-- getting back --")
r = make_server()
far = add_peer(r, "desktop", "right", (1920, 1080))
r.x, r.y = 999, 400
for _ in range(20):
    r._move(5, 0)
    if r.active is far:
        break
check("the pointer went across", r.active is far)

# Stuck in the top-left corner of the other screen: every edge must bring you
# home, corners included, or there is no way back.
r.x, r.y = 0, 0
for _ in range(40):
    r._move(0, -5)
check("pushing up from a corner comes home", r.active is None)

r.x, r.y = 999, 400
for _ in range(20):
    r._move(5, 0)
    if r.active is far:
        break
check("across again", r.active is far)
r.x, r.y = 1919, 540
for _ in range(40):
    r._move(5, 0)
    if r.active is None:
        break
check("so does the far edge, not only the one you came in by", r.active is None)

print("-- the computer you are sitting at must never freeze --")
class Deaf:
    """A machine that stops reading: a real send would block for ever."""
    def __init__(self): self.stuck = threading.Event()
    def sendall(self, blob): self.stuck.wait()
    def close(self): self.stuck.set()

f = make_server()
deaf = server_mod.Peer("deaf", "deaf", Deaf(), (1000, 800), "right")
f.peers["deaf"] = deaf
f.x, f.y = 999, 400
f.go_to(deaf)
start = time.monotonic()
for _ in range(server_mod.Peer.OUTBOX + 50):
    f._moved = True
    f._flush()
spent = time.monotonic() - start
check("sending to a machine that has stopped reading never blocks", spent < 2.0,
      f"{spent:.2f}s for {server_mod.Peer.OUTBOX + 50} frames")
check("and that machine is marked as not keeping up", not deaf.alive)
f._watch_the_peer(time.monotonic())
check("so the pointer comes back by itself", f.active is None)

esc = server_mod.HotkeyWatcher()
brought_home = []
esc.bind_panic(lambda: brought_home.append(True))
for _ in range(4):
    esc.feed(1, 1); esc.feed(1, 0)
check("four escapes do nothing", not brought_home)
esc.feed(1, 1)
check("five escapes bring it home", bool(brought_home))

q = make_server()
quiet = add_peer(q, "desktop", "right")
quiet.heard = time.monotonic() - 60        # connected a while ago, said nothing
q.x, q.y = 999, 400
q.go_to(quiet)
check("pointer is away", q.active is quiet)
q._watch_the_peer(time.monotonic())
check("crossing to a quiet machine does not bounce straight back",
      q.active is quiet, "it came back immediately")
quiet.heard = time.monotonic() - (server_mod.SILENCE_SECONDS + 1)
q._watch_the_peer(time.monotonic())
check("a computer that goes quiet gives the mouse back", q.active is None)

class FakeReader:
    def __init__(self): self.grabbed = True
    def ungrab(self): self.grabbed = False

w = make_server()
w.running = True
w._readers = {"a": FakeReader(), "b": FakeReader()}
w._alive_at = time.monotonic() - (server_mod.WATCHDOG_SECONDS + 2)
threading.Thread(target=w._watchdog, daemon=True).start()
time.sleep(1.4)
check("if it stops responding it lets go of the keyboard anyway",
      not any(r.grabbed for r in w._readers.values()))
w.running = False

print("-- the second crossing --")
d = make_server()
far = add_peer(d, "framework", "left", (1128, 752))
d.x, d.y = 5, 400
for _ in range(40):
    d._move(-5, 0)
    if d.active is far:
        break
check("across the first time", d.active is far)
for _ in range(40):
    d._move(5, 0)
    if d.active is None:
        break
check("and home", d.active is None)
# Now use the desktop: well across to the right and most of the way back, as
# the real pointer would go - it is sped up, so the real one reaches the left
# edge sooner than raw movement suggests.
for _ in range(60):
    d._move(8, 0)
for _ in range(60):
    d._move(-6, 0)
pushes = 0
for _ in range(400):
    d._move(-5, 0)
    pushes += 1
    if d.active is far:
        break
check("the second crossing does not need an endless push", d.active is far and pushes < 120,
      f"{pushes} small pushes")

print("-- letting go really lets go --")
import glob as _glob
from lmflow.linux_input import DeviceInfo, InputReader, VirtualDevice as RealDevice
try:
    probe = RealDevice("Flow selftest release probe", pointer=True)
    time.sleep(1.0)
    node = next(("/dev/input/" + os.path.basename(p)
                 for p in sorted(_glob.glob("/sys/class/input/event*"))
                 if open(os.path.join(p, "device", "name")).read().strip()
                 == "Flow selftest release probe"), None)
    holder = InputReader(DeviceInfo(node, "probe", "mouse"))
    other = InputReader(DeviceInfo(node, "probe", "mouse"))
    holder.grab()
    held = not server_mod._free(other)
    released = holder.ungrab()
    check("a device we hold is really held", held)
    check("and letting go of it really lets go",
          released and server_mod._free(other))
    holder.close(); other.close(); probe.close()
except Exception as exc:
    print(f"SKIP  release check needs /dev/uinput ({exc})")

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
