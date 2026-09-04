"""Matrix-style SAS (Short Authentication String) device pairing.

Why this exists at all, given the transport already authenticates: Iroh's
QUIC handshake authenticates every connection as a specific Ed25519 identity
-- `conn.remote_id()` *is* the far end's real feed pubkey (net.py), and that
cannot be forged by anything on the wire. So the network layer cannot make
you talk to the wrong *key*. What it cannot stop is the wrong *device*
answering in the first place: an attacker on the same LAN can spoof the
pairing beacon, offer their own (real, validly-signed) ticket, and a user who
just clicks "pair" connects to the attacker's device while believing it is
their own second laptop. Comparing 7 emoji, shown independently on both
screens, closes that gap -- the emoji are derived from the two ephemeral keys
actually used in *this* connection, so an attacker sitting in the middle of a
real second, correct connection to your real laptop cannot make its screen
show the same 7 emoji this one does (see run_pairing's docstring for the
mechanism).

This module is deliberately transport-agnostic: `run_pairing` takes `send`/
`recv` callables and a `confirm` callback instead of touching a socket, so
the whole protocol -- including the MITM-detecting property itself -- is
unit-testable over two in-memory queues (tests/test_pairing.py), with no iroh
endpoint needed. device_sync.py's `pair_over_lan` is what actually wires this
to a real QUIC connection.
"""
from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Union

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from roastmesh.identity import Identity, verify as verify_signature

# The canonical 64-entry Matrix SAS emoji table (spec: "SAS as Emoji"),
# reused verbatim rather than inventing our own -- it was already designed
# so every entry is visually and semantically distinct at a glance (no two
# entries that look alike or share a name), which is exactly the property a
# human eyeballing 7 of them under time pressure needs. Index is what
# sas_from_secret's 6-bit groups pick with, so the order is load-bearing:
# changing it would change every SAS this project has ever shown someone.
SAS_EMOJI: list[tuple[str, str]] = [
    ("🐶", "Dog"), ("🐱", "Cat"), ("🦁", "Lion"), ("🐎", "Horse"),
    ("🦄", "Unicorn"), ("🐷", "Pig"), ("🐘", "Elephant"), ("🐰", "Rabbit"),
    ("🐼", "Panda"), ("🐓", "Rooster"), ("🐧", "Penguin"), ("🐢", "Turtle"),
    ("🐟", "Fish"), ("🐙", "Octopus"), ("🦋", "Butterfly"), ("🌷", "Flower"),
    ("🌳", "Tree"), ("🌵", "Cactus"), ("🍄", "Mushroom"), ("🌏", "Globe"),
    ("🌙", "Moon"), ("☁️", "Cloud"), ("🔥", "Fire"), ("🍌", "Banana"),
    ("🍎", "Apple"), ("🍓", "Strawberry"), ("🌽", "Corn"), ("🍕", "Pizza"),
    ("🎂", "Cake"), ("❤️", "Heart"), ("😀", "Smiley"), ("🤖", "Robot"),
    ("🎩", "Hat"), ("👓", "Glasses"), ("🔧", "Spanner"), ("🎅", "Santa"),
    ("👍", "Thumbs up"), ("☂️", "Umbrella"), ("⌛", "Hourglass"), ("⏰", "Clock"),
    ("🎁", "Gift"), ("💡", "Light bulb"), ("📕", "Book"), ("✏️", "Pencil"),
    ("📎", "Paperclip"), ("✂️", "Scissors"), ("🔒", "Lock"), ("🔑", "Key"),
    ("🔨", "Hammer"), ("☎️", "Telephone"), ("🏁", "Flag"), ("🚂", "Train"),
    ("🚲", "Bicycle"), ("✈️", "Aeroplane"), ("🚀", "Rocket"), ("🏆", "Trophy"),
    ("⚽", "Ball"), ("🎸", "Guitar"), ("🎺", "Trumpet"), ("🔔", "Bell"),
    ("⚓", "Anchor"), ("🎧", "Headphones"), ("📁", "Folder"), ("📌", "Pin"),
]
assert len(SAS_EMOJI) == 64  # every 6-bit index (0-63) must resolve to something


def sas_from_secret(shared_secret: bytes, info: bytes) -> list[tuple[str, str]]:
    """Derive the 7-emoji SAS both sides show for comparison.

    HKDF-SHA256(shared_secret, length=6 bytes, salt=None, info=info) yields
    48 bits of output-keying material; the *top* 42 of those 48 bits split
    into 7 groups of 6 bits each, most-significant group first, each
    indexing SAS_EMOJI. The bottom 6 bits are simply unused -- 7*6=42 is the
    largest multiple of 6 that fits in 48, and discarding a partial leftover
    group (rather than, say, wrapping it into an 8th short group) is what
    the Matrix SAS spec this table is borrowed from also does, so this stays
    interoperable with that derivation in spirit even though the transport
    and `info` binding here are roastmesh's own.

    `info` is what actually binds the SAS to a specific pairing exchange:
    HKDF's `info` parameter is *domain separation*, not a secret -- feed it
    anything that differs between two exchanges (see _sas_info) and their
    SAS values become independent even given the exact same shared_secret.
    """
    okm = HKDF(algorithm=hashes.SHA256(), length=6, salt=None, info=info).derive(shared_secret)
    # 48 bits as one big-endian integer; >> 6 drops the unused low 6 bits,
    # leaving the top 42 as a 42-bit value. Group i (0 = most significant)
    # is then bits [36-6i .. 41-6i] of that 42-bit value.
    top_42_bits = int.from_bytes(okm, "big") >> 6
    return [SAS_EMOJI[(top_42_bits >> (36 - 6 * i)) & 0x3F] for i in range(7)]


def _sas_info(pubkey_a: str, pubkey_b: str, eph_a: bytes, eph_b: bytes) -> bytes:
    """The HKDF `info` for one pairing exchange between two identities.

    `eph_a` must be the ephemeral public key belonging to `pubkey_a`, and
    `eph_b` to `pubkey_b` -- but which of the two identities a caller calls
    "a" and which "b" must NOT matter, since initiator and responder each
    call this with their own idea of "self" first. So the two (pubkey, eph)
    pairs are sorted by pubkey before being folded in, which is what makes
    both ends land on byte-identical `info` regardless of role -- the
    load-bearing property this function exists for. This is also exactly
    what makes the MITM case detectable: an attacker who is really talking
    to A as "pubkey X" and to your real second device as "pubkey Y" cannot
    make both of those conversations sort to the same `info`, because X and
    Y are different keys -- so the two SAS values the two conversations
    produce necessarily differ (see tests/test_pairing.py).
    """
    (first_pk, first_eph), (second_pk, second_eph) = sorted(
        [(pubkey_a, eph_a), (pubkey_b, eph_b)], key=lambda pair: pair[0],
    )
    return b"roastmesh-sas-v1|" + first_pk.encode("utf-8") + b"|" + second_pk.encode("utf-8") \
        + b"|" + first_eph + b"|" + second_eph


@dataclass
class PairResult:
    ok: bool
    remote_pubkey_hex: str | None
    sas: list[tuple[str, str]] | None
    # Why `ok` is False, for a caller (the CLI, the GUI's pairing modal) to
    # show a human -- never itself a trust decision, only ok is.
    error: str | None = None


SendFn = Callable[[dict], Awaitable[None]]
RecvFn = Callable[[], Awaitable[dict]]
ConfirmFn = Callable[[list[tuple[str, str]]], Union[bool, Awaitable[bool]]]


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


async def run_pairing(
    *, send: SendFn, recv: RecvFn, own_identity: Identity,
    remote_pubkey_hex: str, is_initiator: bool, confirm: ConfirmFn,
) -> PairResult:
    """Run the SAS handshake to completion over an already-authenticated
    channel, deciding whether the human on the other end is really the
    device this identity's owner intends to pair with.

    `remote_pubkey_hex` is NOT discovered by this protocol -- it is handed
    in already known (device_sync.pair_over_lan passes `conn.remote_id()`,
    the QUIC-authenticated identity of whoever we actually dialed). What
    this function adds on top is proof that a *human* looked at the same 7
    emoji this connection derives on both ends and said they matched, bound
    cryptographically (via the final signature check) to that exact
    `remote_pubkey_hex` -- so a "yes they match" can never be replayed
    against, or silently apply to, a different identity than the one shown.

    `is_initiator` does not change the cryptography: commit-then-reveal
    (step 2 below) already prevents either side from choosing its ephemeral
    key *after* seeing the other's, which is the only thing an
    initiator/responder split would otherwise be needed to guarantee, so
    both roles run the identical sequence below. It is accepted purely for
    the caller's own bookkeeping (e.g. which side dialled the QUIC
    connection) and logging.

    Protocol, symmetric on both ends:
      1. Generate an ephemeral X25519 keypair.
      2. Commit/reveal: send SHA256(own ephemeral pubkey) *before* either
         side has seen the other's ephemeral key at all, then, only after
         receiving the peer's commitment, reveal the real ephemeral pubkey.
         This is what stops a malicious peer from picking its own ephemeral
         key as a function of ours to steer the resulting SAS -- by the time
         either side can see the other's real key, both commitments are
         already fixed.
      3. Verify the peer's revealed key actually hashes to the commitment it
         sent first; reject outright (ok=False) if it doesn't -- this is the
         one thing a mid-flight tamperer could otherwise get away with.
      4. ECDH the two ephemeral keys into a shared secret, derive the 7-emoji
         SAS from it (sas_from_secret), and ask BOTH sides' own human to
         confirm (`confirm(sas)`) -- ok=False the moment either side declines.
      5. Only once both confirmed: sign `info` (the same bytes the SAS was
         derived from) with this identity's real, long-term key and exchange
         that signature, verifying the peer's against `remote_pubkey_hex`.
         This is the step that actually binds "the human said yes" to the
         specific long-term identity the connection is authenticated as --
         without it, the emoji comparison alone would prove two ephemeral
         keys agree but never tie that back to a real, addressable pubkey.
    """
    own_pubkey_hex = own_identity.public_key_hex
    eph_private = X25519PrivateKey.generate()
    eph_public = eph_private.public_key().public_bytes_raw()
    commitment = hashlib.sha256(eph_public).hexdigest()

    await send({"type": "commit", "commitment": commitment})
    commit_msg = await recv()
    if not isinstance(commit_msg, dict) or commit_msg.get("type") != "commit" \
            or not isinstance(commit_msg.get("commitment"), str):
        return PairResult(False, None, None, error="protocol error: expected a commitment")
    peer_commitment = commit_msg["commitment"]

    await send({"type": "reveal", "eph_pub": eph_public.hex()})
    reveal_msg = await recv()
    if not isinstance(reveal_msg, dict) or reveal_msg.get("type") != "reveal":
        return PairResult(False, None, None, error="protocol error: expected a reveal")
    try:
        peer_eph_public = bytes.fromhex(reveal_msg.get("eph_pub", ""))
    except ValueError:
        return PairResult(False, None, None, error="malformed ephemeral key")

    if hashlib.sha256(peer_eph_public).hexdigest() != peer_commitment:
        # The one thing an active tamperer could otherwise pull off: swap in
        # a different ephemeral key after seeing ours but before revealing
        # its own. Caught here, and nothing past this point is trusted.
        return PairResult(False, None, None, error="commitment mismatch -- the reveal was tampered with")

    try:
        peer_eph_public_key = X25519PublicKey.from_public_bytes(peer_eph_public)
        shared_secret = eph_private.exchange(peer_eph_public_key)
    except ValueError:
        return PairResult(False, None, None, error="invalid ephemeral public key")

    info = _sas_info(own_pubkey_hex, remote_pubkey_hex, eph_public, peer_eph_public)
    sas = sas_from_secret(shared_secret, info)

    own_confirmed = bool(await _maybe_await(confirm(sas)))
    await send({"type": "confirmed", "ok": own_confirmed})
    if not own_confirmed:
        return PairResult(False, remote_pubkey_hex, sas, error="not confirmed on this device")

    peer_confirm_msg = await recv()
    if not isinstance(peer_confirm_msg, dict) or not peer_confirm_msg.get("ok"):
        return PairResult(False, remote_pubkey_hex, sas, error="not confirmed on the other device")

    # Both sides said the emoji matched -- now bind that to the actual
    # long-term identities, not just the ephemeral keys the emoji came from.
    signature = own_identity.sign(info)
    await send({"type": "ack", "signature": signature.hex()})
    ack_msg = await recv()
    if not isinstance(ack_msg, dict):
        return PairResult(False, remote_pubkey_hex, sas, error="protocol error: expected an ack")
    try:
        peer_signature = bytes.fromhex(ack_msg.get("signature", ""))
    except ValueError:
        return PairResult(False, remote_pubkey_hex, sas, error="malformed signature")
    if not verify_signature(remote_pubkey_hex, info, peer_signature):
        return PairResult(False, remote_pubkey_hex, sas, error="the other device's signature did not verify")

    return PairResult(True, remote_pubkey_hex, sas)
