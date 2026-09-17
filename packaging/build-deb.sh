#!/usr/bin/env bash
# Builds lmflow_<version>_all.deb into packaging/out/
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
version="$(python3 -c "import sys; sys.path.insert(0,'$root'); import lmflow; print(lmflow.__version__)")"
stage="$(mktemp -d)"
out="$here/out"
chmod 0755 "$stage"
mkdir -p "$out"

# ---- files -----------------------------------------------------------------
install -d "$stage/usr/lib/python3/dist-packages/lmflow"
install -m 0644 "$root"/lmflow/*.py "$stage/usr/lib/python3/dist-packages/lmflow/"

install -d "$stage/usr/bin"
cat > "$stage/usr/bin/lmflow" <<'LAUNCH'
#!/usr/bin/env python3
import sys
from lmflow.cli import main
sys.exit(main())
LAUNCH
chmod 0755 "$stage/usr/bin/lmflow"

install -d "$stage/usr/lib/systemd/user"
for role in server client; do
  cat > "$stage/usr/lib/systemd/user/lmflow-$role.service" <<UNIT
[Unit]
Description=Lightmorphic Flow ($role)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/lmflow $role
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT
done
chmod 0644 "$stage"/usr/lib/systemd/user/*.service

install -d "$stage/usr/lib/udev/rules.d"
cat > "$stage/usr/lib/udev/rules.d/60-lmflow.rules" <<'RULE'
# Installed by lmflow: let members of the 'input' group send keystrokes
# and pointer movement to this machine.
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
RULE
chmod 0644 "$stage/usr/lib/udev/rules.d/60-lmflow.rules"

install -d "$stage/usr/lib/modules-load.d"
printf 'uinput\n' > "$stage/usr/lib/modules-load.d/lmflow.conf"
chmod 0644 "$stage/usr/lib/modules-load.d/lmflow.conf"

install -d "$stage/usr/share/applications"
cat > "$stage/usr/share/applications/lmflow.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Lightmorphic Flow
Comment=Share one mouse, keyboard and clipboard between two computers
Exec=lmflow gui
Icon=lmflow
Terminal=false
Categories=Utility;Settings;HardwareSettings;
Keywords=mouse;keyboard;clipboard;kvm;share;
DESK
chmod 0644 "$stage/usr/share/applications/lmflow.desktop"

install -d "$stage/usr/share/icons/hicolor/scalable/apps"
install -m 0644 "$here/lmflow.svg" \
  "$stage/usr/share/icons/hicolor/scalable/apps/lmflow.svg"

install -d "$stage/usr/share/doc/lmflow"
install -m 0644 "$root/README.md" "$stage/usr/share/doc/lmflow/README.md"
install -m 0644 "$root/LICENSE" "$stage/usr/share/doc/lmflow/copyright"

# ---- control ---------------------------------------------------------------
install -d "$stage/DEBIAN"
size="$(du -sk "$stage" | cut -f1)"
cat > "$stage/DEBIAN/control" <<CTRL
Package: lightmorphic-flow
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, openssl, systemd
Recommends: wl-clipboard | xclip
Installed-Size: $size
Homepage: https://flow.lightmorphic.com
Conflicts: deskmorphic
Replaces: deskmorphic
Provides: deskmorphic
Maintainer: Charlie <claude@charlie.cx>
Description: Share one mouse, keyboard and clipboard between Linux computers
 Lightmorphic Flow lets one keyboard and mouse drive up to four other machines
 on the same network - one on each edge of your screen - and keeps the text
 clipboard in step between them. They find each other by themselves.
 .
 It never takes over a screen edge: a light touch on an edge or a corner does
 nothing, so desktop hot corners and hot edges keep working. Only a deliberate
 push moves the pointer across. Nothing is grabbed while you are working on
 your own machine.
CTRL

cat > "$stage/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e

if [ "$1" = "configure" ]; then
    getent group input >/dev/null || addgroup --system input || true

    # Add the person who installed this, so they do not have to think about it.
    for candidate in "$SUDO_USER" "$PKEXEC_UID"; do
        [ -n "$candidate" ] || continue
        case "$candidate" in
            [0-9]*) candidate="$(getent passwd "$candidate" | cut -d: -f1)" ;;
        esac
        [ -n "$candidate" ] || continue
        if ! id -nG "$candidate" 2>/dev/null | tr ' ' '\n' | grep -qx input; then
            adduser "$candidate" input >/dev/null 2>&1 || usermod -aG input "$candidate" || true
            echo "Lightmorphic Flow: added $candidate to the 'input' group."
            echo "Lightmorphic Flow: log out and back in once before starting it."
        fi
    done

    modprobe uinput >/dev/null 2>&1 || true
    if [ -d /run/udev ]; then
        udevadm control --reload-rules >/dev/null 2>&1 || true
        udevadm trigger --subsystem-match=input --subsystem-match=misc >/dev/null 2>&1 || true
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [ -x /usr/bin/gtk-update-icon-cache ]; then
        gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
    fi
fi

exit 0
POST

cat > "$stage/DEBIAN/postrm" <<'PRM'
#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if [ -d /run/udev ]; then
        udevadm control --reload-rules >/dev/null 2>&1 || true
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
fi

if [ "$1" = "purge" ]; then
    echo "Lightmorphic Flow: your settings in ~/.config/lmflow were left alone."
fi

exit 0
PRM

cat > "$stage/DEBIAN/prerm" <<'PRE'
#!/bin/sh
set -e
exit 0
PRE

chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/postrm" "$stage/DEBIAN/prerm"

deb="$out/lightmorphic-flow_${version}_all.deb"
fakeroot dpkg-deb --build --root-owner-group "$stage" "$deb" >/dev/null
rm -rf "$stage"
echo "$deb"
