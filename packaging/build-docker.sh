#!/usr/bin/env bash
# Build portable roastmesh/roastmesh-gui binaries inside Ubuntu 22.04 (see
# Dockerfile.build for why that base specifically). This is what actually
# produces the binaries meant to be distributed/attached to a release --
# packaging/build.sh (no Docker) is fine for quick local iteration on
# whatever machine you're on, but its output is only guaranteed to run on
# systems at least as new as the machine that built it.
#
# Usage:
#   packaging/build-docker.sh              # host architecture -> dist/
#   packaging/build-docker.sh aarch64      # ARM64 -> dist-aarch64/
#
# aarch64 covers 64-bit Raspberry Pi OS (Pi 4/5) and ARM servers. Built here
# on an x86_64 machine through qemu emulation, which works but is slow --
# expect roughly 10x a native build, most of it in PyInstaller's analysis
# pass. Needs binfmt handlers registered on the host; check with
#   ls /proc/sys/fs/binfmt_misc | grep qemu-aarch64
# and register them if missing with
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
#
# iroh ships a manylinux_2_28_aarch64 wheel, so nothing has to be compiled
# from source inside the emulated container -- which is what makes this
# merely slow rather than impractical.
set -euo pipefail
cd "$(dirname "$0")/.."

ARCH="${1:-native}"
case "$ARCH" in
    native)
        PLATFORM_ARGS=()
        IMAGE="roastmesh-builder"
        DIST_DIR="dist"
        SCRATCH="build-docker-scratch"
        ;;
    aarch64|arm64)
        PLATFORM_ARGS=(--platform linux/arm64)
        IMAGE="roastmesh-builder-arm64"
        DIST_DIR="dist-aarch64"
        SCRATCH="build-arm64-scratch"
        if ! ls /proc/sys/fs/binfmt_misc 2>/dev/null | grep -q qemu-aarch64; then
            echo "No qemu-aarch64 binfmt handler registered -- an ARM64 build cannot run here." >&2
            echo "Register one with: docker run --privileged --rm tonistiigi/binfmt --install arm64" >&2
            exit 1
        fi
        ;;
    *)
        echo "Unknown architecture '$ARCH' -- use 'native' or 'aarch64'." >&2
        exit 1
        ;;
esac

# Docker gives a container the *host's* resolv.conf, which is wrong whenever
# the host resolves through a VPN-local address: a Tailscale machine has
# nameserver 100.100.100.100, the container's bridge network cannot route to
# the tailscale0 interface, and so every apt/pip call inside the build dies
# with "Temporary failure in name resolution" while the host itself resolves
# perfectly. Nothing about the build is broken and nothing in the Dockerfile
# is wrong, which makes it a genuinely confusing failure to land on.
#
#   ROASTMESH_DOCKER_NETWORK=host packaging/build-docker.sh
#
# is the fix; left opt-in because host networking is otherwise worth avoiding.
NETWORK_ARGS=()
if [ -n "${ROASTMESH_DOCKER_NETWORK:-}" ]; then
    NETWORK_ARGS=(--network "$ROASTMESH_DOCKER_NETWORK")
fi

docker build "${PLATFORM_ARGS[@]}" "${NETWORK_ARGS[@]}" \
    -f packaging/Dockerfile.build -t "$IMAGE" .

mkdir -p "$DIST_DIR"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

docker run --rm "${PLATFORM_ARGS[@]}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -v "$(pwd)/$DIST_DIR:/app/dist" \
    -v "$(pwd)/$SCRATCH:/app/build" \
    "$IMAGE"

rm -rf "$SCRATCH"

echo
echo "built (Ubuntu 22.04 base, portable to newer systems):"
ls -lh "$DIST_DIR/roastmesh" "$DIST_DIR/roastmesh-gui"
file "$DIST_DIR/roastmesh" | sed 's/^/  /'
