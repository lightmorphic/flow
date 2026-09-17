#!/usr/bin/env bash
# Puts a 'lmflow' command in ~/.local/bin and registers the background service.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/lmflow" <<LAUNCH
#!/usr/bin/env bash
exec python3 -m lmflow "\$@"
LAUNCH
sed -i "2i export PYTHONPATH=\"$here:\$PYTHONPATH\"" "$HOME/.local/bin/lmflow"
chmod +x "$HOME/.local/bin/lmflow"
"$HOME/.local/bin/lmflow" install-services
echo
echo "Installed. Next:  lmflow setup    (asks for your password once)"
