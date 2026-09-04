"""The unauthenticated discovery datagram.

decode_hello parses a UDP packet anyone on the LAN (or the DHT) can send, and
its `pubkey` field flows into a directory name under peer_feeds. So the
parser is a trust boundary, and these tests treat it as one.
"""
from roastmesh.hello import decode_hello, encode_hello


def test_round_trips_a_well_formed_hello() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    assert decode_hello(encode_hello(pubkey, ticket)) == (pubkey, ticket, False, None, None)


def test_round_trips_a_pairing_hello_with_code_and_hostname() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    encoded = encode_hello(pubkey, ticket, pairing=True, code="4821", hostname="Carl's Pi")
    assert decode_hello(encoded) == (pubkey, ticket, True, "4821", "Carl's Pi")


def test_a_plain_hello_is_byte_identical_to_before_pairing_existed() -> None:
    """The always-on discovery beacon's own wire bytes must never change --
    every already-deployed node's decoder (older builds included) already
    expects exactly {"v": 1, "pubkey": ..., "ticket": ...} and nothing else."""
    import json

    pubkey = "a" * 64
    ticket = "endpointaaaa"
    assert json.loads(encode_hello(pubkey, ticket)) == {"v": 1, "pubkey": pubkey, "ticket": ticket}


def test_a_v1_payload_with_no_new_fields_still_decodes() -> None:
    """A hello from a build that has never heard of pairing/code/hostname --
    decode_hello must still parse it and default the new fields sensibly."""
    import json

    pubkey = "a" * 64
    ticket = "endpointaaaa"
    v1_payload = json.dumps({"v": 1, "pubkey": pubkey, "ticket": ticket}).encode("utf-8")
    assert decode_hello(v1_payload) == (pubkey, ticket, False, None, None)


def test_pairing_hello_omits_code_and_hostname_when_not_given() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    assert decode_hello(encode_hello(pubkey, ticket, pairing=True)) == (pubkey, ticket, True, None, None)


def test_rejects_a_pubkey_that_is_not_64_hex_chars() -> None:
    """A pubkey becomes a peer_feeds directory name, so a value carrying "/"
    or ".." is a path traversal. Found by an adversarial pass: decode_hello
    used to accept any string, including "../../../../tmp/x".
    """
    ticket = "endpointaaaa"
    for hostile in ("../../../../tmp/x", "/tmp/x", "..\\..\\x", "a" * 63,
                    "a" * 65, "A" * 64, "g" * 64, "", "aa\x00bb", "z" * 4096):
        assert decode_hello(encode_hello(hostile, ticket)) is None, hostile


def test_rejects_structurally_broken_datagrams() -> None:
    for junk in (b"", b"not json", b"{}", b'{"pubkey": 5, "ticket": "t"}',
                 b'{"pubkey": "' + b"a" * 64 + b'"}', b"\xff\xfe"):
        assert decode_hello(junk) is None


def test_an_oversized_or_malformed_code_or_hostname_is_dropped_not_rejected() -> None:
    """code/hostname are display-only -- a hostile giant string in either
    must not take down the whole hello, just fall back to "unknown", the
    same posture as every other best-effort display field in this project."""
    import json

    pubkey = "a" * 64
    ticket = "endpointaaaa"
    payload = json.dumps({
        "v": 2, "pubkey": pubkey, "ticket": ticket, "pairing": True,
        "code": "x" * 999, "hostname": 12345,
    }).encode("utf-8")
    assert decode_hello(payload) == (pubkey, ticket, True, None, None)
