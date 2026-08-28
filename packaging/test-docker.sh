#!/usr/bin/env bash
# Smoke-test the built dist/roastmesh + dist/roastmesh-gui binaries across
# major Linux distributions, in Docker. Each container gets just enough
# installed to simulate a real desktop (X11 runtime libs + xvfb-run +
# curl) -- a bare server image lacks what any real desktop install
# already has -- then runs the CLI directly and launches the GUI headless
# under Xvfb, the same verification approach this project's own test
# suite already uses for the GUI (tests/test_gui.py).
set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -x dist/roastmesh ] || [ ! -x dist/roastmesh-gui ]; then
    echo "dist/roastmesh(-gui) not found -- run packaging/build-docker.sh first." >&2
    exit 1
fi

TEST_BODY='
set -e
echo "--- identity show ---"
/dist/roastmesh identity show
echo "--- gui launch under Xvfb ---"
xvfb-run -a /dist/roastmesh-gui &
GUI_PID=$!
sleep 3
if kill -0 "$GUI_PID" 2>/dev/null; then
    echo "GUI still running after 3s -- OK"
    kill "$GUI_PID"
else
    wait "$GUI_PID"
    echo "GUI exited early (see above for any error)"
    exit 1
fi
'

# name : image : package-manager install command (X11 runtime libs + Xvfb + curl)
DISTROS=(
    "ubuntu-22.04|ubuntu:22.04|apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libx11-6 libxft2 libxss1 curl >/dev/null"
    "ubuntu-24.04|ubuntu:24.04|apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libx11-6 libxft2 libxss1 curl >/dev/null"
    "debian-12|debian:12|apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libx11-6 libxft2 libxss1 curl >/dev/null"
    "fedora|fedora:latest|dnf install -y -q xorg-x11-server-Xvfb libX11 libXft libXScrnSaver curl >/dev/null"
    "arch|archlinux:latest|pacman -Sy --noconfirm --quiet xorg-server-xvfb libx11 libxft libxss curl >/dev/null 2>&1"
)

FAILED=()
for entry in "${DISTROS[@]}"; do
    IFS='|' read -r name image install_cmd <<< "$entry"
    echo
    echo "=================================================================="
    echo "  $name  ($image)"
    echo "=================================================================="
    if docker run --rm -v "$(pwd)/dist:/dist:ro" "$image" bash -c "
        $install_cmd
        $TEST_BODY
    "; then
        echo "PASS: $name"
    else
        echo "FAIL: $name"
        FAILED+=("$name")
    fi
done

echo
echo "=================================================================="
if [ ${#FAILED[@]} -eq 0 ]; then
    echo "All ${#DISTROS[@]} distros passed."
else
    echo "FAILED: ${FAILED[*]}"
    exit 1
fi
