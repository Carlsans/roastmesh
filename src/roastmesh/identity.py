"""Ed25519 identity: the keypair a user's feed is addressed by and signed with.

ARCHITECTURE.md's Core Model: "Identity is an Ed25519 keypair generated on
first run. The public key *is* the feed address *is* the namespace." Kept
independent of whatever key Iroh's Endpoint uses for its own connection
identity (Iroh's Python bindings don't yet expose general-purpose signing on
that key) -- transport identity and content-signing identity being separate
keys is a normal split, and nothing here blocks unifying them later.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from roastmesh.paths import config_dir


@dataclass
class Identity:
    _private_key: Ed25519PrivateKey

    @property
    def public_key_hex(self) -> str:
        raw = self._private_key.public_key().public_bytes_raw()
        return raw.hex()

    @property
    def secret_key_hex(self) -> str:
        return self._private_key.private_bytes_raw().hex()

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data)

    @classmethod
    def from_secret_key_hex(cls, secret_key_hex: str) -> "Identity":
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_key_hex)))


def generate_identity() -> Identity:
    return Identity(Ed25519PrivateKey.generate())


def verify(public_key_hex: str, data: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


def default_identity_path() -> Path:
    """Computed fresh on every call (never as a module-level constant or a
    default parameter value) so it correctly reflects the current HOME --
    a frozen constant would make tests that monkeypatch HOME silently write
    to the real user's config instead of an isolated test path."""
    return config_dir() / "identity.json"


def save_identity(identity: Identity, path: Path | None = None) -> None:
    path = path or default_identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"secret_key_hex": identity.secret_key_hex}), encoding="utf-8")
    os.chmod(path, 0o600)


def load_identity(path: Path | None = None) -> Identity:
    path = path or default_identity_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Identity.from_secret_key_hex(data["secret_key_hex"])


def load_or_create_identity(path: Path | None = None) -> tuple[Identity, bool]:
    """Returns (identity, created). `created` is True the first time this is
    called for a given path -- generated silently, no signup, matching
    ARCHITECTURE.md's Key Handling section -- so callers (the CLI) can print
    a one-time "back this up" reminder without requiring a separate init step.
    """
    path = path or default_identity_path()
    if path.exists():
        return load_identity(path), False
    identity = generate_identity()
    save_identity(identity, path)
    return identity, True
