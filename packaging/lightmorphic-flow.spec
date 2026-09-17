Name:           lightmorphic-flow
Version:        0.4.1
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
for role in server client tray; do
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

install -d %{buildroot}%{_sysconfdir}/xdg/autostart
cat > %{buildroot}%{_sysconfdir}/xdg/autostart/uk.lightmorph.Flow.tray.desktop <<'AUTO'
[Desktop Entry]
Type=Application
Name=Lightmorphic Flow tray icon
Comment=Shows where the pointer is and where to send it
Exec=lmflow tray
Icon=uk.lightmorph.Flow
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
AUTO

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
        echo ""
        echo "  +------------------------------------------------------------+"
        echo "  |  LIGHTMORPHIC FLOW IS NOT READY YET                        |"
        echo "  |                                                            |"
        echo "  |  You must LOG OUT and LOG BACK IN before it can read your   |"
        echo "  |  mouse and keyboard. Restarting the computer does it too.   |"
        echo "  |                                                            |"
        echo "  |  Nothing else is needed, and only this once.                |"
        echo "  +------------------------------------------------------------+"
        echo ""
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
%{userunitdir}/lmflow-tray.service
%config(noreplace) %{_sysconfdir}/xdg/autostart/uk.lightmorph.Flow.tray.desktop
%{udevruledir}/60-lmflow.rules
%{_prefix}/lib/modules-load.d/lmflow.conf
%{_datadir}/applications/uk.lightmorph.Flow.desktop
%{_datadir}/metainfo/uk.lightmorph.Flow.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/uk.lightmorph.Flow*.svg
%{_datadir}/icons/hicolor/*/apps/uk.lightmorph.Flow*.png

%changelog
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
