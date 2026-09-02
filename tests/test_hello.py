"""The unauthenticated discovery datagram.

decode_hello parses a UDP packet anyone on the LAN (or the DHT) can send, and
its `pubkey` field flows into a directory name under peer_feeds. So the
parser is a trust boundary, and these tests treat it as one.
"""
from roastmesh.hello import decode_hello, encode_hello


def test_round_trips_a_well_formed_hello() -> None:
    pubkey = "a" * 64
    ticket = "endpointaaaa"
    assert decode_hello(encode_hello(pubkey, ticket)) == (pubkey, ticket)


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
