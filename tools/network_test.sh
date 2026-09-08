#!/usr/bin/env bash
# Standing end-to-end network test procedure: this desktop <-> the Pi.
#
# Why this exists: an ad-hoc, improvised test session (2026-09-07, testing
# v0.6.22's WAN features) produced confusing results multiple times because
# of things this script exists specifically to make impossible:
#   - the Pi silently running an older version than what was being tested
#     (its own daily auto-update timer lags a fresh release/local change);
#   - a stray already-running instance (the user's real GUI, the Pi's real
#     systemd service) interfering with or being confused for a test node;
#   - LAN-discovery or public-DHT cross-contamination between real
#     production identities and test identities that happened to publish
#     the same fixture content.
# Every run of this script starts from a hard-verified clean, identical-code
# state on both ends and ends by restoring both machines to their normal
# running state -- there is no "assume it's still clean from last time".
#
# Usage:
#   tools/network_test.sh all                 # setup, every scenario, teardown
#   tools/network_test.sh setup               # phases 0-2 only (stop real
#                                              # services, sync+build+install
#                                              # identical code on both ends,
#                                              # fresh isolated identities)
#   tools/network_test.sh scenario <name>     # run one scenario against an
#                                              # already-`setup` environment
#                                              # (dht_baseline |
#                                              # fresh_install_discovery |
#                                              # rendezvous | node_doctor |
#                                              # feed_sync | supersede |
#                                              # cross_edit_staging |
#                                              # watchdog_sync)
#   tools/network_test.sh teardown            # phase 4: clean up + restore
#
# `fresh_install_discovery` is NOT part of `all` -- it runs
# FRESH_INSTALL_TRIALS repeated trials (several minutes) specifically to
# measure real-world discovery timing, and is meant to be run on demand
# (`scenario fresh_install_discovery`, after `setup`), not on every routine
# feature-verification pass.
#
# `setup` must be re-run (which re-syncs+rebuilds+reinstalls on the Pi and
# regenerates fresh identities on both ends) any time the code under test has
# changed, including between two `scenario` calls in the same sitting --
# never assume a previous `setup`'s build is still what you want to test.
#
# `all` is the one to reach for by default: it is what makes this
# "serialized to minimize mistakes" -- one command, fixed order, hard
# assertions between phases, no manual step to forget.
#
# Explicitly does NOT touch: real identities/feed/index/devices.json on
# either machine (everything here uses a throwaway HOME on both ends), and
# does NOT commit/push/tag/release anything.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PI_HOST="raspberrypi"
PI_REAL_SERVICE="roastmesh-node@carl.service"
PI_REAL_TIMER="roastmesh-update@carl.timer"
DESKTOP_GUI_BIN="/home/carl/.local/bin/roastmesh-gui"

# Fixed, clearly-non-default test port -- defense in depth even though
# phase 0 already asserts nothing real is running that could collide with it.
TEST_WAN_PORT=47500

DESKTOP_ROOT="/tmp/roastmesh-nettest"
DESKTOP_HOME="$DESKTOP_ROOT/home"
DESKTOP_LOGS="$DESKTOP_ROOT/logs"
DESKTOP_DB="$DESKTOP_ROOT/index.sqlite3"
DESKTOP_FEED="$DESKTOP_ROOT/feed"
# Must match paths.default_devices_dir()/default_device_sync_state_path()'s
# ACTUAL resolution under an isolated HOME, not an arbitrary path of our own
# choosing: `roastmesh device stage-edit` (used by scenario_cross_edit_staging)
# has no --devices-dir override and always writes through those functions, so
# a mismatch here would mean the direct push_staged_edits() Python call below
# looks in the wrong folder and silently finds nothing to deliver.
DESKTOP_DEVICES="$DESKTOP_HOME/RoastMeshDevices"
DESKTOP_DEVICE_STATE="$DESKTOP_HOME/.local/share/roastmesh/device_sync_state.json"

# Resolved once, used as a literal absolute path in every remote command
# below -- avoids any ambiguity about whether ~ or $HOME expands locally
# (when this script builds the ssh command string) or remotely (when the
# Pi's own shell runs it). One resolution, used consistently, is simpler
# than getting that right every time.
PI_HOME="$(ssh "$PI_HOST" 'echo $HOME')"
PI_ROOT="$PI_HOME/roastmesh-nettest"
PI_SRC="$PI_ROOT/src"
PI_TEST_HOME="$PI_ROOT/home"
PI_LOGS="$PI_ROOT/logs"
PI_DB="$PI_ROOT/index.sqlite3"
PI_FEED="$PI_ROOT/feed"
PI_DEVICES="$PI_ROOT/devices"
PI_DEVICE_STATE="$PI_ROOT/device_sync_state.json"
PI_BIN="$PI_HOME/.local/bin/roastmesh"

VENV_PY="$REPO_ROOT/.venv/bin/python"
VENV_ROASTMESH="$REPO_ROOT/.venv/bin/roastmesh"

# Writes to stderr, not stdout: several callers capture a function's stdout
# via $(...) as its actual return value (wait_for_pattern below is exactly
# this) -- log() must stay safe to call from inside those without corrupting
# the captured value. `all > file 2>&1` at the call site still merges both
# streams into one combined, correctly-ordered log file either way.
log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "REFUSING TO PROCEED: $*" >&2; exit 1; }

# Retries a plain ssh call up to 5 times, but ONLY on exit 255 -- ssh's own
# convention for "the connection itself failed" (DNS blip, transient
# Tailscale hiccup), as distinct from the REMOTE command's own exit code
# (which can legitimately be anything 0-254 and must not be retried, since
# retrying a failed remote command could re-run something non-idempotent).
# Live-confirmed 2026-09-07: ssh calls right at the start of teardown failed
# with exit 255 -- THREE IN A ROW in one run (2s apart) -- every single time
# across multiple full runs, always at the same spot: immediately after
# stop_pi_pid kills the watchdog scenario's test node, which was actively
# doing QUIC/UDP hole-punching and DHT traffic over the same Tailscale
# interface. A plain manual ssh right after such a failure succeeds in
# under a second, so this is a brief real network-stack disruption from
# killing a busy networking process, not a persistent problem -- but
# teardown is the one place in this script that must not abandon the real
# GUI/service in a stopped state over it, hence 5 attempts, not 3.
ssh_retry() {
    local attempt rc
    for attempt in 1 2 3 4 5; do
        ssh "$@" && return 0
        rc=$?
        if [ "$rc" -ne 255 ] || [ "$attempt" -eq 5 ]; then
            return "$rc"
        fi
        log "  ssh connection failed (exit 255), retrying (attempt $attempt/5)..."
        sleep 3
    done
}

# --------------------------------------------------------------------------
# Phase 0 -- stop everything real, on both ends, and hard-assert clean
# --------------------------------------------------------------------------
phase0_stop() {
    log "Phase 0: stopping real services on both ends"

    local desktop_pids
    desktop_pids="$(pgrep -f "$DESKTOP_GUI_BIN" || true)"
    if [ -n "$desktop_pids" ]; then
        log "stopping desktop's real GUI (pid(s): $desktop_pids)"
        kill $desktop_pids
        sleep 1
    fi

    log "stopping the Pi's real systemd service + update timer"
    ssh_retry "$PI_HOST" "sudo systemctl stop $PI_REAL_SERVICE $PI_REAL_TIMER"
    sleep 1

    # Both exclusions must strip lines matching "pgrep" itself, not just the
    # scanning script's name: confirmed live over Tailscale SSH (2026-09-07)
    # that the remote command wrapper (`tailscaled be-child ssh ...
    # --cmd=pgrep -af roastmesh`) and its child `/bin/bash -c pgrep -af
    # roastmesh` both embed the literal text "pgrep -af roastmesh" in their
    # OWN argv while the check is running -- so an unfiltered `pgrep -af
    # roastmesh` on the Pi side always finds itself and would die here on a
    # genuinely clean Pi, every single run.
    local desktop_left pi_left
    desktop_left="$(pgrep -af roastmesh 2>/dev/null | grep -v 'pgrep\|network_test.sh' || true)"
    pi_left="$(ssh "$PI_HOST" 'pgrep -af roastmesh || true' | grep -v pgrep || true)"

    [ -z "$desktop_left" ] || die "unexpected roastmesh process(es) still running on the desktop:
$desktop_left"
    [ -z "$pi_left" ] || die "unexpected roastmesh process(es) still running on the Pi:
$pi_left"

    log "Phase 0 OK: both ends confirmed clean"
}

# --------------------------------------------------------------------------
# Phase 1 -- sync the CURRENT source tree to the Pi and build+install fresh,
# every time. Never trust a previous build, a GitHub release, or the Pi's
# own auto-update timer to be what's actually under test right now.
# --------------------------------------------------------------------------
phase1_sync_and_build() {
    log "Phase 1: syncing current source to the Pi and building fresh there"

    local commit dirty=""
    commit="$(git rev-parse HEAD)"
    git diff --quiet || dirty=" (with uncommitted changes on top)"
    log "testing commit $commit$dirty"

    ssh "$PI_HOST" "rm -rf '$PI_SRC' && mkdir -p '$PI_SRC'"
    rsync -a --delete \
        --exclude=.git --exclude=dist --exclude=dist-aarch64 --exclude=.venv \
        --exclude='__pycache__' --exclude='*.sqlite3' \
        ./ "$PI_HOST:$PI_SRC/"

    log "building natively on the Pi (native aarch64 build, no qemu -- expect well under a minute)"
    ssh "$PI_HOST" "cd '$PI_SRC' && python3 -m venv .venv-build && \
        .venv-build/bin/pip install -q -e '.[build]' && \
        .venv-build/bin/pyinstaller packaging/roastmesh.spec --clean --noconfirm --log-level WARN"

    ssh "$PI_HOST" "cp '$PI_SRC/dist/roastmesh' '$PI_SRC/dist/roastmesh-gui' '$PI_HOME/.local/bin/' && \
        chmod +x '$PI_BIN' '$PI_HOME/.local/bin/roastmesh-gui'"

    local desktop_version pi_version
    desktop_version="$("$VENV_ROASTMESH" --version)"
    pi_version="$(ssh "$PI_HOST" "'$PI_BIN' --version")"
    log "desktop: $desktop_version"
    log "pi:      $pi_version"
    [ "$desktop_version" = "$pi_version" ] || die "version strings differ after a fresh build from the same tree -- investigate before testing anything else"

    log "Phase 1 OK: identical code confirmed on both ends, built from the exact commit under test"
}

# --------------------------------------------------------------------------
# Phase 2 -- fresh, empty, isolated identity+data on both ends. Never reuse
# a previous run's leftover state, even if teardown normally removes it --
# wipe first, unconditionally, so an interrupted previous run can't leak in.
# --------------------------------------------------------------------------
phase2_prepare_envs() {
    log "Phase 2: preparing fresh isolated test environments on both ends"

    rm -rf "$DESKTOP_ROOT"
    mkdir -p "$DESKTOP_HOME" "$DESKTOP_LOGS" "$DESKTOP_DEVICES"
    ssh "$PI_HOST" "rm -rf '$PI_TEST_HOME' '$PI_LOGS' '$PI_DEVICES' '$PI_DB' '$PI_FEED' '$PI_DEVICE_STATE' && \
        mkdir -p '$PI_TEST_HOME' '$PI_LOGS' '$PI_DEVICES'"

    local desktop_pubkey pi_pubkey
    desktop_pubkey="$(HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" identity show)"
    pi_pubkey="$(ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' identity show")"

    echo "$desktop_pubkey" > "$DESKTOP_LOGS/desktop_pubkey.txt"
    echo "$pi_pubkey" > "$DESKTOP_LOGS/pi_pubkey.txt"
    log "desktop test pubkey: $desktop_pubkey"
    log "pi test pubkey:      $pi_pubkey"

    local desktop_ts pi_ts
    desktop_ts="$(tailscale ip -4 2>/dev/null || hostname -I | awk '{print $1}')"
    pi_ts="$(ssh "$PI_HOST" "tailscale ip -4 2>/dev/null || hostname -I | awk '{print \$1}'")"
    echo "$desktop_ts" > "$DESKTOP_LOGS/desktop_tailscale_ip.txt"
    echo "$pi_ts" > "$DESKTOP_LOGS/pi_tailscale_ip.txt"
    log "desktop tailscale ip: $desktop_ts"
    log "pi tailscale ip:      $pi_ts"

    log "Phase 2 OK"
}

# --------------------------------------------------------------------------
# Small helpers shared by scenarios
# --------------------------------------------------------------------------
desktop_pubkey() { cat "$DESKTOP_LOGS/desktop_pubkey.txt"; }
pi_pubkey() { cat "$DESKTOP_LOGS/pi_pubkey.txt"; }
desktop_ip() { cat "$DESKTOP_LOGS/desktop_tailscale_ip.txt"; }
pi_ip() { cat "$DESKTOP_LOGS/pi_tailscale_ip.txt"; }

# Start the Pi's node in the background over ssh, remotely detached (setsid
# nohup) so it survives the ssh command returning; prints the remote PID so
# the caller can stop it precisely later. Args after the fixed ones are
# passed straight through to `node serve`.
#
# TWO separate hazards were live-confirmed 2026-09-07 while getting this one
# line right, both specific to Tailscale SSH (reproduced with a plain
# `sleep`, no roastmesh involved -- an OpenSSH server does not exhibit
# either):
#   1. `echo $!` captured through the SAME ssh invocation that backgrounds a
#      job never returns, even though the backgrounded job itself detaches
#      and runs fine -- fixed by writing the PID to a file on the Pi and
#      reading it back with a SEPARATE ssh call instead.
#   2. `cd DIR && setsid nohup LONG_RUNNING_CMD & echo $!` ALSO hangs even
#      with (1) fixed, because `cd DIR && cmd &` is a compound AND-list, and
#      bash backgrounds a compound list by forking an intermediary subshell
#      that runs "cd DIR" and then blocks in the FOREGROUND on `cmd` inside
#      itself (there is no `&` left to consume once you're already inside
#      that subshell) -- so the subshell's own lifetime becomes exactly as
#      long as the long-running server it's waiting on. Tailscale's SSH
#      server appears to wait for the whole descendant process tree of the
#      exec'd command to exit, not just for the channel's pipes to see EOF
#      (which is all real OpenSSH waits for) -- so that lingering subshell
#      hangs the channel even though the top-level command already finished.
#      Fixed by splitting `cd` into its own statement (`;`, not `&&`) so the
#      long-running command is backgrounded directly as a single simple
#      command from the top-level shell, with no wrapping subshell created.
start_pi_node() {
    local pidfile="$PI_LOGS/node.pid"
    timeout 20 ssh "$PI_HOST" "cd '$PI_ROOT' || exit 1; HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' \
        setsid nohup '$PI_BIN' --db '$PI_DB' node serve --no-lan-discovery $* \
        > '$PI_LOGS/node.log' 2>&1 < /dev/null & echo \$! > '$pidfile'" \
        || die "ssh to launch the Pi's test node did not return within 20s -- this exact symptom (an ssh call over Tailscale SSH that starts a background job and never returns) was live-confirmed 2026-09-07; if this fires, something regressed the fix in start_pi_node()'s comment"
    timeout 10 ssh "$PI_HOST" "cat '$pidfile'" || die "ssh to fetch the Pi test node's pidfile did not return within 10s"
}

stop_pi_pid() {
    local pid="$1"
    ssh "$PI_HOST" "kill '$pid' 2>/dev/null || true"
}

start_desktop_node() {
    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" setsid nohup "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" node serve --no-lan-discovery "$@" \
        > "$DESKTOP_LOGS/node.log" 2>&1 < /dev/null &
    echo $!
}

# Poll a (local or already-fetched-locally) log file for a pattern, up to a
# timeout; echoes the elapsed seconds (integer) on success, "TIMEOUT" on
# failure. start_epoch is passed in so callers control what "elapsed" means
# (e.g. "since both nodes were started", not "since this poll began").
wait_for_pattern() {
    local file="$1" pattern="$2" timeout_s="$3" start_epoch="$4"
    local waited=0
    log "  waiting up to ${timeout_s}s for '$pattern' in $file"
    while [ "$waited" -lt "$timeout_s" ]; do
        if grep -q -- "$pattern" "$file" 2>/dev/null; then
            echo "$(( $(date +%s) - start_epoch ))"
            return 0
        fi
        # Heartbeat every 15s so a long wait is visibly alive in the log
        # rather than looking indistinguishable from a hang -- confirmed
        # 2026-09-07 this distinction matters: a real 44-minute ssh hang and
        # a normal ~120s DHT wait both look identical (silence) without one.
        if [ "$((waited % 15))" -eq 0 ] && [ "$waited" -gt 0 ]; then
            log "  ... still waiting (${waited}s/${timeout_s}s elapsed, no match yet)"
        fi
        sleep 1
        waited=$(( waited + 1 ))
    done
    log "  TIMEOUT after ${timeout_s}s waiting for '$pattern'"
    echo "TIMEOUT"
    return 1
}

fetch_pi_log() {
    scp -q "$PI_HOST:$PI_LOGS/node.log" "$DESKTOP_LOGS/pi_node.log" 2>/dev/null || true
}

# Like wait_for_pattern, but for a file that lives on the Pi -- re-fetches
# it via scp EVERY iteration instead of checking one stale snapshot.
#
# This replaces a real bug found live 2026-09-07: scenario_rendezvous used
# to scp the Pi's log ONCE, then poll that single unchanging snapshot for 5
# more seconds -- so a discovery that happened moments after the snapshot
# was taken was invisible, and the scenario's own cleanup (stop_pi_pid) then
# killed the Pi's still-legitimately-running process (its own internal
# asyncio timeout is 30s) before it ever got a fair chance to either
# discover its peer or genuinely give up. This is what made the rendezvous
# mechanism look one-directional in every full run -- it wasn't the
# mechanism, it was the check.
wait_for_remote_pattern() {
    local remote_file="$1" local_file="$2" pattern="$3" timeout_s="$4" start_epoch="$5"
    local waited=0
    log "  waiting up to ${timeout_s}s for '$pattern' in $PI_HOST:$remote_file (re-fetched live, not a one-shot snapshot)"
    while [ "$waited" -lt "$timeout_s" ]; do
        scp -q "$PI_HOST:$remote_file" "$local_file" 2>/dev/null || true
        if grep -q -- "$pattern" "$local_file" 2>/dev/null; then
            echo "$(( $(date +%s) - start_epoch ))"
            return 0
        fi
        if [ "$((waited % 15))" -eq 0 ] && [ "$waited" -gt 0 ]; then
            log "  ... still waiting (${waited}s/${timeout_s}s elapsed, no match yet)"
        fi
        sleep 2
        waited=$(( waited + 2 ))
    done
    log "  TIMEOUT after ${timeout_s}s waiting for '$pattern' on the Pi"
    echo "TIMEOUT"
    return 1
}

# Poll for a `ticket: ...` line, extracting it once found. `read_cmd` is a
# full shell command STRING that prints the log's current content when
# eval'd (e.g. "cat '$DESKTOP_LOGS/node.log'" or "ssh host \"cat 'path'\"")
# -- this lets the same helper cover both a local file and a remote one over
# ssh. Echoes the ticket on success, empty string + nonzero exit on timeout.
#
# This replaces a fixed `sleep 2` before a single grep attempt, which was a
# live-confirmed race (2026-09-07): a node under load took over 60s to print
# its ticket in one real run, not the ~2s every call site assumed. Worse, a
# bare `grep '^ticket: ' file | head -1 | sed ...` that finds nothing exits
# non-zero, and this script's `set -euo pipefail` turns that into an
# IMMEDIATE, SILENT death of the entire script the moment it happens -- no
# die() message, nothing -- which is exactly what happened. Looping with the
# grep guarded by `|| true` means "not ready yet" is never mistaken for a
# fatal pipeline failure.
wait_for_ticket() {
    local read_cmd="$1" timeout_s="$2" waited=0 ticket=""
    while [ "$waited" -lt "$timeout_s" ]; do
        ticket="$(eval "$read_cmd" 2>/dev/null | grep '^ticket: ' | head -1 | sed 's/^ticket: //' || true)"
        if [ -n "$ticket" ]; then
            echo "$ticket"
            return 0
        fi
        if [ "$((waited % 10))" -eq 0 ] && [ "$waited" -gt 0 ]; then
            log "  ... still waiting for a ticket line (${waited}s/${timeout_s}s elapsed)"
        fi
        sleep 1
        waited=$(( waited + 1 ))
    done
    echo ""
    return 1
}

# --------------------------------------------------------------------------
# Scenario: dht_baseline -- cold start, real public DHT, no rendezvous
# seeding beyond what's baked into the binary (moduloinfo.ca, which does not
# currently answer -- see the rendezvous scenario for the mechanism that
# actually matters). This is deliberately "just start it normally": it's
# what a brand new user's cold start actually looks like today.
# --------------------------------------------------------------------------
scenario_dht_baseline() {
    log "Scenario dht_baseline: cold start, public DHT only, no test-side rendezvous seeding"
    local start_epoch pi_pid desktop_pid elapsed_desktop elapsed_pi
    start_epoch="$(date +%s)"

    pi_pid="$(start_pi_node --wan-discovery --wan-port "$TEST_WAN_PORT")"
    desktop_pid="$(start_desktop_node --wan-discovery --wan-port "$TEST_WAN_PORT")"
    log "pi test node pid=$pi_pid, desktop test node pid=$desktop_pid -- watching for mutual discovery, up to 120s"

    # Both directions get the SAME 120s budget, live-polled -- a one-shot
    # scp of the Pi's log followed by polling that unchanging snapshot for
    # only 5 more seconds (the original shape here) is the exact measurement
    # bug found and fixed in scenario_rendezvous: it can report a false
    # TIMEOUT for a direction that would have succeeded moments later.
    elapsed_desktop="$(wait_for_pattern "$DESKTOP_LOGS/node.log" "wan: discovered $(pi_pubkey | cut -c1-16)" 120 "$start_epoch" || true)"
    elapsed_pi="$(wait_for_remote_pattern "$PI_LOGS/node.log" "$DESKTOP_LOGS/pi_node.log" "wan: discovered $(desktop_pubkey | cut -c1-16)" 120 "$start_epoch" || true)"

    log "RESULT dht_baseline: desktop discovered pi after ${elapsed_desktop}s; pi discovered desktop after ${elapsed_pi}s"

    kill "$desktop_pid" 2>/dev/null || true
    stop_pi_pid "$pi_pid"
    sleep 1
}

# --------------------------------------------------------------------------
# Scenario: fresh_install_discovery -- repeatedly simulates a BRAND NEW Pi
# install (fresh identity, empty DHT node cache, no prior state -- exactly
# what install.sh produces) discovering an already-running, warmed-up
# desktop peer, via the REAL production path (`node serve --wan-discovery`,
# the same live BOOTSTRAP_NODES resolution net.serve() itself uses -- not
# an isolated mechanism test like the rendezvous scenario below). Repeated
# FRESH_INSTALL_TRIALS times so the result is a real efficiency distribution
# instead of one noisy sample -- added 2026-09-07 specifically to measure
# "how long does a new user actually wait" for the rendezvous+DHT combo as
# it exists today.
#
# The desktop is started ONCE and stays warm across every trial -- that's
# the realistic case ("my friend is already using roastmesh and I just
# installed it"), not two simultaneous cold starts (dht_baseline above
# already covers that). Only the Pi gets a genuinely fresh identity + empty
# state each trial, matching what a real fresh install looks like every
# single time, not just the first.
# --------------------------------------------------------------------------
FRESH_INSTALL_TRIALS=5
FRESH_INSTALL_TIMEOUT_S=90

scenario_fresh_install_discovery() {
    log "Scenario fresh_install_discovery: $FRESH_INSTALL_TRIALS fresh-Pi-install trials against an already-running desktop, real production path"

    local desktop_pid
    desktop_pid="$(start_desktop_node --wan-discovery --wan-port "$TEST_WAN_PORT")"
    log "desktop node warmed up (pid=$desktop_pid), giving it 5s before the first trial"
    sleep 5

    local trial results=() successes=0
    for trial in $(seq 1 "$FRESH_INSTALL_TRIALS"); do
        log "trial $trial/$FRESH_INSTALL_TRIALS: wiping the Pi's test identity/state -- simulating a fresh install"
        ssh_retry "$PI_HOST" "rm -rf '$PI_TEST_HOME' && mkdir -p '$PI_TEST_HOME'"

        local pi_pubkey_trial
        pi_pubkey_trial="$(ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' identity show")"

        local start_epoch pi_pid elapsed
        start_epoch="$(date +%s)"
        # The real production command -- no test-only flags beyond --db
        # (pointed inside the isolated test root so production data is
        # never touched) and --wan-port (a fixed, clearly-non-default port,
        # same reasoning as every other scenario here).
        pi_pid="$(start_pi_node --wan-discovery --wan-port "$TEST_WAN_PORT")"

        elapsed="$(wait_for_pattern "$DESKTOP_LOGS/node.log" "wan: discovered $(echo "$pi_pubkey_trial" | cut -c1-16)" "$FRESH_INSTALL_TIMEOUT_S" "$start_epoch" || true)"
        if [ "$elapsed" != "TIMEOUT" ]; then
            log "trial $trial: desktop discovered the fresh pi in ${elapsed}s"
        else
            log "trial $trial: TIMEOUT after ${FRESH_INSTALL_TIMEOUT_S}s -- fresh pi was NOT discovered"
        fi
        results+=("$elapsed")

        stop_pi_pid "$pi_pid"
        sleep 1
    done

    kill "$desktop_pid" 2>/dev/null || true
    sleep 1

    for r in "${results[@]}"; do
        [ "$r" = "TIMEOUT" ] || successes=$(( successes + 1 ))
    done
    log "RESULT fresh_install_discovery: $successes/$FRESH_INSTALL_TRIALS succeeded -- individual results: ${results[*]}"

    if [ "$successes" -gt 0 ]; then
        local sum=0 n=0 fastest="" slowest=""
        for r in "${results[@]}"; do
            [ "$r" = "TIMEOUT" ] && continue
            sum=$(( sum + r ))
            n=$(( n + 1 ))
            if [ -z "$fastest" ] || [ "$r" -lt "$fastest" ]; then fastest="$r"; fi
            if [ -z "$slowest" ] || [ "$r" -gt "$slowest" ]; then slowest="$r"; fi
        done
        log "RESULT fresh_install_discovery: avg=$(( sum / n ))s fastest=${fastest}s slowest=${slowest}s (over $n successful trial(s) of $FRESH_INSTALL_TRIALS)"
    else
        log "RESULT fresh_install_discovery: 0 successful trials -- no timing stats to report"
    fi
}

# --------------------------------------------------------------------------
# Scenario: rendezvous -- the actual new feature, tested at the mechanism
# level (run_wan_discovery called directly with an explicit rendezvous host)
# rather than through net.serve()'s fetch/cache resolution. This is
# deliberate, not a shortcut: net.serve() fetches BOOTSTRAP_NODES live from
# GitHub and correctly prefers a successful fetch over anything locally
# seeded, so a real internet-connected machine can never be made to use a
# fake/test rendezvous host through the normal startup path once that file
# is live on GitHub (confirmed 2026-09-07). Testing the mechanism directly
# is the only reliable way to measure it in isolation; `node_doctor` below
# is what verifies the real, live, GitHub-hosted list end to end.
# --------------------------------------------------------------------------
scenario_rendezvous() {
    log "Scenario rendezvous: direct run_wan_discovery call, each side pointed at the other's real address"
    local d_ip p_ip start_epoch
    d_ip="$(desktop_ip)"; p_ip="$(pi_ip)"
    start_epoch="$(date +%s)"

    local py_snippet
    py_snippet=$(cat <<'PYEOF'
import asyncio, sys, time
from roastmesh.identity import load_or_create_identity
from roastmesh.wan_discovery import run_wan_discovery

peer_host, peer_port, own_port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

async def main():
    ident, _ = load_or_create_identity()
    found = asyncio.Event()
    t0 = time.monotonic()

    async def on_discovered(pubkey, ticket):
        print(f"DISCOVERED {pubkey} after {time.monotonic() - t0:.2f}s", flush=True)
        found.set()

    task = asyncio.create_task(run_wan_discovery(
        ident.public_key_hex, "network-test-placeholder-ticket", on_discovered,
        port=own_port, rendezvous_hosts=[(peer_host, None, peer_port)],
        bootstrap_nodes=[], lookup_interval_s=999.0,
    ))
    try:
        await asyncio.wait_for(found.wait(), timeout=30)
    except asyncio.TimeoutError:
        print("TIMEOUT", flush=True)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

asyncio.run(main())
PYEOF
)
    echo "$py_snippet" > "$DESKTOP_LOGS/rendezvous_test.py"
    scp -q "$DESKTOP_LOGS/rendezvous_test.py" "$PI_HOST:$PI_LOGS/rendezvous_test.py"

    # Desktop launched FIRST (instant, local), Pi launched SECOND (needs an
    # ssh round-trip) -- live-confirmed 2026-09-07 the reverse order (Pi
    # first, via 2 sequential ssh calls, desktop started only after both
    # returned) produced a rock-solid one-directional result every time: the
    # Pi discovered the desktop in <1s, the desktop never discovered the Pi
    # within 35s. HELLO_RETRIES is only an 8-second burst with no further
    # retry in this test (lookup_interval_s is set to 999 below specifically
    # to isolate that burst from production's real 120s periodic re-hello),
    # so a multi-second launch stagger from sequential ssh round-trips can
    # plausibly exhaust the whole burst on one side before the other side's
    # socket even exists. Launching the side with no network latency first
    # removes that confound instead of just asserting it isn't there.
    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" setsid nohup "$VENV_PY" \
        "$DESKTOP_LOGS/rendezvous_test.py" "$p_ip" "$TEST_WAN_PORT" "$TEST_WAN_PORT" \
        > "$DESKTOP_LOGS/rendezvous.log" 2>&1 < /dev/null &
    local desktop_pid=$!

    # PID written to a file and fetched separately -- see start_pi_node's
    # comment for why `echo $!` through the same ssh call that backgrounds
    # a job hangs the channel indefinitely over Tailscale SSH.
    timeout 20 ssh "$PI_HOST" "cd '$PI_SRC' || exit 1; HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' setsid nohup \
        .venv-build/bin/python '$PI_LOGS/rendezvous_test.py' '$d_ip' '$TEST_WAN_PORT' '$TEST_WAN_PORT' \
        > '$PI_LOGS/rendezvous.log' 2>&1 < /dev/null & echo \$! > '$PI_LOGS/rendezvous.pid'" \
        || die "ssh to launch the Pi's rendezvous test script did not return within 20s -- see start_pi_node's comment"
    timeout 10 ssh "$PI_HOST" "cat '$PI_LOGS/rendezvous.pid'" > "$DESKTOP_LOGS/pi_rendezvous_pid.txt" \
        || die "ssh to fetch the Pi rendezvous test's pidfile did not return within 10s"

    # Both sides get the SAME 35s budget, and the Pi's side is polled live
    # (re-fetched, not a one-shot snapshot) -- see wait_for_remote_pattern's
    # comment for why the old 2s-sleep-then-one-scp-then-poll-a-stale-file
    # approach made the mechanism look one-directional when it wasn't.
    local elapsed_desktop elapsed_pi
    elapsed_desktop="$(wait_for_pattern "$DESKTOP_LOGS/rendezvous.log" "DISCOVERED" 35 "$start_epoch" || true)"
    elapsed_pi="$(wait_for_remote_pattern "$PI_LOGS/rendezvous.log" "$DESKTOP_LOGS/pi_rendezvous.log" "DISCOVERED" 35 "$start_epoch" || true)"

    # grep for the actual outcome line rather than `tail -1` -- the log also
    # contains an unrelated "wan-stats: {...}" line from run_wan_discovery's
    # own startup round announcement (unconditional, independent of the
    # rendezvous mechanism this scenario is isolating), which `tail -1` can
    # pick up instead of the real DISCOVERED/TIMEOUT outcome if the process
    # printed nothing else yet.
    local desktop_result pi_result
    desktop_result="$(grep -m1 'DISCOVERED\|TIMEOUT' "$DESKTOP_LOGS/rendezvous.log" 2>/dev/null || echo 'no DISCOVERED/TIMEOUT line yet')"
    pi_result="$(grep -m1 'DISCOVERED\|TIMEOUT' "$DESKTOP_LOGS/pi_rendezvous.log" 2>/dev/null || echo 'no DISCOVERED/TIMEOUT line yet')"

    log "RESULT rendezvous: desktop side: $desktop_result (wall ${elapsed_desktop}s)"
    log "RESULT rendezvous: pi side:      $pi_result (wall ${elapsed_pi}s)"

    kill "$desktop_pid" 2>/dev/null || true
    stop_pi_pid "$(cat "$DESKTOP_LOGS/pi_rendezvous_pid.txt")"
    sleep 1
}

# --------------------------------------------------------------------------
# Scenario: node_doctor -- real, live diagnostic on both ends (moduloinfo.ca
# is expected to show ok:false until a node is actually deployed to answer
# there; this scenario is what proves that honestly rather than assuming).
# --------------------------------------------------------------------------
scenario_node_doctor() {
    log "Scenario node_doctor: real network diagnostic on both ends"
    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" node doctor --json \
        > "$DESKTOP_LOGS/desktop_doctor.json"
    ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' node doctor --json" \
        > "$DESKTOP_LOGS/pi_doctor.json"
    log "RESULT node_doctor: desktop -> $DESKTOP_LOGS/desktop_doctor.json"
    log "RESULT node_doctor: pi      -> $DESKTOP_LOGS/pi_doctor.json"
    "$VENV_PY" -c "
import json
for who, path in [('desktop', '$DESKTOP_LOGS/desktop_doctor.json'), ('pi', '$DESKTOP_LOGS/pi_doctor.json')]:
    d = json.load(open(path))
    print(f\"{who}: external_ip={d.get('external_ip')} nat={d.get('nat')} \"
          f\"warm={d.get('warm')} bootstrap_ok={sum(1 for r in d.get('bootstrap', []) if r.get('ok'))}/{len(d.get('bootstrap', []))} \"
          f\"rendezvous={d.get('rendezvous')}\")
"
}

# --------------------------------------------------------------------------
# Scenario: feed_sync -- publish on the desktop, manually dial-sync from the
# Pi (ticket-based, not auto-discovery -- see the module docstring above for
# why auto-discovery of an arbitrary test peer can't be relied on once
# BOOTSTRAP_NODES is live). Measures the sync operation itself, not discovery.
# --------------------------------------------------------------------------
scenario_feed_sync() {
    log "Scenario feed_sync: publish on desktop, dial-sync from the Pi"
    local desktop_pid start_epoch elapsed ticket
    mkdir -p "$DESKTOP_FEED"

    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" setsid nohup "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" node serve --no-lan-discovery --no-device-sync --feed-dir "$DESKTOP_FEED" \
        > "$DESKTOP_LOGS/node.log" 2>&1 < /dev/null &
    desktop_pid=$!
    ticket="$(wait_for_ticket "cat '$DESKTOP_LOGS/node.log'" 30)"
    [ -n "$ticket" ] || die "desktop test node never printed a ticket within 30s -- check $DESKTOP_LOGS/node.log"

    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" feed --feed-dir "$DESKTOP_FEED" publish tests/fixtures/philstyle_1.alog

    start_epoch="$(date +%s)"
    ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' --db '$PI_DB' peer sync '$ticket'"
    elapsed=$(( $(date +%s) - start_epoch ))

    local pi_search
    pi_search="$(ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' --db '$PI_DB' search --json")"
    echo "$pi_search" > "$DESKTOP_LOGS/pi_search_after_feed_sync.json"
    echo "$pi_search" | "$VENV_PY" -c "import json,sys; rows=json.load(sys.stdin); print('found on pi:', len(rows), 'row(s)')"

    log "RESULT feed_sync: sync took ${elapsed}s"
    kill "$desktop_pid" 2>/dev/null || true
    sleep 1
}

# --------------------------------------------------------------------------
# Scenario: supersede -- edit notes on the just-published (now on both ends)
# roast; the desktop side publishes a superseding entry; re-sync to the Pi
# and confirm it shows the edit as current and the original under
# --show-superseded. Requires scenario_feed_sync to have run first in this
# same `setup` (uses the same desktop feed/db).
# --------------------------------------------------------------------------
scenario_supersede() {
    log "Scenario supersede: notes edit on an already-published roast, re-sync, verify on the Pi"
    local roast_id desktop_pid start_epoch elapsed ticket

    roast_id="$(HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" search --json | "$VENV_PY" -c "import json,sys; print(json.load(sys.stdin)[0]['roast_id'])")"

    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" notes edit "$roast_id" --feed-dir "$DESKTOP_FEED" \
        --roasting-notes "network_test.sh supersede scenario $(date -Iseconds)"

    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" feed --feed-dir "$DESKTOP_FEED" verify \
        || die "feed verify failed on the desktop's own feed after superseding -- do not treat this as a passing test"

    setsid nohup env HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" node serve --no-lan-discovery --no-device-sync --feed-dir "$DESKTOP_FEED" \
        > "$DESKTOP_LOGS/node.log" 2>&1 < /dev/null &
    desktop_pid=$!
    ticket="$(wait_for_ticket "cat '$DESKTOP_LOGS/node.log'" 30)"
    [ -n "$ticket" ] || die "desktop test node never printed a ticket within 30s for the supersede re-sync"

    start_epoch="$(date +%s)"
    ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' --db '$PI_DB' peer sync '$ticket'"
    elapsed=$(( $(date +%s) - start_epoch ))

    local default_count superseded_count
    default_count="$(ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' --db '$PI_DB' search --json" \
        | "$VENV_PY" -c "import json,sys; print(len(json.load(sys.stdin)))")"
    superseded_count="$(ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' --db '$PI_DB' search --show-superseded --json" \
        | "$VENV_PY" -c "import json,sys; print(len(json.load(sys.stdin)))")"

    log "RESULT supersede: re-sync took ${elapsed}s; pi default search: $default_count row(s); with --show-superseded: $superseded_count row(s)"
    [ "$default_count" = "1" ] && [ "$superseded_count" = "2" ] \
        && log "RESULT supersede: PASS (exactly the superseding entry shown by default, both visible with --show-superseded)" \
        || log "RESULT supersede: UNEXPECTED COUNTS -- investigate before trusting this as a pass"

    kill "$desktop_pid" 2>/dev/null || true
    sleep 1
}

# --------------------------------------------------------------------------
# Scenario: cross_edit_staging -- inject mutual device-sync trust directly
# (SAS pairing needs real LAN-broadcast reachability, which the desktop and
# the Tailscale-reached Pi do not have to each other -- this scenario tests
# the sync mechanism, not the pairing ceremony), stage an edit on the
# desktop for a "Pi-owned" roast, deliver it via a direct push_staged_edits
# call (same reasoning as the rendezvous scenario: auto-discovery populating
# known_tickets can't be relied on here), confirm it lands on the Pi.
# --------------------------------------------------------------------------
scenario_cross_edit_staging() {
    log "Scenario cross_edit_staging: inject trust, stage an edit, deliver directly, verify on the Pi"
    local roast_id pi_pk desktop_pk pi_pid ticket start_epoch elapsed

    pi_pk="$(pi_pubkey)"
    desktop_pk="$(desktop_pubkey)"

    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_PY" -c "
from roastmesh.devices import Device, add_device
import datetime
add_device(Device(pubkey='$pi_pk', name='pi-test', platform='linux', paired_at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
"
    ssh "$PI_HOST" "HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' '$PI_BIN' identity show >/dev/null"  # ensure identity exists

    # Written to a local file and scp'd over rather than an inline `python -c`
    # through ssh's own quoting -- a multi-line snippet double-nested inside
    # an ssh command string is exactly the kind of thing that's easy to get
    # subtly wrong; a real file removes the ambiguity entirely.
    cat > "$DESKTOP_LOGS/pi_add_device.py" <<PYEOF
from roastmesh.devices import Device, add_device
import datetime
add_device(Device(pubkey="$desktop_pk", name="desktop-test", platform="linux",
                   paired_at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
PYEOF
    scp -q "$DESKTOP_LOGS/pi_add_device.py" "$PI_HOST:$PI_LOGS/add_device.py"
    ssh "$PI_HOST" "cd '$PI_SRC' && HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' .venv-build/bin/python '$PI_LOGS/add_device.py'"

    # ingest a fixture on the desktop, then attribute it to the Pi's test pubkey
    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" ingest tests/fixtures/hottop_1.alog
    # Filter on raw_path, not "the last search result": $DESKTOP_DB accumulates
    # roasts from every scenario run so far in this `setup` (feed_sync,
    # supersede) -- "take the last row" would silently grab the wrong one.
    roast_id="$(HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" search --json \
        | "$VENV_PY" -c "import json,sys; rows=json.load(sys.stdin); print(next(r['roast_id'] for r in rows if r['raw_path'].endswith('hottop_1.alog')))")"
    "$VENV_PY" -c "
import sqlite3
conn = sqlite3.connect('$DESKTOP_DB')
conn.execute(\"UPDATE sources SET author_pubkey = ? WHERE source_id = (SELECT source_id FROM roasts WHERE roast_id = ?)\", ('$pi_pk', '$roast_id'))
conn.commit()
"

    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_ROASTMESH" \
        --db "$DESKTOP_DB" device stage-edit "$roast_id" --roasting-notes "staged from desktop $(date -Iseconds)"

    # Bring up a Pi node (device-sync only, no discovery) to get a real dialable
    # ticket. PID written to a file and fetched separately -- see
    # start_pi_node's comment for why `echo $!` through the same ssh call
    # that backgrounds a job hangs the channel indefinitely over Tailscale SSH.
    timeout 20 ssh "$PI_HOST" "cd '$PI_ROOT' || exit 1; HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' setsid nohup \
        '$PI_BIN' --db '$PI_DB' node serve --no-lan-discovery --devices-dir '$PI_DEVICES' \
        > '$PI_LOGS/node.log' 2>&1 < /dev/null & echo \$! > '$PI_LOGS/node.pid'" \
        || die "ssh to launch the Pi's cross-edit-staging test node did not return within 20s -- see start_pi_node's comment"
    pi_pid="$(timeout 10 ssh "$PI_HOST" "cat '$PI_LOGS/node.pid'")" \
        || die "ssh to fetch the Pi test node's pidfile did not return within 10s"
    ticket="$(wait_for_ticket "ssh $PI_HOST \"cat '$PI_LOGS/node.log'\"" 30)"
    [ -n "$ticket" ] || die "pi test node never printed a ticket within 30s for cross-edit-staging delivery"

    start_epoch="$(date +%s)"
    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_PY" -c "
import asyncio
from pathlib import Path
from roastmesh import device_sync
from roastmesh.identity import load_or_create_identity

async def main():
    ident, _ = load_or_create_identity()
    delivered = await device_sync.push_staged_edits(
        Path('$DESKTOP_DEVICES'), Path('$DESKTOP_DEVICE_STATE'), ident, {'$pi_pk': '$ticket'},
    )
    print('delivered:', delivered)

asyncio.run(main())
"
    elapsed=$(( $(date +%s) - start_epoch ))

    local remote_file="edited/$roast_id.alog"
    if ssh "$PI_HOST" "test -f '$PI_DEVICES/$remote_file'"; then
        log "RESULT cross_edit_staging: PASS -- delivered in ${elapsed}s, found at $PI_DEVICES/$remote_file"
    else
        log "RESULT cross_edit_staging: FAIL -- expected file not found on the Pi after ${elapsed}s"
    fi
    if [ -d "$DESKTOP_DEVICES/.staging" ] && [ -n "$(find "$DESKTOP_DEVICES/.staging" -type f 2>/dev/null)" ]; then
        log "RESULT cross_edit_staging: staging copy did NOT clear (unexpected)"
    else
        log "RESULT cross_edit_staging: staging copy correctly cleared"
    fi

    stop_pi_pid "$pi_pid"
    sleep 1
}

# --------------------------------------------------------------------------
# Scenario: watchdog_sync -- full-mirror reconcile of a plain file, timed.
# Uses the same trust/devices-dir state cross_edit_staging left behind, so
# run that scenario first in the same `setup` if running scenarios
# individually rather than via `all`.
# --------------------------------------------------------------------------
scenario_watchdog_sync() {
    log "Scenario watchdog_sync: drop a file on the desktop, reconcile, time delivery to the Pi"
    local pi_pk pi_pid ticket start_epoch elapsed test_file="watchdog-test-$(date +%s).txt"

    pi_pk="$(pi_pubkey)"
    echo "network_test.sh watchdog scenario $(date -Iseconds)" > "$DESKTOP_DEVICES/$test_file"

    # PID written to a file and fetched separately -- see start_pi_node's
    # comment for why `echo $!` through the same ssh call that backgrounds
    # a job hangs the channel indefinitely over Tailscale SSH.
    timeout 20 ssh "$PI_HOST" "cd '$PI_ROOT' || exit 1; HOME='$PI_TEST_HOME' USERPROFILE='$PI_TEST_HOME' setsid nohup \
        '$PI_BIN' --db '$PI_DB' node serve --no-lan-discovery --devices-dir '$PI_DEVICES' \
        > '$PI_LOGS/node2.log' 2>&1 < /dev/null & echo \$! > '$PI_LOGS/node2.pid'" \
        || die "ssh to launch the Pi's watchdog test node did not return within 20s -- see start_pi_node's comment"
    pi_pid="$(timeout 10 ssh "$PI_HOST" "cat '$PI_LOGS/node2.pid'")" \
        || die "ssh to fetch the Pi test node's pidfile did not return within 10s"
    ticket="$(wait_for_ticket "ssh $PI_HOST \"cat '$PI_LOGS/node2.log'\"" 30)"
    [ -n "$ticket" ] || die "pi test node never printed a ticket within 30s for the watchdog scenario"

    start_epoch="$(date +%s)"
    HOME="$DESKTOP_HOME" USERPROFILE="$DESKTOP_HOME" "$VENV_PY" -c "
import asyncio
from pathlib import Path
from roastmesh import device_sync
from roastmesh.identity import load_or_create_identity

async def main():
    ident, _ = load_or_create_identity()
    report = await device_sync.reconcile_with_device('$ticket', ident, Path('$DESKTOP_DEVICES'), Path('$DESKTOP_DEVICE_STATE'))
    print('pushed:', report.pushed, 'pulled:', report.pulled)

asyncio.run(main())
"
    elapsed=$(( $(date +%s) - start_epoch ))

    local remote_content local_content
    local_content="$(cat "$DESKTOP_DEVICES/$test_file")"
    remote_content="$(ssh "$PI_HOST" "cat '$PI_DEVICES/$test_file' 2>/dev/null || echo MISSING")"
    if [ "$local_content" = "$remote_content" ]; then
        log "RESULT watchdog_sync: PASS -- byte-identical on the Pi after ${elapsed}s"
    else
        log "RESULT watchdog_sync: FAIL -- content mismatch or missing after ${elapsed}s (got: $remote_content)"
    fi

    stop_pi_pid "$pi_pid"
    sleep 1
}

# --------------------------------------------------------------------------
# Phase 4 -- cleanup and restore both machines to normal
# --------------------------------------------------------------------------
phase4_teardown() {
    log "Phase 4: cleaning up and restoring both machines"

    # Let the network settle: the scenario just before this (watchdog_sync)
    # kills a test node that was actively doing QUIC/UDP hole-punching and
    # DHT traffic over the same Tailscale interface teardown's own ssh calls
    # need -- live-confirmed 2026-09-07 that ssh calls right here fail with
    # exit 255 (connection-level failure) immediately after that kill, every
    # time, several times in a row, before recovering on their own within a
    # few seconds. A few seconds' pause here is cheap against an ~8-minute
    # run and avoids leaning entirely on ssh_retry for something predictable.
    sleep 5

    # Preserve captured logs/JSON BEFORE wiping the test root -- otherwise
    # the very results this whole run exists to produce are deleted along
    # with the throwaway identity/data a moment later.
    local results_dir="/tmp/roastmesh-nettest-results/$(date +%Y%m%d-%H%M%S)"
    if [ -d "$DESKTOP_LOGS" ]; then
        mkdir -p "$results_dir"
        cp -r "$DESKTOP_LOGS/." "$results_dir/"
        log "captured logs/results preserved at $results_dir"
    fi

    # Match on ROOT, not HOME: not every test process's argv contains the
    # isolated HOME path (e.g. `node serve --db $DESKTOP_DB` only ever
    # mentions $DESKTOP_ROOT-rooted paths), but every one of them is rooted
    # under $DESKTOP_ROOT/$PI_ROOT one way or another -- db, feed, devices,
    # home, logs are all subdirectories of it. pkill -f matches argv, not
    # environment variables, so an env-var-only prefix like HOME=... being
    # set on a command is invisible to it either way.
    pkill -f "$DESKTOP_ROOT" 2>/dev/null || true
    pkill -f "rendezvous_test.py" 2>/dev/null || true
    sleep 1
    rm -rf "$DESKTOP_ROOT"

    # NOT a plain `ssh host "pkill -f '$PI_ROOT'; ..."` -- live-confirmed
    # 2026-09-07 that this is SELF-DEFEATING over ssh: the remote shell
    # sshd/tailscaled invokes to run that whole command has, AS ITS OWN
    # ARGV, the literal string "pkill -f '$PI_ROOT' ..." -- which itself
    # contains $PI_ROOT as a substring. `pkill -f` matches against full
    # argv, so it matches and kills its OWN invoking shell before "sleep 1;
    # rm -rf" ever runs, every single time, deterministically (reproduced
    # directly: `ssh host "pkill -f 'PATH'; echo SURVIVED"` never prints
    # SURVIVED). This is what looked like a flaky "ssh connection failed
    # (255)" teardown failure in earlier runs -- it wasn't the connection,
    # it was suicide. A standalone script file has a stable, filterable
    # name (its own filename) to exclude, which an inline `bash -c` blob
    # does not -- so write one and scp it over instead of matching inline.
    # Written to /tmp, NOT under $DESKTOP_LOGS -- $DESKTOP_ROOT (which
    # contains it) was just rm -rf'd above, so writing it there would fail
    # immediately (confirmed live 2026-09-07: exactly this ordering mistake).
    local local_cleanup_script="/tmp/roastmesh-nettest-pi-cleanup.sh"
    cat > "$local_cleanup_script" <<'EOF'
#!/bin/sh
root="$1"
pgrep -af "$root" | grep -v 'pgrep\|pi_cleanup.sh' | awk '{print $1}' | while read -r pid; do
    kill "$pid" 2>/dev/null
done
sleep 1
rm -rf "$root"
EOF
    scp -q "$local_cleanup_script" "$PI_HOST:/tmp/pi_cleanup.sh"
    rm -f "$local_cleanup_script"
    ssh_retry "$PI_HOST" "sh /tmp/pi_cleanup.sh '$PI_ROOT'; rm -f /tmp/pi_cleanup.sh"

    log "restarting the Pi's real systemd service + update timer"
    ssh_retry "$PI_HOST" "sudo systemctl start $PI_REAL_SERVICE $PI_REAL_TIMER"
    sleep 2
    ssh_retry "$PI_HOST" "systemctl is-active $PI_REAL_SERVICE" | grep -q active \
        || die "the Pi's real service did not come back up after teardown -- investigate immediately, do not leave it down"

    log "relaunching the desktop's real GUI"
    setsid nohup "$DESKTOP_GUI_BIN" > /tmp/roastmesh-gui-restored.log 2>&1 < /dev/null &
    disown
    sleep 2
    pgrep -f "$DESKTOP_GUI_BIN" >/dev/null || die "the desktop's real GUI did not come back up after teardown"

    # Match on the TEST ROOT specifically, not on "roastmesh" generally --
    # live-confirmed 2026-09-07 that a broad "roastmesh" match produces a
    # false-positive NOTE every run, because the just-relaunched real GUI
    # legitimately shells out to the CLI for its own Network tab (a real
    # `node serve --publish-watch-dir ...`) and its paired-devices list
    # (`device list --json`), both of which obviously also match
    # "roastmesh". The one thing that's unambiguous is that $DESKTOP_ROOT
    # was just rm -rf'd above -- nothing legitimate can still reference it,
    # so any process whose argv still does is a genuine leftover, and
    # nothing else needs to be enumerated/excluded.
    local desktop_left pi_left
    desktop_left="$(pgrep -af "$DESKTOP_ROOT" | grep -v pgrep || true)"
    pi_left="$(ssh "$PI_HOST" "pgrep -af '$PI_ROOT'" | grep -v pgrep || true)"
    [ -z "$desktop_left" ] || log "NOTE: unexpected leftover desktop process(es): $desktop_left"
    [ -z "$pi_left" ] || log "NOTE: unexpected leftover pi process(es): $pi_left"

    log "Phase 4 OK: both machines restored to normal -- results preserved at $results_dir"
}

run_scenario() {
    case "$1" in
        dht_baseline) scenario_dht_baseline ;;
        fresh_install_discovery) scenario_fresh_install_discovery ;;
        rendezvous) scenario_rendezvous ;;
        node_doctor) scenario_node_doctor ;;
        feed_sync) scenario_feed_sync ;;
        supersede) scenario_supersede ;;
        cross_edit_staging) scenario_cross_edit_staging ;;
        watchdog_sync) scenario_watchdog_sync ;;
        *) die "unknown scenario: $1 (expected one of: dht_baseline fresh_install_discovery rendezvous node_doctor feed_sync supersede cross_edit_staging watchdog_sync)" ;;
    esac
}

main() {
    case "${1:-}" in
        setup)
            phase0_stop
            phase1_sync_and_build
            phase2_prepare_envs
            ;;
        scenario)
            [ -n "${2:-}" ] || die "usage: $0 scenario <name>"
            run_scenario "$2"
            ;;
        teardown)
            phase4_teardown
            ;;
        all)
            phase0_stop
            phase1_sync_and_build
            phase2_prepare_envs
            scenario_dht_baseline
            scenario_rendezvous
            scenario_node_doctor
            scenario_feed_sync
            scenario_supersede
            scenario_cross_edit_staging
            scenario_watchdog_sync
            phase4_teardown
            log "ALL DONE -- see the 'results preserved at ...' line just above for this run's logs/captured JSON"
            ;;
        *)
            echo "usage: $0 {all|setup|scenario <name>|teardown}" >&2
            exit 1
            ;;
    esac
}

main "$@"
