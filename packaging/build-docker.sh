#!/usr/bin/env bash
# Build portable roastnet/roastnet-gui binaries inside Ubuntu 22.04 (see
# Dockerfile.build for why that base specifically). This is what actually
# produces the binaries meant to be distributed/attached to a release --
# packaging/build.sh (no Docker) is fine for quick local iteration on
# whatever machine you're on, but its output is only guaranteed to run on
# systems at least as new as the machine that built it.
set -euo pipefail
cd "$(dirname "$0")/.."

docker build -f packaging/Dockerfile.build -t roastnet-builder .

mkdir -p dist
rm -rf build-docker-scratch
mkdir -p build-docker-scratch

docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -v "$(pwd)/dist:/app/dist" \
    -v "$(pwd)/build-docker-scratch:/app/build" \
    roastnet-builder

rm -rf build-docker-scratch

echo
echo "built (Ubuntu 22.04 base, portable to newer systems):"
ls -lh dist/roastnet dist/roastnet-gui
