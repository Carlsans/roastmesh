#!/usr/bin/env bash
# Installs roastmesh as a Linux systemd system service: an always-on `node
# serve --wan-discovery` that survives reboots and restarts itself after a
# crash (Restart=on-failure), plus a daily timer that self-updates it
# (`roastmesh update --yes`) and restarts only if the binary actually
# changed. Linux (systemd) only -- see ARCHITECTURE.md's "Peer discovery"
# section for why an always-on node matters: it's what net.serve()'s
# rendezvous-host fast-discovery path dials directly on every other node's
# startup, bypassing the public DHT lookup.
#
# Run as root, naming the (existing, unprivileged) user roastmesh should
# run as -- defaults to whoever invoked sudo:
#   sudo ./install-systemd-service.sh [username]
# or, fetched directly (mirrors install.sh's own curl|bash pattern):
#   curl -fsSL https://raw.githubusercontent.com/Carlsans/roastmesh/master/packaging/install-systemd-service.sh \
#     | sudo bash -s -- <username>
#
# Safe to re-run: re-running upgrades the unit files and re-enables the
# service in place.
#
# ROASTMESH_SKIP_BINARY_INSTALL=1 skips the install.sh step below entirely,
# leaving whatever is already at ~/.local/bin/roastmesh(-gui) alone -- for a
# machine already running a locally-built binary (e.g. testing an unreleased
# change), the normal path would silently overwrite it with the latest
# GitHub release, which is the opposite of what you want in that situation.
set -euo pipefail

REPO="Carlsans/roastmesh"

if [ "$(uname -s)" != "Linux" ]; then
    echo "This installer is for Linux (systemd) only." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root (sudo) -- it installs a system-wide systemd service" >&2
    echo "under /etc/systemd/system. The roastmesh binary itself still installs" >&2
    echo "into the target user's own ~/.local/bin, not root's." >&2
    exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-}}"
if [ -z "$TARGET_USER" ]; then
    echo "Usage: sudo $0 <username>" >&2
    echo "(the existing, unprivileged user roastmesh should run as)" >&2
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [ -z "$TARGET_HOME" ]; then
    echo "No such user: $TARGET_USER" >&2
    exit 1
fi

if [ -n "${ROASTMESH_SKIP_BINARY_INSTALL:-}" ]; then
    echo "ROASTMESH_SKIP_BINARY_INSTALL set -- leaving the existing binary at"
    echo "$TARGET_HOME/.local/bin/roastmesh alone."
else
    echo "Installing/updating the roastmesh binary for user '$TARGET_USER'..."
    sudo -u "$TARGET_USER" bash -c "curl -fsSL https://raw.githubusercontent.com/$REPO/master/install.sh | bash"
fi

# Prefer the unit files sitting next to this script (a local checkout);
# fall back to fetching them, since this script is also meant to be run
# directly via curl|bash with nothing else checked out.
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/systemd"
if [ ! -f "$UNIT_SRC/roastmesh-node@.service" ]; then
    UNIT_SRC="$(mktemp -d)"
    for f in roastmesh-node@.service roastmesh-update@.service roastmesh-update@.timer; do
        curl -fsSL "https://raw.githubusercontent.com/$REPO/master/packaging/systemd/$f" -o "$UNIT_SRC/$f"
    done
fi

cp "$UNIT_SRC/roastmesh-node@.service" "$UNIT_SRC/roastmesh-update@.service" "$UNIT_SRC/roastmesh-update@.timer" \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "roastmesh-node@$TARGET_USER.service"
systemctl enable --now "roastmesh-update@$TARGET_USER.timer"

echo
echo "Installed and started. Useful commands:"
echo "  systemctl status roastmesh-node@$TARGET_USER.service"
echo "  systemctl status roastmesh-update@$TARGET_USER.timer"
echo "  journalctl -u roastmesh-node@$TARGET_USER.service -f"
echo
echo "This host's port/discovery flags live in the unit's ExecStart= line --"
echo "edit with 'systemctl edit --full roastmesh-node@$TARGET_USER.service' if" \
     "this network needs --wan-port/--public-port set explicitly."
