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
