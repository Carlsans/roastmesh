"""The unauthenticated discovery datagram.

decode_hello parses a UDP packet anyone on the LAN (or the DHT) can send, and
its `pubkey` field flows into a directory name under peer_feeds. So the
parser is a trust boundary, and these tests treat it as one.
"""
from roastmesh.hello import decode_hello, encode_hello


def test_round_trips_a_well_formed_hello() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    assert decode_hello(encode_hello(pubkey, ticket)) == (pubkey, ticket, False, None, None, [])


def test_round_trips_a_pairing_hello_with_code_and_hostname() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    encoded = encode_hello(pubkey, ticket, pairing=True, code="4821", hostname="Carl's Pi")
    assert decode_hello(encoded) == (pubkey, ticket, True, "4821", "Carl's Pi", [])


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
    assert decode_hello(v1_payload) == (pubkey, ticket, False, None, None, [])


def test_pairing_hello_omits_code_and_hostname_when_not_given() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    assert decode_hello(encode_hello(pubkey, ticket, pairing=True)) == (pubkey, ticket, True, None, None, [])


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
    assert decode_hello(payload) == (pubkey, ticket, True, None, None, [])


def test_round_trips_known_peers_gossip() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    other_pubkey = "b" * 64
    other_ticket = "endpointbbbb"
    encoded = encode_hello(pubkey, ticket, known_peers=[(other_pubkey, other_ticket)])
    assert decode_hello(encoded) == (pubkey, ticket, False, None, None,
                                      [(other_pubkey, other_ticket)])


def test_known_peers_is_capped_at_max_entries() -> None:
    """A hello must stay comfortably within a safe UDP payload size -- a
    hostile or buggy peer relaying an unbounded gossip list must not be able
    to inflate every hello it causes to be sent onward."""
    from roastmesh.hello import _MAX_KNOWN_PEERS

    pubkey = "a" * 64
    ticket = "endpointaaaa"
    many_peers = [(f"{i:064x}", f"endpoint{i}") for i in range(_MAX_KNOWN_PEERS + 10)]
    decoded = decode_hello(encode_hello(pubkey, ticket, known_peers=many_peers))
    assert decoded is not None
    assert len(decoded[5]) == _MAX_KNOWN_PEERS
    assert decoded[5] == many_peers[:_MAX_KNOWN_PEERS]


def test_known_peers_with_a_malformed_entry_drops_only_that_entry() -> None:
    """Gossip is unauthenticated third-party data relayed by an intermediary
    -- one bad entry (a hostile pubkey, an oversized ticket, wrong shape)
    must not take down an otherwise-valid introduction to everyone else in
    the same list, and must not reject the hello's own (pubkey, ticket)."""
    import json

    pubkey = "a" * 64
    ticket = "endpointaaaa"
    good_pubkey = "b" * 64
    payload = json.dumps({
        "v": 3, "pubkey": pubkey, "ticket": ticket,
        "known_peers": [
            [good_pubkey, "endpointgood"],
            ["not-a-pubkey", "endpointbad"],
            [good_pubkey, "x" * 9999],  # oversized ticket
            "not-a-list",
            [good_pubkey],  # wrong length
        ],
    }).encode("utf-8")
    decoded = decode_hello(payload)
    assert decoded is not None
    assert decoded[:5] == (pubkey, ticket, False, None, None)
    assert decoded[5] == [(good_pubkey, "endpointgood")]


def test_known_peers_absent_on_an_older_payload_decodes_to_empty_list() -> None:
    """A hello from a build that has never heard of gossip -- decode_hello
    must still parse it and default known_peers to [], not crash or None."""
    import json

    pubkey = "a" * 64
    ticket = "endpointaaaa"
    v1_payload = json.dumps({"v": 1, "pubkey": pubkey, "ticket": ticket}).encode("utf-8")
    assert decode_hello(v1_payload) == (pubkey, ticket, False, None, None, [])
