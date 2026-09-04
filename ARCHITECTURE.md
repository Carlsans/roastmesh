# Decentralized Artisan Roast Profile Network — Architecture

Status: design agreed, not yet implemented.

## Goal

A peer-to-peer directory of Artisan (`.alog`) roast profiles. Any user can
publish their own profiles; any user can discover and search everyone else's.
No server, no accounts, no maintainers.

## Core model

**Each user owns exactly one append-only signed feed containing their own
profiles.** Identity is an Ed25519 keypair generated on first run. The public
key *is* the feed address *is* the namespace.

This is the load-bearing decision. Because you can only write to a feed you
hold the private key for, "user A vandalizes user B's data" is not expressible
in the protocol — it needs no policy, no review, and no enforcement. Everything
else follows from this.

```
feed(pubkey_A) -> [profile, profile, profile, ...]   # only A can append
feed(pubkey_B) -> [profile, ...]                     # only B can append
```

A client's view of the world is: a set of known peer pubkeys, plus the
replicated contents of their feeds.

## Transport

Preference order:

1. **Hypercore / Hyperswarm** — append-only signed log per author, single-writer
   by construction, DHT + hole-punching included. Directly expresses the model
   above. Append-only is cryptographically enforced, so authors cannot silently
   rewrite or delete their own history.
2. **Iroh** — QUIC, content-addressed blobs + signed doc namespaces, ships as a
   small static binary. Verify current API surface; it moves fast.

**Rejected: plain BitTorrent.** A torrent infohash covers fixed metadata, so
appending a profile produces a different torrent and a different swarm. BEP 46
mutable torrents (over BEP 44 DHT records) would fix this, but those records are
~1 KB and expire within hours unless refreshed by an online publisher. For users
who open the app twice a week, most pointers would be dead. Making that work
means building a peer-side caching/re-announce layer on top of BitTorrent —
i.e. reimplementing what Hypercore already provides.

## Full replication

A gzipped `.alog` is ~30–60 KB. 10,000 profiles is ~100–300 MB. **Every peer
mirrors the entire corpus.** P2P here is for discovery and resilience, not
bandwidth sharing.

Consequences worth exploiting:
- Availability scales with participant count; no peer is load-bearing.
- Gossip is trivial — peers exchange complete indexes rather than negotiating
  what to fetch.
- The app is fully functional offline.

Revisit only if the corpus exceeds a few GB.

## Abuse resistance

All rules are evaluated **locally and independently by each client**. No
consensus, no shared blocklist, no voting, no maintainers. A peer that violates
them is simply ignored by whoever noticed.

Check feed *metadata* first (a few KB) and only fetch content if it passes:

- max files per feed
- max bytes per feed
- max size per individual file (a `.alog` has no business exceeding ~1 MB)
- max feed growth rate (files/day) — stops a compliant-but-churning peer from
  wasting bandwidth
- file must parse as valid Artisan data

Sybil note: identities are free, so quotas are per-key, not per-person. Someone
can run N compliant identities — but each must stay online and keep seeding or
it falls out of peer lists through normal liveness pruning. That cost is already
unfavorable against a payoff of fake coffee data. **Ship without further sybil
defense**; add per-peer trust weighting only if it becomes a real problem.

## Peer discovery

- Ship a handful of well-known bootstrap node addresses in the binary (same
  pattern as `router.bittorrent.com`). Failure-tolerant: any one working entry
  recovers the whole peer list.
- Manual peer entry / paste-a-key from a friend.
- Peer exchange: on connect, peers swap known-peer lists.
- Liveness pruning: drop peers unreachable for N days, but keep their replicated
  data.

An always-on node (VPS, Pi) run by the maintainers removes availability concerns
entirely, without becoming an authority.

## Device pairing & private sync

Everything above is one identity publishing to one public feed shared with
*everyone*. This is the opposite trust model, for the same person's own
multiple computers:

|                | Public feed                          | Device sync                              |
|----------------|---------------------------------------|-------------------------------------------|
| Direction      | one-way broadcast to all peers        | bidirectional, only between paired devices |
| Mutability     | append-only, immutable                | files change and delete -- full mirror     |
| Audience       | everyone                              | only your SAS-verified own devices         |
| Trust basis    | per-entry Ed25519 signatures          | pairing + a per-connection identity check  |

**Public-feed identities are never merged.** Each device keeps its own
Ed25519 keypair; two devices signing one hash-chained feed would fork it.
"Same user, multiple devices" is instead a local **trusted-device set**
(`devices.json`) recording each other's Ed25519 pubkey, written only after
a human confirms a SAS.

**Pairing (Matrix-style SAS).** The Iroh QUIC handshake already
authenticates a connection as a specific Ed25519 identity --
`conn.remote_id()` *is* the far end's real key, and that cannot be forged
on the wire. The one real attack is substitution at discovery: a LAN
attacker spoofs the pairing beacon and offers *their* ticket, so the human
connects to the attacker's device believing it is their own second laptop.
Two devices in pairing mode commit-then-reveal an ephemeral X25519 key
each, derive a shared secret, and turn it into 7 emoji (HKDF-SHA256 over
the two devices' pubkeys + ephemeral keys, so the emoji are bound to
*this* connection specifically). Both humans compare the two screens: an
attacker sitting on a different, real connection to your actual second
device cannot make its screen show the same 7 emoji this one does. Only on
a match does each side sign the exchange with its long-term identity and
add the other's pubkey to its own trusted-device set.

**Sync (full mirror).** Once paired, a private folder mirrors between
devices: add, change, or delete a file on one and it propagates to every
other reachable paired device. Newest write wins on a conflict (a
wall-clock timestamp stamped fresh whenever a change is actually noticed,
never a file's own mtime, which two machines' clocks cannot be trusted to
agree on). A delete is a **tombstone**, not silence -- without one, a peer
that still has the file would just re-supply it on the next sync. Change
detection is the same mtime/size fingerprint poll the public feed's watch
folder already uses; content is only re-hashed when a fingerprint
actually changes.

**Three ALPNs, one endpoint.** `roastmesh/peer-sync/0` (the public feed,
unchanged), `roastmesh/device-pair/0` (the SAS handshake), and
`roastmesh/device-sync/0` (the folder mirror) all share one bound Iroh
endpoint, routed by `conn.alpn()`. A `device-sync` connection is only ever
answered after `conn.remote_id()` passes the trusted-device check --
otherwise the connection is closed without a single request answered.
That check, not anything in the request itself, is the private folder's
entire access boundary.

**Non-goals.** No feed-identity merge -- a paired device is still its own
independent public-feed author, if it publishes at all. The device folder
is **never** published to, or readable from, the public feed; none of the
quota/abuse-resistance rules above apply to it, because it has no
stranger-facing audience to defend against in the first place.

## Search index

The feeds are the only source of truth. The index is a **pure function of the
corpus and is never synchronized** — it is rebuilt locally. This avoids
convergence problems entirely.

Extract normalized metadata from each `.alog` into SQLite + FTS5: machine,
origin, process, charge weight, CHARGE / DRY / FCs / DROP temps and times, RoR,
development time ratio, density. Target queries look like: "Vienna, washed
Ethiopian, M2 Lite, DTR 18–22%, dropped after 2C".

**Parser must be tolerant.** Artisan's schema has drifted across versions and
annotation fields (`computed`, custom events, alarms) are not guaranteed
present. Record what can't be interpreted rather than rejecting the file.

## SECURITY — non-negotiable

`.alog` is a serialized Python dict. Parse with `ast.literal_eval` or a strict
JSON reader. **Never `eval()`, never `pickle.load()`.** A corpus of files from
strangers that gets `eval`'d on every consumer's machine is a remote code
execution pipeline. State this prominently in contributor docs — someone will
otherwise write a quick script that does exactly this.

Also: reject symlinks, path traversal, and filenames outside a strict allowlist
regex, since profiles land on disk on every peer.

## Distribution

P2P and zero-install are mutually exclusive for a publishing node. Browsers have
no UDP (no DHT) and no raw TCP; WebTorrent only reaches other WebRTC peers, and
a tab that lives four minutes cannot hold a network up. So, split:

- **Full node** — single native binary, ~10–20 MB, no dependency chain,
  double-click to run. Publishes, seeds, participates. This is what roasters
  install. Hypercore via Bare/pkg, or Iroh, both produce this.
- **Read-only web view** — static page against a gateway any full node can
  expose. Zero install, browse and download, contributes nothing. For "someone
  sent me a link" and for evaluating before installing.

Target platforms: Linux, macOS, Windows.

## Key handling

- Generated silently on first publish. No signup, no fields, no email.
- Stored with app config so ordinary machine backups capture it.
- Offer export (recovery phrase or QR) at first publish, when the user has
  motivation to care.
- **No recovery if lost** — there is no credential holder to reset anything.
  Fallback is acceptable: generate a new key and keep publishing. Old profiles
  stay valid, attributed, and replicated; that feed just stops growing.
- Display names live in optional per-feed metadata and are cosmetic only. Never
  trusted for uniqueness — collisions are a rendering problem, not a security
  one.

## Build order

1. `.alog` parser + metadata extractor + SQLite/FTS5 index (standalone, testable
   against local roast files, zero networking).
2. Feed abstraction: create keypair, append profile, verify signatures, replicate.
3. Peer discovery, bootstrap, gossip, liveness pruning.
4. Quota enforcement at the metadata-check boundary.
5. UI — search first, publish second.
6. Packaging into single binaries.
7. Read-only web gateway.

Step 1 is useful on its own even if the network never ships, so it derisks
everything.
