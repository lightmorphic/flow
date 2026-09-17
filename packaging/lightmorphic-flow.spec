Name:           lightmorphic-flow
Version:        0.2.0
Release:        1%{?dist}
Summary:        Share one mouse, keyboard and clipboard between Linux computers

License:        GPL-3.0-or-later
URL:            https://flow.lightmorphic.com
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# Distro-neutral locations, so this builds the same anywhere.
%global flowlibdir  /usr/lib/lightmorphic-flow
%global userunitdir /usr/lib/systemd/user
%global udevruledir /usr/lib/udev/rules.d

Requires:       python3 >= 3.9
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       openssl
Requires:       systemd
Recommends:     wl-clipboard
Obsoletes:      deskmorphic < 0.2.0
Provides:       deskmorphic = %{version}-%{release}

%description
Lightmorphic Flow lets one keyboard and mouse drive up to four other machines
on the same network - one on each edge of your screen - and keeps the text
clipboard in step between them. They find each other by themselves.

It never takes over a screen edge: a light touch on an edge or a corner does
nothing, so desktop hot corners and hot edges keep working. Only a deliberate
push moves the pointer across. Nothing is grabbed while you are working on
your own machine.

%prep
%setup -q

%build
# Nothing to build: plain Python.

%install
install -d %{buildroot}%{flowlibdir}/lmflow
install -m 0644 lmflow/*.py %{buildroot}%{flowlibdir}/lmflow/

install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/lmflow <<'LAUNCH'
#!/usr/bin/env python3
import sys
sys.path.insert(0, "/usr/lib/lightmorphic-flow")
from lmflow.cli import main
sys.exit(main())
LAUNCH
chmod 0755 %{buildroot}%{_bindir}/lmflow

install -d %{buildroot}%{userunitdir}
for role in server client; do
cat > %{buildroot}%{userunitdir}/lmflow-$role.service <<UNIT
[Unit]
Description=Lightmorphic Flow ($role)
After=graphical-session.target

[Service]
Type=simple
ExecStart=%{_bindir}/lmflow $role
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT
done

install -d %{buildroot}%{udevruledir}
cat > %{buildroot}%{udevruledir}/60-lmflow.rules <<'RULE'
# Installed by Lightmorphic Flow: let members of the 'input' group send
# keystrokes and pointer movement to this machine.
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
RULE

install -d %{buildroot}%{_prefix}/lib/modules-load.d
printf 'uinput\n' > %{buildroot}%{_prefix}/lib/modules-load.d/lmflow.conf

install -d %{buildroot}%{_datadir}/applications
cat > %{buildroot}%{_datadir}/applications/lmflow.desktop <<'DESK'
[Desktop Entry]
Type=Application
Name=Lightmorphic Flow
Comment=Share one mouse, keyboard and clipboard between computers
Exec=lmflow gui
Icon=lmflow
Terminal=false
Categories=Utility;Settings;HardwareSettings;
Keywords=mouse;keyboard;clipboard;kvm;share;
DESK

install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -m 0644 packaging/lmflow.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/lmflow.svg

%post
getent group input >/dev/null || groupadd -r input || :
for candidate in "$SUDO_USER" "$PKEXEC_UID"; do
    [ -n "$candidate" ] || continue
    case "$candidate" in
        [0-9]*) candidate="$(getent passwd "$candidate" | cut -d: -f1)" ;;
    esac
    [ -n "$candidate" ] || continue
    if ! id -nG "$candidate" 2>/dev/null | tr ' ' '\n' | grep -qx input; then
        usermod -aG input "$candidate" || :
        echo "Lightmorphic Flow: added $candidate to the 'input' group."
        echo "Lightmorphic Flow: log out and back in once before starting it."
    fi
done
modprobe uinput >/dev/null 2>&1 || :
if [ -d /run/udev ]; then
    udevadm control --reload-rules >/dev/null 2>&1 || :
    udevadm trigger --subsystem-match=input --subsystem-match=misc >/dev/null 2>&1 || :
fi
systemctl daemon-reload >/dev/null 2>&1 || :

%postun
if [ -d /run/udev ]; then
    udevadm control --reload-rules >/dev/null 2>&1 || :
fi

%files
%license LICENSE
%doc README.md
%{_bindir}/lmflow
%{flowlibdir}/
%{userunitdir}/lmflow-server.service
%{userunitdir}/lmflow-client.service
%{udevruledir}/60-lmflow.rules
%{_prefix}/lib/modules-load.d/lmflow.conf
%{_datadir}/applications/lmflow.desktop
%{_datadir}/icons/hicolor/scalable/apps/lmflow.svg

%changelog
* Wed Sep 17 2025 Charlie <claude@charlie.cx> - 0.2.0-1
- Finds other computers on the network by itself
- Drives up to four machines at once, one on each screen edge
- Screen edges and corners are left to the desktop
