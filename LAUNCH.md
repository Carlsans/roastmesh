# roastmesh launch plan

How to take roastmesh from "works on my machines" to "roasters are using it."
This is a playbook, meant to be worked top to bottom.

## The one-line pitch

> **Share and discover Artisan roast profiles, peer-to-peer — no cloud, no
> account, no fee.**

Differentiators to lead with, everywhere: **free · noncommercial · no accounts ·
no server · works with your existing Artisan `.alog` files · private P2P.** That
is a real contrast with the paid tools roasters already know (artisan.plus,
Cropster, roast.world subscriptions).

Word it as **"free and noncommercial, source-available"** — never "open source."
The PolyForm Noncommercial license isn't OSI open-source, and the HN crowd will
call that out instantly if the wording is loose.

---

## Phase 0 — before you announce anything (the cold-start fix)

A P2P app with no peers shows a newcomer an empty screen. Fix that first, or the
first wave bounces.

1. **Ship real bootstrap nodes.** `src/roastmesh/bootstrap.py`'s
   `BOOTSTRAP_TICKETS` is empty, so a fresh install finds nobody and
   `roastmesh peer bootstrap` is a no-op. Run the always-on Pi **and** a small
   VPS as seed nodes, and put their tickets in `BOOTSTRAP_TICKETS` so every new
   install immediately finds the network. (Internet-wide DHT discovery also
   works without this, but a shipped bootstrap ticket is the reliable path.)
2. **Seed a starter library.** Publish a curated set of well-labeled roast
   profiles on the seed nodes — a range of machines, origins, and roast levels —
   so a first-time search returns real, useful results in seconds. Aim for a few
   dozen good roasts, not an empty index.
3. **Stand up the read-only web view.** `roastmesh gateway serve` exposes a
   browsable HTTP view; put it behind a URL. It's the landing page and it makes
   "someone sent me a link" work with zero install.
4. **Record a 20–40s demo GIF/video:** drop an `.alog` in the share folder → it
   appears in your feed → open it on the graph → find a stranger's roast and open
   theirs. This single asset does most of the persuading.
5. **Refresh the README top** around the pitch above, with the GIF, the
   noncommercial line, and a one-command install.

Only when 0–5 are done does the network make a good first impression.

---

## Phase 1 — where to announce (in order)

Sequence matters: warm, on-target communities first (they'll give honest
feedback and become the seed users), broad reach next, the tech crowd last once
the rough edges are smoothed.

### 1. Roasting forums — highest signal

- **Home-Barista.com** → *Roasting Coffee* subforum. The premier English
  home/specialty roasting community; serious Artisan users live here.
- **Coffee Snobs (coffeesnobs.com.au)** → home-roasting boards. Very active,
  especially AU/NZ.
- *Tone:* introduce it as a hobby project that scratches a real itch — sharing
  and discovering roast profiles without a cloud or account. Ask for feedback,
  answer every reply, post as a participant, not an ad. Read each forum's
  self-promotion rules first; some want it in a specific thread.

### 2. Aillio & Artisan communities — most on-target

- **Aillio Bullet owner groups** (Facebook, and the roast.world community). Huge,
  engaged base; many also use Artisan. Frame roastmesh as *"a free way to share
  the roasts you already log in Artisan."*
- **Artisan project channels** — its GitHub Discussions and community Discord.
  roastmesh is an Artisan *companion*, so this is squarely on-target. Be explicit
  that it's independent and complements Artisan, not competes with it.

### 3. Reddit — broad reach

- **r/roasting** (home roasters — the core), then **r/espresso** / **r/coffee**.
- Lead with the demo GIF; a short, honest "I built this, it's free, feedback
  welcome" post. Reddit rewards the GIF and punishes anything that smells like
  marketing.

### 4. Hacker News — the engineering angle, last

- **Show HN:** the interesting tech is the hook — decentralized, no server, no
  accounts, iroh/QUIC + BitTorrent Mainline DHT, single native binary, signed
  append-only feeds. Title like *"Show HN: Peer-to-peer sharing of coffee-roast
  profiles (no server, no accounts)."*
- **Do not call it "open source"** (noncommercial license). Post weekday morning
  US time; be present in the thread all day — HN is a conversation.

---

## Phase 2 — keep it alive

- **Be present.** Early on, the founder answering questions *is* the marketing.
- **Keep the seed nodes up.** Availability is the product; a Pi + VPS that never
  sleep are the backbone until the network is self-sustaining.
- **Turn feedback into a short roadmap** in the README so people see it's alive.
- **A tiny landing page** (the gateway view, or a static page) gives every post a
  single link that survives.

---

## Draft posts (adapt per venue)

**Forum / Reddit (short):**
> I made a free tool to share and discover Artisan roast profiles peer-to-peer —
> no cloud, no account, no subscription. Drop your `.alog` files in a folder and
> they're shared; search everyone else's roasts and open them on a chart with the
> usual stats (DTR, phases, RoR). It also reads CSV exports. It's noncommercial
> and the source is available. Would love feedback from people who actually
> roast. [demo GIF] [download]

**Show HN:**
> Show HN: Peer-to-peer sharing of coffee-roast profiles (no server, no accounts)
>
> Roasters log their roasts in Artisan (`.alog` files). There's no good way to
> share and discover each other's profiles without a paid cloud. roastmesh is a
> single-binary desktop app that does it peer-to-peer: signed append-only feeds
> per user, discovery over the BitTorrent Mainline DHT, transport over iroh/QUIC,
> a local SQLite/FTS index, and every node mirrors others' feeds so a roast
> survives its author going offline. Free and noncommercial (PolyForm NC —
> source-available, not OSI open-source). Happy to go deep on the DHT/sybil and
> replication design.

---

## Honest caveats to keep in view

- **Noncommercial ≠ open source.** Consistent wording avoids a credibility hit.
- **The network needs participants.** Every phase above is really about not
  showing an empty app; the seed nodes and seed library are non-negotiable.
- **tkinter has a ceiling.** The app looks good (Sun Valley, dark mode, a real
  roast chart), but it's a native desktop tool, not a web app — set that
  expectation rather than over-promising.
