"""pairing.py: the SAS crypto and handshake state machine, entirely offline
-- no iroh endpoint, no socket, just two coroutines talking over in-memory
queues (or, for the tampering test, a single side against a scripted
adversary). This is deliberate: pairing.py's whole point is to be provably
correct independent of the transport it eventually runs over
(device_sync.pair_over_lan).
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from roastmesh.identity import generate_identity
from roastmesh.pairing import PairResult, SAS_EMOJI, _sas_info, run_pairing, sas_from_secret


def test_sas_emoji_table_has_64_distinct_entries() -> None:
    assert len(SAS_EMOJI) == 64
    assert len({name for _emoji, name in SAS_EMOJI}) == 64
    assert len({emoji for emoji, _name in SAS_EMOJI}) == 64


def test_sas_from_secret_is_deterministic() -> None:
    secret = os.urandom(32)
    info = b"some-info"
    assert sas_from_secret(secret, info) == sas_from_secret(secret, info)


def test_sas_from_secret_yields_seven_emoji_from_the_table() -> None:
    sas = sas_from_secret(os.urandom(32), b"info")
    assert len(sas) == 7
    assert all(entry in SAS_EMOJI for entry in sas)


def test_sas_from_secret_differs_with_different_info() -> None:
    secret = os.urandom(32)
    assert sas_from_secret(secret, b"info-one") != sas_from_secret(secret, b"info-two")


def test_sas_info_is_symmetric_regardless_of_argument_order() -> None:
    """Both ends of a handshake call this with their own idea of "self"
    first -- the initiator passes (own, remote), the responder passes
    (remote, own) for the exact same pair. Both must land on identical
    bytes, or the two sides would derive different SAS values for a
    perfectly honest pairing."""
    pubkey_a, pubkey_b = "a" * 64, "b" * 64
    eph_a, eph_b = os.urandom(32), os.urandom(32)
    assert _sas_info(pubkey_a, pubkey_b, eph_a, eph_b) == _sas_info(pubkey_b, pubkey_a, eph_b, eph_a)


def test_mitm_pubkey_substitution_produces_different_sas() -> None:
    """The property that makes comparing emoji actually catch an attack:
    an attacker impersonating Alice's real second device to Alice, and
    impersonating Alice to Bob, cannot make Alice's and Bob's screens agree
    -- because the `info` each side derives its SAS from is bound to the
    *actual* pubkeys in that specific connection, and the attacker's own
    pubkey necessarily appears in exactly one of the two conversations.
    Isolated to just the `info` differing (same shared_secret on both
    sides, the attacker's best case) so this tests exactly that property
    and nothing else.
    """
    shared_secret = os.urandom(32)
    pubkey_alice, pubkey_bob, pubkey_attacker = "a" * 64, "b" * 64, "c" * 64
    eph_alice, eph_bob = os.urandom(32), os.urandom(32)
    eph_attacker_to_alice, eph_attacker_to_bob = os.urandom(32), os.urandom(32)

    info_alice_sees = _sas_info(pubkey_alice, pubkey_attacker, eph_alice, eph_attacker_to_alice)
    info_bob_sees = _sas_info(pubkey_attacker, pubkey_bob, eph_attacker_to_bob, eph_bob)

    assert sas_from_secret(shared_secret, info_alice_sees) != sas_from_secret(shared_secret, info_bob_sees)


def _duplex():
    """Two independent, order-preserving channels -- one per direction --
    so two run_pairing() coroutines can talk to each other exactly the way
    two ends of a real bidirectional stream would, with none of the
    network."""
    a_to_b: asyncio.Queue = asyncio.Queue()
    b_to_a: asyncio.Queue = asyncio.Queue()

    async def send_a(msg: dict) -> None:
        await a_to_b.put(msg)

    async def recv_a() -> dict:
        return await b_to_a.get()

    async def send_b(msg: dict) -> None:
        await b_to_a.put(msg)

    async def recv_b() -> dict:
        return await a_to_b.get()

    return (send_a, recv_a), (send_b, recv_b)


async def test_full_happy_handshake_over_an_in_memory_duplex() -> None:
    id_a = generate_identity()
    id_b = generate_identity()
    (send_a, recv_a), (send_b, recv_b) = _duplex()

    result_a, result_b = await asyncio.gather(
        run_pairing(send=send_a, recv=recv_a, own_identity=id_a,
                    remote_pubkey_hex=id_b.public_key_hex, is_initiator=True,
                    confirm=lambda sas: True),
        run_pairing(send=send_b, recv=recv_b, own_identity=id_b,
                    remote_pubkey_hex=id_a.public_key_hex, is_initiator=False,
                    confirm=lambda sas: True),
    )

    assert result_a.ok is True
    assert result_b.ok is True
    assert result_a.sas == result_b.sas
    assert len(result_a.sas) == 7
    assert result_a.remote_pubkey_hex == id_b.public_key_hex
    assert result_b.remote_pubkey_hex == id_a.public_key_hex


async def test_either_sides_confirm_returning_false_fails_both() -> None:
    id_a = generate_identity()
    id_b = generate_identity()
    (send_a, recv_a), (send_b, recv_b) = _duplex()

    result_a, result_b = await asyncio.gather(
        run_pairing(send=send_a, recv=recv_a, own_identity=id_a,
                    remote_pubkey_hex=id_b.public_key_hex, is_initiator=True,
                    confirm=lambda sas: False),  # "they don't match"
        run_pairing(send=send_b, recv=recv_b, own_identity=id_b,
                    remote_pubkey_hex=id_a.public_key_hex, is_initiator=False,
                    confirm=lambda sas: True),
    )

    assert result_a.ok is False
    assert result_b.ok is False
    # Both computed a real SAS before either confirmed -- refusal is a human
    # decision on top of a completed derivation, not a failure to derive one.
    assert result_a.sas is not None
    assert result_b.sas is not None


async def test_async_confirm_callback_is_awaited() -> None:
    id_a = generate_identity()
    id_b = generate_identity()
    (send_a, recv_a), (send_b, recv_b) = _duplex()

    async def confirm_yes(_sas):
        return True

    result_a, result_b = await asyncio.gather(
        run_pairing(send=send_a, recv=recv_a, own_identity=id_a,
                    remote_pubkey_hex=id_b.public_key_hex, is_initiator=True, confirm=confirm_yes),
        run_pairing(send=send_b, recv=recv_b, own_identity=id_b,
                    remote_pubkey_hex=id_a.public_key_hex, is_initiator=False, confirm=confirm_yes),
    )
    assert result_a.ok is True
    assert result_b.ok is True


async def test_tampered_reveal_is_rejected_and_nothing_is_trusted() -> None:
    """A scripted adversarial peer: it commits honestly to one ephemeral
    key, then reveals a *different* one. run_pairing must catch the hash
    mismatch on its own, without ever needing the other side to behave --
    exactly the shape of an active tamperer sitting on the wire.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    id_a = generate_identity()
    id_attacker = generate_identity()

    committed_key = X25519PrivateKey.generate().public_key().public_bytes_raw()
    swapped_key = X25519PrivateKey.generate().public_key().public_bytes_raw()
    assert committed_key != swapped_key

    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "commit", "commitment": hashlib.sha256(committed_key).hexdigest()})
    await inbox.put({"type": "reveal", "eph_pub": swapped_key.hex()})
    sent: list[dict] = []

    async def send(msg: dict) -> None:
        sent.append(msg)

    async def recv() -> dict:
        return await inbox.get()

    result = await run_pairing(
        send=send, recv=recv, own_identity=id_a,
        remote_pubkey_hex=id_attacker.public_key_hex, is_initiator=True,
        confirm=lambda sas: True,
    )

    assert result.ok is False
    assert result.remote_pubkey_hex is None
    assert result.sas is None
    assert "commitment" in (result.error or "").lower()
    # Never got past the reveal check to the point of asking a human to
    # confirm anything -- "commit" and "reveal" are the only two messages
    # this side ever sent.
    assert [m["type"] for m in sent] == ["commit", "reveal"]


async def test_a_malformed_reveal_message_is_rejected_not_raised() -> None:
    id_a = generate_identity()
    id_attacker = generate_identity()
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "commit", "commitment": "not-a-real-hash"})
    await inbox.put({"type": "reveal", "eph_pub": "not-hex-at-all"})

    async def send(_msg: dict) -> None:
        pass

    async def recv() -> dict:
        return await inbox.get()

    result = await run_pairing(
        send=send, recv=recv, own_identity=id_a,
        remote_pubkey_hex=id_attacker.public_key_hex, is_initiator=True, confirm=lambda sas: True,
    )
    assert result.ok is False
