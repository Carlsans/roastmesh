#!/usr/bin/env bash
# roastmesh installer -- Linux, x86_64 and aarch64.
#
# Run it with:
#   curl -fsSL https://raw.githubusercontent.com/Carlsans/roastmesh/master/install.sh | bash
#
# To also pick an interface language up front (instead of relying on your
# OS locale, or picking one later in Settings), pass --lang. Because this
# script arrives over a pipe (`curl | bash`), the script itself occupies
# bash's stdin -- there's no plain argv slot, so arguments need `-s --`:
#   curl -fsSL https://raw.githubusercontent.com/Carlsans/roastmesh/master/install.sh | bash -s -- --lang fr
# An env var works too, and needs no `-s --`:
#   curl -fsSL https://raw.githubusercontent.com/Carlsans/roastmesh/master/install.sh | ROASTMESH_LANG=fr bash
#
# Downloads the prebuilt roastmesh/roastmesh-gui binaries from the latest
# GitHub release, installs them to ~/.local/bin (no sudo, no system
# packages touched), and adds a roastmesh entry to your applications menu
# so it's a normal double-clickable app afterward. Safe to re-run --
# re-running upgrades in place.
set -euo pipefail

REPO="Carlsans/roastmesh"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
CONFIG_FILE="$HOME/.local/share/roastmesh/gui_config.json"

# Keep this in sync with roastmesh.gui.i18n.LANGUAGES.
SUPPORTED_LANGS="en fr"

LANG_CHOICE="${ROASTMESH_LANG:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        --lang)   LANG_CHOICE="${2:-}"; shift 2 ;;
        --lang=*) LANG_CHOICE="${1#*=}"; shift ;;
        *)        shift ;;
    esac
done

if [ -n "$LANG_CHOICE" ]; then
    case " $SUPPORTED_LANGS " in
        *" $LANG_CHOICE "*) ;;
        *)
            echo "Unknown language '$LANG_CHOICE' -- supported: $SUPPORTED_LANGS" >&2
            exit 1
            ;;
    esac
fi

echo "roastmesh installer"
echo

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Linux. For macOS/Windows, see:" >&2
    echo "  https://github.com/$REPO#install" >&2
    exit 1
fi

# Asset naming: x86_64 keeps the bare `roastmesh`/`roastmesh-gui` names it
# has always had, because every copy of this script already in circulation
# fetches exactly those. aarch64 (Raspberry Pi 4/5 on a 64-bit OS, ARM
# servers) gets a -aarch64 suffix instead, so both live in one release
# without renaming anything.
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)          ASSET_SUFFIX="" ;;
    aarch64|arm64)   ASSET_SUFFIX="-aarch64" ;;
    *)
        echo "No prebuilt binary for '$ARCH' yet (x86_64 and aarch64 today)." >&2
        if [ "$ARCH" = "armv7l" ] || [ "$ARCH" = "armv6l" ]; then
            echo "That looks like a 32-bit Raspberry Pi OS. A 64-bit OS on the same" >&2
            echo "hardware ('uname -m' reporting aarch64) is supported." >&2
        fi
        echo "You can still build from source -- see https://github.com/$REPO#install" >&2
        exit 1
        ;;
esac

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q "$1" -O "$2"; }
else
    echo "Need curl or wget to download roastmesh -- please install one and re-run this script." >&2
    exit 1
fi

mkdir -p "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"

echo "Downloading roastmesh ($ARCH)..."
fetch "https://github.com/$REPO/releases/latest/download/roastmesh$ASSET_SUFFIX" "$BIN_DIR/roastmesh.new"
echo "Downloading roastmesh-gui ($ARCH)..."
fetch "https://github.com/$REPO/releases/latest/download/roastmesh-gui$ASSET_SUFFIX" "$BIN_DIR/roastmesh-gui.new"

chmod +x "$BIN_DIR/roastmesh.new" "$BIN_DIR/roastmesh-gui.new"
# only replace the live binaries once both downloads have fully succeeded
mv "$BIN_DIR/roastmesh.new" "$BIN_DIR/roastmesh"
mv "$BIN_DIR/roastmesh-gui.new" "$BIN_DIR/roastmesh-gui"

# The project was called roastnet before v0.5.0. Remove the old program files
# so an upgrade doesn't leave two menu entries and a stale binary that still
# runs an older build. Only the *program* is removed -- the identity, feed and
# index under ~/.local/share/roastnet are deliberately left exactly where they
# are, and the app keeps reading them from there (see roastmesh/paths.py).
for stale in "$BIN_DIR/roastnet" "$BIN_DIR/roastnet-gui" \
             "$APPS_DIR/roastnet.desktop" "$ICON_DIR/roastnet.svg"; do
    [ -e "$stale" ] && rm -f "$stale" && echo "removed the old $(basename "$stale")"
done

cat > "$ICON_DIR/roastmesh.svg" <<'SVGEOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="30" fill="#f7f6f3"/>
  <path fill="#7a4a2b" d="M32 10c-13 0-22 9.5-22 21 0 6.6 3.4 12 9 15.8-1.6-3.4-1.4-7 .6-9.8 2.6-3.6 2-7 0-10.2-1.6-2.6-1.6-5.6.6-8 2.4-2.6 6-2.6 8.4-.2 2.4 2.4 2.4 5.6.2 8.2-2 2.4-2.6 5.6-1 8.6 1.4 2.6 4 4 6.8 3.8 6-3.8 9.4-9.6 9.4-16.2 0-11.5-9-21-22-21z"/>
  <path fill="#f7f6f3" d="M27 24c1.4 1.8 1.4 4-.2 6.4-2.4 3.6-2.6 7.4-.4 10.6 1.2 1.8 3 2.8 5 3-1.6-2-1.8-4.8-.2-7.4 1.8-3 2-6-.2-9-1-1.4-2.4-2.6-4-3.6z" opacity=".55"/>
</svg>
SVGEOF

cat > "$APPS_DIR/roastmesh.desktop" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=roastmesh
Comment=Peer-to-peer directory of Artisan roast profiles
Exec=$BIN_DIR/roastmesh-gui
Icon=$ICON_DIR/roastmesh.svg
Terminal=false
Categories=Utility;
DESKTOPEOF
chmod +x "$APPS_DIR/roastmesh.desktop"

# best-effort refresh so the icon/menu entry shows up immediately in
# desktop environments that cache this -- harmless if the tool isn't present
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

# Only seed a language when there's no config yet -- this script promises
# re-running upgrades in place, and gui_config.json also holds the user's
# chosen database path and watch folder, which a blind overwrite here would
# reset. A config with only "language" set loads fine either way: roastmesh
# defaults every other field independently. Already have a config and asked
# for a language anyway? Leave it alone and say so, rather than guess.
if [ -n "$LANG_CHOICE" ]; then
    if [ ! -e "$CONFIG_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        printf '{\n  "language": "%s"\n}\n' "$LANG_CHOICE" > "$CONFIG_FILE"
    else
        echo
        echo "Note: you already have a roastmesh configuration, so --lang was not applied."
        echo "Change the language in the app's Settings tab instead."
    fi
fi

echo
echo "Installed. roastmesh should now appear in your applications menu."
echo "  GUI:  click it there, or run: $BIN_DIR/roastmesh-gui"
echo "  CLI:  $BIN_DIR/roastmesh --help"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo
        echo "Note: $BIN_DIR isn't on your PATH, so plain 'roastmesh' won't work by name"
        echo "in a terminal yet (the applications-menu icon above works regardless of"
        echo "this). To fix that, add this line to your shell's startup file"
        echo "(e.g. ~/.bashrc or ~/.zshrc) and open a new terminal:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac
