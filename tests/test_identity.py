import sys
from pathlib import Path

import pytest

from roastnet.identity import (
    generate_identity,
    load_identity,
    load_or_create_identity,
    save_identity,
    verify,
)


def test_generate_identity_has_valid_hex_public_key() -> None:
    identity = generate_identity()
    assert len(identity.public_key_hex) == 64  # 32 bytes, hex-encoded
    bytes.fromhex(identity.public_key_hex)  # doesn't raise


def test_sign_and_verify_round_trip() -> None:
    identity = generate_identity()
    signature = identity.sign(b"hello roastnet")
    assert verify(identity.public_key_hex, b"hello roastnet", signature) is True


def test_verify_fails_on_tampered_data() -> None:
    identity = generate_identity()
    signature = identity.sign(b"hello roastnet")
    assert verify(identity.public_key_hex, b"goodbye roastnet", signature) is False


def test_verify_fails_with_wrong_public_key() -> None:
    identity = generate_identity()
    other = generate_identity()
    signature = identity.sign(b"hello roastnet")
    assert verify(other.public_key_hex, b"hello roastnet", signature) is False


def test_verify_fails_on_garbage_signature() -> None:
    identity = generate_identity()
    assert verify(identity.public_key_hex, b"hello", b"not a real signature") is False


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    identity = generate_identity()
    save_identity(identity, path)

    loaded = load_identity(path)
    assert loaded.public_key_hex == identity.public_key_hex
    assert loaded.sign(b"x") != b""  # can still sign after reload
    assert verify(identity.public_key_hex, b"x", loaded.sign(b"x")) is True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes. Windows chmod honours only the read-only bit, so 0o600 "
           "cannot be asserted -- the key is protected there by the per-user ACL on "
           "%APPDATA% instead, which is weaker and worth knowing about.",
)
def test_save_identity_sets_restrictive_permissions(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    save_identity(generate_identity(), path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_or_create_creates_once_then_loads(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    assert not path.exists()

    identity1, created1 = load_or_create_identity(path)
    assert created1 is True
    assert path.exists()

    identity2, created2 = load_or_create_identity(path)
    assert created2 is False
    assert identity2.public_key_hex == identity1.public_key_hex
