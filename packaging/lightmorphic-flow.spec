Name:           lightmorphic-flow
Version:        0.8.1
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
# Without these the update dot is simply never drawn.
Requires:       python3-cairo
Requires:       gtk4
Requires:       libadwaita
Requires:       gtk3
Requires:       libayatana-appindicator-gtk3
Requires:       openssl
Requires:       systemd
Suggests:       wl-clipboard
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
for role in server client tray; do
cat > %{buildroot}%{userunitdir}/lmflow-$role.service <<UNIT
[Unit]
Description=Lightmorphic Flow ($role)
After=graphical-session.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart=%{_bindir}/lmflow $role
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT
done

install -d %{buildroot}%{udevruledir}
cat > %{buildroot}%{udevruledir}/60-lmflow.rules <<'RULE'
# Installed by Lightmorphic Flow.
#
# "uaccess" hands the device to whoever is signed in at this computer, the
# moment the rule is installed - no group membership and no logging out. The
# group is kept as a fallback for a machine where that does not apply, such as
# a remote or headless session.
#
# This does mean any program you run can read what you type. On a computer with
# one user that is the trade for not having to log out; on a shared machine,
# delete the second line and use the group instead.
KERNEL=="uinput", MODE="0660", GROUP="input", TAG+="uaccess", OPTIONS+="static_node=uinput"
SUBSYSTEM=="input", KERNEL=="event*", MODE="0660", GROUP="input", TAG+="uaccess"
RULE

install -d %{buildroot}%{_prefix}/lib/modules-load.d
printf 'uinput\n' > %{buildroot}%{_prefix}/lib/modules-load.d/lmflow.conf

install -d %{buildroot}%{_datadir}/applications
cat > %{buildroot}%{_datadir}/applications/uk.lightmorph.Flow.desktop <<'DESK'
[Desktop Entry]
Type=Application
Name=Lightmorphic Flow
Comment=Share one mouse, keyboard and clipboard between computers
Exec=lmflow gui
Icon=uk.lightmorph.Flow
Terminal=false
Categories=Utility;
Keywords=mouse;keyboard;clipboard;kvm;share;
StartupNotify=true
DESK

install -d %{buildroot}%{_sysconfdir}/ufw/applications.d
cat > %{buildroot}%{_sysconfdir}/ufw/applications.d/lightmorphic-flow <<'UFW'
[Lightmorphic-Flow]
title=Lightmorphic Flow
description=Share one mouse, keyboard and clipboard between your computers
ports=24810/tcp|24811/udp
UFW

install -d %{buildroot}%{_prefix}/lib/firewalld/services
cat > %{buildroot}%{_prefix}/lib/firewalld/services/lightmorphic-flow.xml <<'FWD'
<?xml version="1.0" encoding="utf-8"?>
<service>
  <short>Lightmorphic Flow</short>
  <description>Share one mouse, keyboard and clipboard between your computers.</description>
  <port protocol="tcp" port="24810"/>
  <port protocol="udp" port="24811"/>
</service>
FWD

install -d %{buildroot}%{_datadir}/metainfo
install -m 0644 packaging/uk.lightmorph.Flow.metainfo.xml %{buildroot}%{_datadir}/metainfo/

install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
for icon in uk.lightmorph.Flow uk.lightmorph.Flow-away; do
    install -m 0644 packaging/$icon.svg \
        %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/$icon.svg
    for s in 48 64 128 256 512; do
        install -d %{buildroot}%{_datadir}/icons/hicolor/${s}x${s}/apps
        install -m 0644 packaging/icons/$s/$icon.png \
            %{buildroot}%{_datadir}/icons/hicolor/${s}x${s}/apps/$icon.png
    done
done


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
        echo "Lightmorphic Flow: added $candidate to the 'input' group as a fallback."
    fi
done
modprobe uinput >/dev/null 2>&1 || :
if [ -d /run/udev ]; then
    udevadm control --reload-rules >/dev/null 2>&1 || :
    udevadm trigger --subsystem-match=input --subsystem-match=misc >/dev/null 2>&1 || :
fi
systemctl daemon-reload >/dev/null 2>&1 || :
    systemctl --global enable lmflow-tray.service >/dev/null 2>&1 || :
rm -f %{_sysconfdir}/xdg/autostart/uk.lightmorph.Flow.tray.desktop
echo ""
echo "  Lightmorphic Flow is ready. Open it from your applications."
echo "  If it tells you it cannot read your mouse, log out and back in once."
echo ""

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
%{userunitdir}/lmflow-tray.service
%config(noreplace) %{_sysconfdir}/ufw/applications.d/lightmorphic-flow
%{_prefix}/lib/firewalld/services/lightmorphic-flow.xml
%{udevruledir}/60-lmflow.rules
%{_prefix}/lib/modules-load.d/lmflow.conf
%{_datadir}/applications/uk.lightmorph.Flow.desktop
%{_datadir}/metainfo/uk.lightmorph.Flow.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/uk.lightmorph.Flow*.svg
%{_datadir}/icons/hicolor/*/apps/uk.lightmorph.Flow*.png

%changelog
* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.8.1-1
- Exit from the tray menu stops the whole app

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.8.0-1
- Flow drives the main computer's pointer, so its edges are exact

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.12-1
- No more jumping across a quarter of a screen early

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.11-1
- GNOME's top-left hot corner opens the Overview on the other computer

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.10-1
- Clicking the update dot no longer maximises the window

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.9-1
- Only the facing edge comes home; hot corners work there; tray icon on GNOME

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.8-1
- Finds the other computer through a strict firewall

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.7-1
- Crossing over no longer feels like pushing through a wall

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.6-1
- The clipboard works on GNOME with Wayland, and crossing is less fussy

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.5-1
- Crossing a second time works, and Flow never takes hold of its own devices

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.4-1
- Letting go of the mouse and keyboard really lets go

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.3-1
- The pointer is marked as a pointer; a fallback for desktops that refuse it

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.2-1
- Refuses a machine running a version it cannot work with, and says so

* Fri Sep 18 2026 Lightmorphic <github@lightmorphic.com> - 0.7.1-1
- Coming home is a nudge, not a shove

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.7.0-1
- The pointer can find its way back, and the clipboard needs no other tools

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.8-1
- Says when the other computer's identity has changed, and offers to trust it

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.7-1
- Finds computers on networks that are not a /24, and upgrades restart it

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.6-1
- A service that cannot start gives up instead of flashing for ever

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.5-1
- No more stray flickering icon in the dock

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.4-1
- Says exactly what it does when the pointer crosses and comes back

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.3-1
- The pointer no longer bounces straight back the moment it crosses

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.2-1
- Logs as it happens, and a report command

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.1-1
- The computer you are sitting at can no longer be left frozen

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.6.0-1
- The controlled computer asks, so a firewall there needs no change

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.5.3-1
- Says when a firewall is blocking the two computers, and offers to open it

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.5.2-1
- Allow counts down in the row; no popups

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.5.1-1
- The tray icon actually appears, with connect and disconnect

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.5.0-1
- Works the moment it is installed; no logging out

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.4.1-1
- Screenshots and a proper listing in every software centre

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.4.0-1
- Says plainly, in a dialog you must acknowledge, that you have to log out

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.3.5-1
- The doctor reports the tray library correctly

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.3.4-1
- Declare the cairo bindings the update dot is drawn with

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.3.3-1
- A doctor command, and the update dot can no longer vanish silently

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.3.2-1
- The update dot follows the revised house standard

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.3.1-1
- The update dot sits opposite the logo again

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.3.0-1
- A tray icon showing where the pointer is, and sending it elsewhere

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.2.2-1
- The update dot follows the house standard exactly

* Thu Sep 17 2026 Lightmorphic <github@lightmorphic.com> - 0.2.1-1
- Proper application icon and a listing in the software centre

* Wed Sep 17 2025 Lightmorphic <github@lightmorphic.com> - 0.2.0-1
- Finds other computers on the network by itself
- Drives up to four machines at once, one on each screen edge
- Screen edges and corners are left to the desktop
