#!/usr/bin/env bash
# Log CPU time actually consumed by a running roastmesh-gui -- including
# every subprocess it spawns, even short-lived ones a plain `top` snapshot
# would usually miss entirely -- to a CSV file for later analysis.
#
# Background: three real, measured causes of sustained idle CPU use (and
# the fan-cycling that comes with it) were found and fixed using this same
# technique:
#   1. The GUI used to shell out to a whole new `roastmesh peer list`
#      process every 5 seconds, forever, just to refresh the "Known
#      peers" table -- ~0.3s of CPU per run under a packaged build
#      (PyInstaller onefile self-extracts on every launch). Now every 30s.
#   2. The background watch-folder scanner re-read and re-hashed every
#      file in the watch folder on every 10-second tick, forever, even
#      when nothing had changed -- now skips files whose size+mtime match
#      what was already checked.
#   3. The dominant one: an automatically LAN-discovered peer got a full
#      resync every 60 seconds forever -- and a real sync against an
#      actually-live peer (not just two processes on one host, which syncs
#      in ~30ms and completely hid this) measured at ~9 seconds of
#      mostly-CPU-bound work. One always-on LAN peer meant ~15% of a core,
#      continuously; two, ~30% -- easily enough to keep a laptop's fan
#      cycling even with nobody touching the app. Now every 15 minutes.
# If a machine still shows the pattern after upgrading, this script is how
# to get hard, shareable evidence of what's actually using the CPU and how
# often, for a fresh report.
#
# How it measures short bursts accurately: rather than sampling
# instantaneous %CPU (which can miss a subprocess that starts and exits
# between samples), this reads /proc/<pid>/stat's utime/stime (own CPU)
# AND cutime/cstime (the CUMULATIVE CPU time of every child process
# already waited on, including ones that exited between samples) every
# interval, so a burst of short-lived subprocess spawns shows up in full
# even if none of them happen to be alive at the exact sampling moment.
#
# It also sums across every process matching each role, not just the
# first one found: a packaged (PyInstaller onefile) binary launches as
# TWO processes per invocation (an outer bootloader that self-extracts and
# waits, and the inner one that does the real work) -- confirmed directly
# while building this tool: tracking only the first (often the idle outer
# one) silently missed almost all of the real signal.
#
# Usage:
#   packaging/benchmark_cpu.sh [duration_seconds] [interval_seconds] [logfile]
#     duration_seconds  how long to sample for (default 3600 = 1 hour)
#     interval_seconds  how often to sample (default 2)
#     logfile           CSV output path (default ./roastmesh_cpu_log_<timestamp>.csv)
#
# Start roastmesh-gui first, then run this alongside it (it finds the
# already-running process by name). Leave the app idle for a meaningful
# "is it doing anything with nobody touching it" reading, or use it
# normally if you want a realistic usage-pattern log instead.
#
# Requires: Linux (reads /proc -- this technique has no equivalent on
# macOS/Windows), awk, getconf. No Python, no extra packages -- this is
# meant to run on a plain end-user install with just the packaged binary,
# not a development checkout.
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "This tool reads /proc, which only exists on Linux." >&2
    exit 1
fi

DURATION="${1:-3600}"
INTERVAL="${2:-2}"
LOGFILE="${3:-roastmesh_cpu_log_$(date +%Y%m%d_%H%M%S).csv}"

GUI_PATTERN='roastmesh-gui'
SERVE_PATTERN='node serve'

if ! pgrep -f "$GUI_PATTERN" > /dev/null; then
    echo "No running roastmesh-gui process found. Start it first, then run this." >&2
    exit 1
fi

CLK_TCK="$(getconf CLK_TCK)"

echo "roastmesh CPU benchmark"
echo "  watching for '$GUI_PATTERN' and '$SERVE_PATTERN' processes for ${DURATION}s, sampling every ${INTERVAL}s"
echo "  logging to: $LOGFILE"
echo "  own_cpu_pct      = summed CPU use of every matching process, this interval"
echo "  children_cpu_pct = CPU consumed by subprocesses they spawned and finished"
echo "                     this interval (e.g. the periodic peer-list check) --"
echo "                     a 'top' snapshot usually misses this entirely"
echo

# Prints "utime stime cutime cstime" (clock ticks) for $1, or "0 0 0 0" if
# the process is gone. /proc/<pid>/stat's 2nd field (the command name) is
# parenthesized and can itself contain spaces or parens, so field-splitting
# from the start is unsafe -- taking everything after the *last* ')' and
# counting from there is the standard safe way to parse this file.
read_stat_fields() {
    local statfile="/proc/$1/stat"
    if [ ! -r "$statfile" ]; then
        echo "0 0 0 0"
        return
    fi
    awk '{
        s = $0
        close_paren = 0
        for (i = length(s); i > 0; i--) {
            if (substr(s, i, 1) == ")") { close_paren = i; break }
        }
        rest = substr(s, close_paren + 2)
        n = split(rest, f, " ")
        print f[12], f[13], f[14], f[15]
    }' "$statfile" 2>/dev/null || echo "0 0 0 0"
}

rss_kb_of() {
    local statusfile="/proc/$1/status"
    if [ -r "$statusfile" ]; then
        awk '/VmRSS/{print $2}' "$statusfile" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

echo "timestamp,elapsed_s,pids,role,own_cpu_pct,children_cpu_pct,rss_kb" > "$LOGFILE"

declare -A PREV_UT PREV_ST PREV_CUT PREV_CST

start_ts="$(date +%s)"
end_ts=$((start_ts + DURATION))

# Sums CPU deltas across every currently-matching process for one role
# (see the header comment on why this can be more than one process).
# Returns 1 (nothing logged) if no process matches right now.
sample_role() {
    local pattern="$1" role="$2" now="$3"
    local pids; pids="$(pgrep -f "$pattern" || true)"
    [ -n "$pids" ] || return 1

    local sum_own=0 sum_children=0 sum_rss=0 pid_list=""
    local pid ut st cut cst rss_kb key p_ut p_st p_cut p_cst
    for pid in $pids; do
        [ -d "/proc/$pid" ] || continue
        read -r ut st cut cst <<< "$(read_stat_fields "$pid")"
        rss_kb="$(rss_kb_of "$pid")"

        key="${role}_${pid}"
        p_ut="${PREV_UT[$key]:-$ut}"; p_st="${PREV_ST[$key]:-$st}"
        p_cut="${PREV_CUT[$key]:-$cut}"; p_cst="${PREV_CST[$key]:-$cst}"
        PREV_UT[$key]=$ut; PREV_ST[$key]=$st; PREV_CUT[$key]=$cut; PREV_CST[$key]=$cst

        sum_own=$(( sum_own + (ut - p_ut) + (st - p_st) ))
        sum_children=$(( sum_children + (cut - p_cut) + (cst - p_cst) ))
        sum_rss=$(( sum_rss + rss_kb ))
        pid_list="${pid_list:+$pid_list+}$pid"
    done
    [ -n "$pid_list" ] || return 1

    local own_pct children_pct
    own_pct=$(awk -v d="$sum_own" -v tck="$CLK_TCK" -v iv="$INTERVAL" 'BEGIN{printf "%.2f", (d/tck)/iv*100}')
    children_pct=$(awk -v d="$sum_children" -v tck="$CLK_TCK" -v iv="$INTERVAL" 'BEGIN{printf "%.2f", (d/tck)/iv*100}')

    echo "$(date '+%Y-%m-%dT%H:%M:%S'),$((now - start_ts)),$pid_list,$role,$own_pct,$children_pct,$sum_rss" >> "$LOGFILE"
    return 0
}

while [ "$(date +%s)" -lt "$end_ts" ]; do
    now="$(date +%s)"
    if ! sample_role "$GUI_PATTERN" "gui" "$now"; then
        echo "roastmesh-gui no longer found -- stopping early." >&2
        break
    fi
    sample_role "$SERVE_PATTERN" "serve" "$now" || true  # not an error -- serving may be stopped
    sleep "$INTERVAL"
done

echo
echo "Done. Wrote $LOGFILE"
echo
echo "Summary (average % of one CPU core across the whole run):"
awk -F, 'NR>1 {own[$4]+=$5; child[$4]+=$6; n[$4]++}
    END {
        for (role in own) {
            printf "  %-6s  own=%.2f%%  children=%.2f%%  (n=%d samples)\n", \
                role, own[role]/n[role], child[role]/n[role], n[role]
        }
    }' "$LOGFILE"
echo
echo "A steady, nonzero percentage means something is repeatedly doing real"
echo "work in the background -- open the CSV and look for which rows have"
echo "the highest own_cpu_pct/children_cpu_pct to see how regularly."
