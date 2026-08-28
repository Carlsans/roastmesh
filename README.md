# roastnet

A peer-to-peer directory of Artisan (`.alog`) roast profiles: publish your own, discover and
search everyone else's, no server and no accounts. Full design rationale lives in
[`ARCHITECTURE.md`](./ARCHITECTURE.md) — this file is about getting it running.

All 7 steps of that document's build order are implemented: local parsing/search index, signed
feeds, real peer sync over [Iroh](https://iroh.computer), quota enforcement, a desktop GUI,
standalone binaries, and a read-only web gateway. On top of that: automatic peer discovery, both
on your local network and (on by default in the GUI, opt-in from the CLI) over the whole internet
via the public BitTorrent DHT, no tracker or bootstrap node of roastnet's own required — see
[Peer discovery](#peer-discovery-lan-and-internet) below. 278 tests, all passing.

The desktop app (`roastnet-gui`) is the primary way to use this — search, publish (including by
just dropping files in a folder), and serve and sync with peers, all from four tabs, no typing
required. The command line (`roastnet`) does everything the GUI does and more (it's what the GUI
itself runs under the hood), and is there for scripting or if you just prefer a terminal.

## Install

### Linux — one command

```bash
curl -fsSL https://raw.githubusercontent.com/Carlsans/roastnet/master/install.sh | bash
```

Downloads the prebuilt binaries from the [latest release](https://github.com/Carlsans/roastnet/releases/latest),
installs them to `~/.local/bin` (no sudo, no system packages touched), and adds a roastnet entry
to your applications menu so it's a normal double-clickable app afterward. Safe to re-run any
time — re-running just upgrades in place.

The interface is available in English and French, and defaults to whatever language your system
is already set to. To pick one explicitly instead — handy for sharing an install link that opens
straight into a given language — pass `--lang` after `--` (needed because the script is read from
stdin, not run as a file):

```bash
curl -fsSL https://raw.githubusercontent.com/Carlsans/roastnet/master/install.sh | bash -s -- --lang fr
```

Only takes effect on a first install (it seeds `~/.local/share/roastnet/gui_config.json`); on an
existing install, change the language from the app's Settings tab instead. Switch it there at any
time, regardless of how you installed — it applies the next time you open roastnet.

These binaries are built inside an Ubuntu 22.04 container specifically for portability (glibc is
forward-compatible only, so building on an old base is what makes the same binary work on newer
systems too — see `packaging/Dockerfile.build` for why this matters) and are verified, in Docker,
to actually run — both the CLI and the GUI under Xvfb — on Ubuntu 22.04, Ubuntu 24.04, Debian 12,
Fedora, and Arch Linux (`packaging/test-docker.sh` reruns this check any time). x86_64 only for
now; no prebuilt binaries yet for other architectures or for macOS/Windows (PyInstaller doesn't
cross-compile) — see the from-source option below for those.

**ARM (e.g. Raspberry Pi OS)**: no prebuilt binary yet, but the from-source install below works —
confirmed with a real install and a real generated identity under aarch64 emulation. This needs
64-bit Raspberry Pi OS (the default on Pi 3/4/5 and newer); 32-bit Raspberry Pi OS is not
supported, because `iroh`, one of the three real dependencies, has never published a 32-bit ARM
(armv7/armhf) wheel for Linux.

### Everyone else — from source (Linux, macOS, Windows)

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

## Quick start (GUI)

Launch it:
- Installed via the one-command installer above? Click "roastnet" in your applications menu.
- From source (activated venv): `roastnet-gui`
- Ran a prebuilt binary yourself without the installer: `./roastnet-gui` — keep it next to
  `./roastnet`, it shells out to it.

Every label, button, and the roast chart scale together based on your screen's resolution (a
laptop screen and a 4K monitor get noticeably different, appropriate sizes automatically) — adjust
it yourself with Ctrl+scroll or Ctrl+plus/Ctrl+minus, and Ctrl+0 to go back to auto-detect
(Settings tab shows the current percentage). Restarts roastnet to apply, the same brief interruption
(and fresh ticket, if Network was serving) as Stop-then-Start already causes.

Four tabs, left to right:

- **Search** — free-text and filtered search over your local index (your own roasts plus
  anything you've synced from peers). Blank search returns everything. Results show each
  roast's title (Artisan's own `title` field -- often left at its default unless you've renamed
  it) and filename, not an opaque id. Click a column header to sort by it (click again to
  reverse); the sort sticks across new searches. **LAN only** is unchecked by default: peers found
  via internet-wide discovery, manually pasted, or gossiped about show up in results the same as
  LAN-discovered ones -- check it to restrict results to your own roasts and LAN-discovered peers
  only. **Only my own roasts** hides everything synced from any peer (also unchecked by default —
  synced roasts show up alongside your own). **Show hidden roasts too** reveals anything you've hidden (see
  below). **Double-click a result** to see its full detail, open the original `.alog` file with
  whatever your system would normally open it with (Artisan, if it's installed), or **Hide** it
  from your own search results -- local only: it doesn't touch the feed, so it can't retroactively
  un-share something already replicated to a peer (a signed, hash-chained feed entry can't be
  selectively removed without breaking the chain for everything after it); it only changes what
  this machine shows itself. Hide is reversible (**Unhide**, or "Show hidden roasts too" to find
  it again).
- **Publish** — the recommended way is the **shared folder** shown at the top (default
  `~/RoastNetShare`, changeable in Settings): drop `.alog` files in there and they're published
  automatically, no button to click, as long as Network is serving. "Publish a single file"
  below it is the original one-off flow, for a file you don't want to leave in that folder. Your
  feed's address (public key) shows at the top; your identity is created silently the first time
  you publish.
- **Network** — the piece that talks to other machines. The network is **on automatically**
  the moment this tab exists — no click needed — and stays on for as long as the app is open:
  - **Serve your feed**: starts as soon as you open the app. A **ticket** (a long string
    starting with `endpoint...`) appears — that's what you share with someone discovery won't
    reach so they can sync with you directly. Click "Copy" to put it on your clipboard.
    "Stop" if you deliberately want to go offline; "Start serving" resumes (with a fresh ticket
    — it encodes your current network address, not just your identity, so it's expected to
    change run to run).
  - **Automatic LAN discovery**: on by default. Any other roastnet node on the same local
    network is found and synced with on its own, continuously, with zero clicks on either side
    — open the app on two machines on the same LAN and they just find each other.
  - **Automatic internet-wide discovery**: on by default, turned off in Settings if you'd rather
    not. Does the same thing as LAN discovery but across the whole internet — see
    [Peer discovery](#peer-discovery-lan-and-internet) below for how and the trade-off involved.
  - **Sync with a peer**: for someone discovery doesn't reach — paste the ticket *they* gave
    you and click "Sync". Pulls their new feed entries into your search index and exchanges
    known-peer lists both ways, same as an automatic sync does.
  - **Known peers**: everyone found via LAN/internet discovery, synced with manually, or
    gossiped about by another peer — refreshes on its own every few seconds, so this stays live.
- **Settings** — the database file (used to be a bar repeated atop every tab; now set once
  here), the shared publish folder's path, and the internet-wide discovery toggle. Changes to
  the database file apply the next time a tab runs something; changes to the other two apply the
  next time you Stop then Start serving on the Network tab.

That's the whole interface — every button just runs the equivalent command shown in the console
under it, so nothing here is hidden from you.

## Peer discovery: LAN and internet

Two independent, both opt-out-able, layers on top of manual ticket-pasting:

- **LAN discovery** (on by default): a small UDP broadcast on your local network (port `41888`)
  — any roastnet node nearby announces itself and reacts to others' announcements. Never leaves
  the local network.
- **Internet-wide discovery** (on by default in the GUI's Settings tab; off by default from the
  CLI, where it's an explicit opt-in `--wan-discovery` flag): the same idea,
  extended to the whole internet, as easy to join as a BitTorrent swarm. Every opted-in roastnet
  node announces itself on the real, already-running, public **BitTorrent Mainline DHT** — under
  one fixed made-up identifier shared by every roastnet node everywhere, the same way every user
  of one specific torrent is a peer of every other user of that torrent. No tracker or bootstrap
  server of roastnet's own to run or configure; it piggybacks entirely on infrastructure that
  already exists, entering the network through the same well-known routers real BitTorrent
  clients use. Once found, a peer goes through exactly the same handshake, signature
  verification, and quota checks as a LAN-discovered or manually-pasted one — discovery only ever
  produces a "try this address," never trust.

  Finding the swarm is an *iterative* lookup: peers for an identifier are held only by the
  handful of DHT nodes numerically closest to it, so each round asks the closest nodes known so
  far and repeats until it can get no closer, then publishes to exactly those. Nodes that answer
  are remembered in `~/.local/share/roastnet/dht_nodes.json`, which matters more than it sounds —
  most of the historically-cited bootstrap routers no longer answer at all, so after the first
  successful round a node stops depending on them. Expect the first lookup after a fresh install
  to be the weakest one.

  **If it isn't working, ask it why**: `roastnet node doctor` reports which routers answered, how
  close the lookup got, and how many nodes accepted the announcement, instead of leaving you to
  guess.

  **The trade-off, worth knowing**: a LAN broadcast never leaves your local network, but
  announcing on the public DHT makes your node's public IP address (and the fact that it's
  running roastnet) visible to anyone else looking at that same swarm — a materially bigger
  exposure. The GUI defaults this on alongside LAN discovery, since finding peers is the point of
  the app; uncheck it in Settings if you'd rather stay LAN-only. The CLI's `--wan-discovery` flag
  defaults off, since a script's behavior shouldn't change based on this without being asked
  explicitly for it.

  **The honest limitation**: the introduction packet has to arrive from a peer your router has
  never seen you contact, so it depends on your NAT's filtering. Typical home routers let it
  through, especially since both sides send at once (that simultaneous exchange is what opens the
  path). Symmetric NAT and carrier-grade NAT — common on mobile tethering and some corporate and
  ISP networks — will not, and no amount of DHT correctness changes that. A node behind one of
  those simply won't be met this way.

  That limit is one-time rather than permanent, though: it only affects the *introduction*. Once
  two nodes have met by any route — internet discovery, the LAN, or a pasted ticket — each
  remembers the other's public key, which never changes, and Iroh can re-establish the connection
  from the key alone (through its relays and hole-punching) even after both machines have
  restarted on new addresses. Meeting once is the hard part; staying in touch isn't.

## Usage (command line)

Everything the GUI does, it does by running these same commands — useful for scripting, or if
you just prefer a terminal. Assumes either `roastnet` is on your `PATH` (activated venv) or
you're running `./roastnet` / `.venv/bin/roastnet` directly — same commands either way. `--db`
on the top-level command picks the SQLite index file (default `roastnet.sqlite3` in the current
directory); pass an explicit path if you want it somewhere stable regardless of what directory
you happen to run commands from.

**Search your local index, and look at one result in full:**
```bash
roastnet --db ~/roastnet.sqlite3 ingest path/to/some.alog     # or a directory of .alog files
roastnet --db ~/roastnet.sqlite3 search washed ethiopian --machine kaleido_m2 --dtr-min 15
roastnet --db ~/roastnet.sqlite3 show <roast_id>               # roast_id may be a prefix
```
`search` covers everything you have by default -- your own roasts plus every peer's, however that
peer was discovered (LAN, the internet-wide DHT, a pasted ticket, or gossip). Narrow it with
`--lan-only` to just your own roasts and peers on your local network, `--own-only` to only your
own, or add `--show-hidden` to also include roasts you've hidden.

**Hide a roast from your own search results** (local only -- see the GUI bullet above for why
this can't retroactively un-share it from a peer):
```bash
roastnet --db ~/roastnet.sqlite3 hide <roast_id>      # roast_id may be a prefix
roastnet --db ~/roastnet.sqlite3 unhide <roast_id>
```

If an entry looks stale after updating roastnet (an old roast type, a missing title) -- this
fixes itself automatically the next time `node serve` starts (which the GUI always does), by
re-ingesting everything already known once per version, without wiping anything. Run it directly
with `roastnet refresh` (safe and near-instant to run repeatedly -- it skips if already done for
the running version); `--force` re-runs it anyway.

**Publish one of your own roasts** (creates your Ed25519 identity silently on first use):
```bash
roastnet feed publish path/to/your-roast.alog
roastnet identity export      # back up your secret key -- there is no recovery if it's lost
```
Or drop `.alog` files into a folder and let a running `node serve` publish them for you --
see the watch-folder flag below.

**Run a node** so others can sync with you, and **sync with someone else's node**:
```bash
roastnet node serve                      # prints your ticket -- share it with peers
roastnet --db ~/roastnet.sqlite3 peer sync <their-ticket>    # pulls their feed + peer list
roastnet peer list
```
`node serve` also, by default: finds and syncs with other nodes on your local network
(`--no-lan-discovery` to turn off), and auto-publishes any `.alog` file dropped into
`~/RoastNetShare` (`--publish-watch-dir` to change the folder, `--no-publish-watch` to turn
off). Add `--wan-discovery` to also find peers over the whole internet via the public BitTorrent
DHT -- off by default; see [Peer discovery](#peer-discovery-lan-and-internet) above for the
trade-off before turning it on.

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

**If both machines are on the same local network** (true for almost anyone testing this at
home): there's nothing to click for the networking part at all.

**1. On Machine A** — launch `roastnet-gui`, go to the **Publish** tab, choose a `.alog` file,
click Publish. Leave the app running (the **Network** tab is already serving — that started the
moment the app opened).

**2. On Machine B** — launch `roastnet-gui`. Within a few seconds it finds Machine A on its own
(Network tab → Known peers) and automatically pulls its content — no ticket, no clicking Sync.
Switch to the **Search** tab and run a blank search — you should see the roast you published on
Machine A.

**3. Publish something new on Machine A** (Publish tab, while it's still open) — within the same
few-second window, it shows up on Machine B automatically too.

**If the two machines are *not* on the same local network**, automatic discovery can't reach
across networks by nature (it's a local broadcast) — use the **Network** tab's manual path
instead: "Start serving" on Machine A already ran automatically, so just copy its ticket and
paste it into "Sync with a peer" on Machine B.

The CLI equivalent, if you'd rather script it or watch it from a terminal (LAN discovery is on
by default here too — pass `--no-lan-discovery` to `node serve` to turn it off):
```bash
# Machine A
roastnet feed publish path/to/some-roast.alog
roastnet --db ~/roastnet.sqlite3 node serve
# Machine B -- nothing else needed if on the same LAN; it'll auto-discover and
# auto-sync within a few seconds. To sync across networks instead, manually:
roastnet --db ~/roastnet.sqlite3 peer sync '<paste the ticket Machine A printed>'
roastnet --db ~/roastnet.sqlite3 search
```

**If it doesn't connect:**
- Double-check you copied the *entire* ticket, for the manual/cross-network path — it's long,
  and easy to truncate when copy-pasting (the GUI's "Copy" button avoids this; if typing/pasting
  by hand in a terminal, be careful).
- A local firewall on either machine can block the connection — the Iroh QUIC handshake (a UDP
  port picked automatically each run), LAN auto-discovery (`UDP 41888`, broadcast), and, if
  `--wan-discovery`/the Settings toggle is on, internet-wide discovery (`UDP 41890`, plus
  outbound UDP 6881 to the public DHT bootstrap routers) all need to get through. If you have
  `ufw`/`firewalld`/Windows Firewall/etc. active, try temporarily disabling it on both machines
  to confirm that's the cause before figuring out a permanent rule.
- `roastnet node serve` (CLI only — not exposed as a GUI option yet) accepts `--no-relay`, which
  restricts it to direct connections only, no fallback to Iroh's relay infrastructure. This is
  what this project's own automated tests use for same-process testing; for two *separate*
  machines it's untested by me specifically, but worth trying if the default mode doesn't
  connect.
- Both machines need real internet access even for a LAN-only test *unless* you use
  `--no-relay` — the default mode's relay/hole-punch path involves Iroh's public relay
  infrastructure as a fallback.

## Packaging

Two ways to produce `dist/roastnet` + `dist/roastnet-gui`:

- **`packaging/build-docker.sh`** — the one that actually produces distributable binaries (this
  is what built the ones attached to the [releases](https://github.com/Carlsans/roastnet/releases)).
  Builds inside an Ubuntu 22.04 Docker container for portability across newer distros (see
  `packaging/Dockerfile.build`'s comments for why the build machine's glibc matters). Requires
  Docker; needs nothing else installed on the host.
- **`packaging/build.sh`** — builds directly on whatever machine you run it on (needs
  `pip install -e ".[build]"` locally first). Faster for quick local iteration, but the result
  is only guaranteed to run on systems at least as new as the build machine — not what you want
  for something you're about to hand to someone else.

Either way, this has to be run **on each target OS** — PyInstaller does not cross-compile. Only
Linux x86_64 exists today (built and verified, including running fully standalone with no dev
environment present, a real two-binary network sync, and — via `packaging/test-docker.sh` — actually
running on 5 different distros in Docker); macOS and Windows builds are unbuilt and unverified.

## Development

```bash
pip install -e ".[dev]"
pytest -v
```

Some GUI tests need a display; they auto-skip if none is available (`$DISPLAY` unset and no
`Xvfb` installed) rather than failing.

**Proving internet discovery actually works** — one test publishes a random identifier to the
real public BitTorrent DHT and then requires a second, independent lookup to find it. That is the
end-to-end claim, checked against other people's BEP 5 implementations rather than a mock, so it
is the thing to run when internet sharing is suspect:

```bash
ROASTNET_LIVE_DHT=1 pytest tests/test_dht.py -k announce_then_find -v
```

It's opt-in because it takes ~80 seconds and shares the public DHT's per-IP rate limits with
anything else on the machine (including this suite's own GUI tests, which start real serving
nodes) — run back-to-back with those it can fail for reasons unrelated to the code. The
equivalent property is covered offline and in a second by `tests/test_kademlia.py`, which runs a
real in-process DHT swarm, including a check that the *previous*, broken lookup fails it.

## Known limitations

- No real bootstrap nodes exist yet (`roastnet peer bootstrap` is a documented no-op until a
  maintainer runs an always-on node and its ticket gets added to `src/roastnet/bootstrap.py`) —
  in the meantime, use `roastnet peer add <ticket>` (manual, from a friend), or turn on
  internet-wide discovery (Settings tab / `--wan-discovery`), which finds other opted-in nodes
  without needing a bootstrap node at all.
- `peer sync` only replicates the feed of the peer you directly connect to, not a relay of
  everyone *that* peer knows about ("every peer mirrors the entire corpus" from
  `ARCHITECTURE.md`'s Full Replication section is future work).
- Binary sizes (~20 MB each for CLI and GUI, from the portable Docker build) are a bit over the
  doc's aspirational "~10-20 MB" — that
  number describes the JS/Bare or Rust/Iroh-native stacks it lists as alternatives to the Python
  stack actually used here.
