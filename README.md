# Lightmorphic Flow

[flow.lightmorphic.com](https://flow.lightmorphic.com)

> **Beta.** This is an early release. It does what is described here, but it
> has not been through much use on other people's machines yet, so expect
> rough edges and do not lean on it for anything that matters. It takes over
> your mouse and keyboard while the pointer is on another computer; closing
> the program always gives them straight back, and `Ctrl+Alt+Shift+K` brings
> everything home at once.

One mouse, one keyboard and one clipboard across your Linux computers.
Up to four others at once - one on each edge of your screen.
Pure Python 3, no libraries to install, about 1,700 lines.

## What it does differently

* **The clipboard actually works.** Copy on any of the machines, paste on any
  of the others.
* **Your screen edges stay yours.** Flow never claims an edge. A light touch on
  an edge or a corner does nothing at all, so GNOME hot corners and hot edges
  behave exactly as they always did. Only a deliberate continued push - about
  90 pixels' worth, within half a second - moves the pointer across. Corners
  are ignored completely.
* **It finds the other computers itself.** They announce themselves on your
  network. No addresses to type.
* **You decide where each one sits.** Right, left, above or below - one machine
  per edge, so pushing off that edge always reaches the same computer.
* **Nothing is grabbed while you are on your own machine.** Touchpad gestures,
  pointer acceleration and shortcuts are untouched until the pointer has
  actually left your screen.

## Install (on every machine)

```
sudo apt install ./lightmorphic-flow_0.2.2_all.deb
```

That puts everything in place, including permission to read the mouse and
keyboard, and adds you to the `input` group. **Log out and back in once**, and
that is the whole install.

On Fedora, openSUSE and anything else that uses `.rpm`:

```
sudo dnf install ./lightmorphic-flow-0.2.2-1.noarch.rpm
```

To build either package yourself:

```
./packaging/build-deb.sh
./packaging/build-rpm.sh     # needs the 'rpm' package installed
```

On a Linux that does not use `.deb`, install into your home folder instead with
`./install.sh`, then `lmflow setup`, then log out and back in.

## Setting it up

On the machine with the mouse and keyboard, open **Lightmorphic Flow** from your
applications, leave the role as "Share my mouse and keyboard", and press
**Allow a new computer**.

On the other machine, open it, set the role to "Be controlled by another
computer", pick the machine from the list it found, and turn on the switch at
the top.

Do that once per computer. Then say where each one sits, in the list on the
first machine: to my right, to my left, above me, below me.

From a terminal, the same thing:

```
lmflow allow                 # on the machine with the mouse
lmflow scan                  # see what is on the network
lmflow peers                 # list the computers you control
lmflow where desktop right   # say where one of them sits
lmflow forget desktop        # remove one
```

If the two are not on the same network - over Tailscale, say - discovery will
not reach, so use the pairing code in the settings window instead.

## Settings

Everything is in the settings window and saves as you change it. The same
values live in `~/.config/lmflow/config.json`:

| Setting | What it does |
| --- | --- |
| `peers` | Each known computer and which edge it sits on |
| `corner_guard_px` | How much of each edge, next to the corners, never crosses |
| `push_px` / `push_ms` | How hard and how quickly you must push to cross |
| `edge_only_with_hotkey` | Ignore edges completely; use the hotkey only |
| `share_clipboard` | Copy and paste between the machines |
| `discovery` | Announce yourself on the network |
| `grab_touchpads` | Use the laptop touchpad on the other machines too |
| `pointer_speed` | Pointer speed once you are on another machine |

## Hotkeys

* `Ctrl+Alt+S` - step through each computer in turn and back to this one.
* `Ctrl+Alt+Shift+K` - panic key. Brings everything back to this machine.

If it ever stops responding while the pointer is away, killing it releases the
mouse and keyboard immediately.

## Security

Each link is encrypted. A new computer is only let in while you have pressed
Allow, and after that it is remembered by a shared code and a pinned
certificate. Machines you have not allowed are turned away.

## What it does not do

Files, drag and drop, images on the clipboard, and Windows or macOS. Text
clipboard only. That is the point.

## Removing it

```
sudo apt remove lightmorphic-flow
```

Your settings in `~/.config/lmflow` are left alone.

## Licence

GPL-3.0-or-later. The full text is in `LICENSE`.

## Checking it works

```
python3 tests/selftest.py
```

Twenty-six checks covering the crossing rules, the corner guard, several
machines at once, discovery, pairing, the encrypted link and the clipboard -
none of which need input permissions.
