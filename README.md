# roastnet

A peer-to-peer directory of Artisan (`.alog`) roast profiles: publish your own, discover and
search everyone else's, no server and no accounts. Full design rationale lives in
[`ARCHITECTURE.md`](./ARCHITECTURE.md) — this file is about getting it running.

All 7 steps of that document's build order are implemented: local parsing/search index, signed
feeds, real peer sync over [Iroh](https://iroh.computer), quota enforcement, a desktop GUI,
standalone binaries, and a read-only web gateway. 108 tests, all passing.

## Install

Pick whichever of these fits the machine you're on.

### Option A — prebuilt binary (Linux x86_64 only, for now)

This repo's `dist/` (after running `packaging/build.sh` once, see [Packaging](#packaging)) has
two standalone binaries that need nothing else installed — no Python, no venv:

- `dist/roastnet` — the CLI (search, publish, node, peer, gateway).
- `dist/roastnet-gui` — the desktop app. **Keep both files in the same directory** — the GUI
  shells out to the CLI binary sitting next to it.

Copy both to the other machine (`scp dist/roastnet dist/roastnet-gui user@host:~/roastnet/`, a USB
stick, whatever you normally use) and run them directly, e.g. `./roastnet search`. There's
currently no prebuilt macOS or Windows binary — PyInstaller doesn't cross-compile, so one has to
actually be built by running `packaging/build.sh` on a real machine of that OS (see
[Packaging](#packaging)).

### Option B — from source (Linux, macOS, Windows)

Needs Python 3.10+.

```bash
git clone <this repo>   # or just copy the folder
cd claude_roast_share
python3 -m venv .venv
```

Activate the venv:
```bash
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Install:
```bash
pip install -e ".[dev]"
```

This gives you the `roastnet` and `roastnet-gui` commands inside the venv (or run them without
activating via `.venv/bin/roastnet` / `.venv\Scripts\roastnet.exe`).

**GUI on Linux**: `tkinter` isn't always bundled with Python by the system package manager.
- Arch: `sudo pacman -S tk`
- Debian/Ubuntu: `sudo apt install python3-tk`
- Fedora: `sudo dnf install python3-tkinter`

macOS (python.org installer) and Windows (python.org installer) both bundle `tkinter` already —
nothing extra needed there. If you installed Python via Homebrew on macOS instead:
`brew install python-tk`.

## Usage

Everything below assumes either `roastnet` is on your `PATH` (activated venv) or you're running
`./roastnet` / `.venv/bin/roastnet` directly — same commands either way. `--db` on the top-level
command picks the SQLite index file (default `roastnet.sqlite3` in the current directory); pass
an explicit path if you want it somewhere stable regardless of what directory you happen to run
commands from.

**Search your local index:**
```bash
roastnet --db ~/roastnet.sqlite3 ingest path/to/some.alog     # or a directory of .alog files
roastnet --db ~/roastnet.sqlite3 search washed ethiopian --machine kaleido_m2 --dtr-min 15
```

**Publish one of your own roasts** (creates your Ed25519 identity silently on first use):
```bash
roastnet feed publish path/to/your-roast.alog
roastnet identity export      # back up your secret key -- there is no recovery if it's lost
```

**Run a node** so others can sync with you, and **sync with someone else's node**:
```bash
roastnet node serve                      # prints your ticket -- share it with peers
roastnet --db ~/roastnet.sqlite3 peer sync <their-ticket>    # pulls their feed + peer list
roastnet peer list
```

**Desktop app** (Search tab first, Publish tab second):
```bash
roastnet-gui
```

**Read-only web view** of your local index, browsable from any browser on the machine (or your
LAN, with `--host`):
```bash
roastnet gateway serve --db ~/roastnet.sqlite3       # http://127.0.0.1:8420
```

Run `roastnet --help` or `roastnet <command> --help` for the full option list on anything above.

## Testing peer-to-peer sync across two machines on your LAN

This is the actual point of the project, so it's worth walking through end to end. "Machine A"
and "Machine B" below are two different computers on the same local network — any mix of Linux,
macOS, or Windows works, since the protocol is the same everywhere; only the [install](#install)
step differs per OS.

**1. On Machine A** — publish something and start serving:
```bash
roastnet feed publish path/to/some-roast.alog
roastnet node serve
```
This prints your identity's public key and a **ticket** — a long string starting with `endpoint...`.
Copy the whole thing; you'll paste it on Machine B. Leave this command running.

**2. On Machine B** — sync with Machine A using the ticket you just copied:
```bash
roastnet --db ~/roastnet.sqlite3 peer sync '<paste the ticket here>'
```
On success this prints how many new entries it pulled, whether the feed verified, and how many
peers it now knows about. Then confirm it's actually searchable:
```bash
roastnet --db ~/roastnet.sqlite3 search
```
You should see the roast you published on Machine A, with `source_type` recorded as `p2p` (check
with `sqlite3 ~/roastnet.sqlite3 "SELECT source_type, source_ref FROM sources;"` if you want to
see that directly).

**3. Publish something new on Machine A while it's still serving**, then run `peer sync` again on
Machine B with the same ticket — it should report pulling only the new entry, not re-fetching
everything (confirms incremental sync is actually incremental, not a full re-copy each time).

**If it doesn't connect:**
- Double-check you copied the *entire* ticket string — it's long and easy to truncate when
  copy-pasting across a terminal.
- A local firewall on either machine can block the connection — Iroh uses a UDP port (picked
  automatically each run) for the QUIC handshake. If you have `ufw`/`firewalld`/Windows
  Firewall/etc. active, try temporarily disabling it on both machines to confirm that's the
  cause before figuring out a permanent rule.
- `node serve` accepts `--no-relay`, which restricts it to direct connections only (no fallback
  to Iroh's relay infrastructure). This is what this project's own automated tests use for
  same-process testing; for two *separate* machines it's untested by me specifically, but worth
  trying in both directions (with and without it) if the default doesn't connect.
- Both machines need real internet access even for a LAN-only test *unless* you use
  `--no-relay` — the default mode's relay/hole-punch path involves Iroh's public relay
  infrastructure as a fallback.

## Packaging

`packaging/build.sh` runs PyInstaller and produces `dist/roastnet` + `dist/roastnet-gui`. Requires
the `build` extra: `pip install -e ".[build]"`. This has to be run **on each target OS** —
PyInstaller does not cross-compile. Only been built and verified on Linux x86_64 so far (including
running the binaries fully standalone with no dev environment present, and a real two-binary
network sync — see git history / commit message for details); macOS and Windows builds are
unverified.

## Development

```bash
pip install -e ".[dev]"
pytest -v
```

Some GUI tests need a display; they auto-skip if none is available (`$DISPLAY` unset and no
`Xvfb` installed) rather than failing.

## Known limitations

- No real bootstrap nodes exist yet (`roastnet peer bootstrap` is a documented no-op until a
  maintainer runs an always-on node and its ticket gets added to `src/roastnet/bootstrap.py`) —
  use `roastnet peer add <ticket>` (manual, from a friend) in the meantime.
- `peer sync` only replicates the feed of the peer you directly connect to, not a relay of
  everyone *that* peer knows about ("every peer mirrors the entire corpus" from
  `ARCHITECTURE.md`'s Full Replication section is future work).
- Binary sizes (~24 MB CLI, ~53 MB GUI) are well over the doc's aspirational "~10-20 MB" — that
  number describes the JS/Bare or Rust/Iroh-native stacks it lists as alternatives to the Python
  stack actually used here.
