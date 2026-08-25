#!/usr/bin/env bash
# Build the roastnet/roastnet-gui single binaries for THIS platform.
#
# PyInstaller does not cross-compile: a macOS or Windows build has to be run
# on that OS, with the `build` extra installed there too. This script is the
# same on every platform -- only the machine it runs on changes what comes
# out the other end.
set -euo pipefail
cd "$(dirname "$0")/.."

pyinstaller packaging/roastnet.spec --clean --noconfirm

echo
echo "built:"
ls -lh dist/roastnet dist/roastnet-gui 2>/dev/null || ls -lh dist/
