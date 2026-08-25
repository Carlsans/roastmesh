#!/usr/bin/env bash
# roastnet installer -- Linux, x86_64.
#
# Run it with:
#   curl -fsSL https://raw.githubusercontent.com/Carlsans/roastnet/master/install.sh | bash
#
# Downloads the prebuilt roastnet/roastnet-gui binaries from the latest
# GitHub release, installs them to ~/.local/bin (no sudo, no system
# packages touched), and adds a roastnet entry to your applications menu
# so it's a normal double-clickable app afterward. Safe to re-run --
# re-running upgrades in place.
set -euo pipefail

REPO="Carlsans/roastnet"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

echo "roastnet installer"
echo

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Linux. For macOS/Windows, see:" >&2
    echo "  https://github.com/$REPO#install" >&2
    exit 1
fi

ARCH="$(uname -m)"
if [ "$ARCH" != "x86_64" ]; then
    echo "No prebuilt binary for '$ARCH' yet (only x86_64 today)." >&2
    echo "You can still build from source -- see https://github.com/$REPO#install" >&2
    exit 1
fi

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q "$1" -O "$2"; }
else
    echo "Need curl or wget to download roastnet -- please install one and re-run this script." >&2
    exit 1
fi

mkdir -p "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"

echo "Downloading roastnet..."
fetch "https://github.com/$REPO/releases/latest/download/roastnet" "$BIN_DIR/roastnet.new"
echo "Downloading roastnet-gui..."
fetch "https://github.com/$REPO/releases/latest/download/roastnet-gui" "$BIN_DIR/roastnet-gui.new"

chmod +x "$BIN_DIR/roastnet.new" "$BIN_DIR/roastnet-gui.new"
# only replace the live binaries once both downloads have fully succeeded
mv "$BIN_DIR/roastnet.new" "$BIN_DIR/roastnet"
mv "$BIN_DIR/roastnet-gui.new" "$BIN_DIR/roastnet-gui"

cat > "$ICON_DIR/roastnet.svg" <<'SVGEOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="30" fill="#f7f6f3"/>
  <path fill="#7a4a2b" d="M32 10c-13 0-22 9.5-22 21 0 6.6 3.4 12 9 15.8-1.6-3.4-1.4-7 .6-9.8 2.6-3.6 2-7 0-10.2-1.6-2.6-1.6-5.6.6-8 2.4-2.6 6-2.6 8.4-.2 2.4 2.4 2.4 5.6.2 8.2-2 2.4-2.6 5.6-1 8.6 1.4 2.6 4 4 6.8 3.8 6-3.8 9.4-9.6 9.4-16.2 0-11.5-9-21-22-21z"/>
  <path fill="#f7f6f3" d="M27 24c1.4 1.8 1.4 4-.2 6.4-2.4 3.6-2.6 7.4-.4 10.6 1.2 1.8 3 2.8 5 3-1.6-2-1.8-4.8-.2-7.4 1.8-3 2-6-.2-9-1-1.4-2.4-2.6-4-3.6z" opacity=".55"/>
</svg>
SVGEOF

cat > "$APPS_DIR/roastnet.desktop" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=roastnet
Comment=Peer-to-peer directory of Artisan roast profiles
Exec=$BIN_DIR/roastnet-gui
Icon=$ICON_DIR/roastnet.svg
Terminal=false
Categories=Utility;
DESKTOPEOF
chmod +x "$APPS_DIR/roastnet.desktop"

# best-effort refresh so the icon/menu entry shows up immediately in
# desktop environments that cache this -- harmless if the tool isn't present
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

echo
echo "Installed. roastnet should now appear in your applications menu."
echo "  GUI:  click it there, or run: $BIN_DIR/roastnet-gui"
echo "  CLI:  $BIN_DIR/roastnet --help"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo
        echo "Note: $BIN_DIR isn't on your PATH, so plain 'roastnet' won't work by name"
        echo "in a terminal yet (the applications-menu icon above works regardless of"
        echo "this). To fix that, add this line to your shell's startup file"
        echo "(e.g. ~/.bashrc or ~/.zshrc) and open a new terminal:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac
