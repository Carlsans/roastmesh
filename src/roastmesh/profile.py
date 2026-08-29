"""Your own signed profile: display name, declared machine, and likes.

Stored at `config_dir()/profile.json`, a sibling of `identity.json` -- NOT in
`gui_config.json`, which is documented as GUI-only and unread by the CLI
(`gui/config.py`). Peers need to be able to see this (a later phase serves
it over the wire), so it has to live somewhere the CLI reads and writes too.

Follows `identity.default_identity_path()`'s convention
(`identity.py:55-60`): the path is computed fresh on every call, never
cached at module level, so a test that monkeypatches HOME stays isolated --
a frozen constant would make it silently write to the real user's config.

Shape (`sig` is Ed25519 over the canonical bytes of every other field --
`json.dumps(payload, sort_keys=True, separators=(",", ":"))`, signed with
`Identity.sign` and checked with `identity.verify`):

    {"v": 1, "pubkey": "<hex>", "name": "Amber Chaff", "machine_key": "aillio_bullet",
     "machine_display": "Aillio Bullet R1", "likes": ["<hex>", ...],
     "updated_at": "2026-08-28T00:00:00+00:00", "sig": "<hex>"}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from roastmesh.identity import Identity, verify
from roastmesh.paths import config_dir
from roastmesh.usernames import default_display_name

PROFILE_VERSION = 1


@dataclass
class Profile:
    pubkey: str
    name: str
    machine_key: str | None = None
    machine_display: str | None = None
    likes: list[str] = field(default_factory=list)
    updated_at: str = ""
    sig: str = ""

    def to_dict(self) -> dict:
        return {
            "v": PROFILE_VERSION,
            "pubkey": self.pubkey,
            "name": self.name,
            "machine_key": self.machine_key,
            "machine_display": self.machine_display,
            "likes": list(self.likes),
            "updated_at": self.updated_at,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        pubkey = data["pubkey"]
        return cls(
            pubkey=pubkey,
            # Falls back to the deterministic default whenever a profile on
            # disk (or received from a peer) has no name set yet.
            name=data.get("name") or default_display_name(pubkey),
            machine_key=data.get("machine_key"),
            machine_display=data.get("machine_display"),
            likes=list(data.get("likes") or []),
            updated_at=data.get("updated_at", ""),
            sig=data.get("sig", ""),
        )


def default_profile_path() -> Path:
    """Computed fresh on every call -- never cache this at module level or
    as a default parameter value (see this module's docstring)."""
    return config_dir() / "profile.json"


def _canonical_bytes(payload: dict) -> bytes:
    """Bytes actually signed/verified: every field of `payload` except
    `sig` itself, canonicalized identically on both sides."""
    signable = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(signable, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_profile(identity: Identity, profile: Profile) -> Profile:
    """Stamps `profile.pubkey` from `identity` and (re)computes `profile.sig`
    over everything else. Mutates and returns the same Profile."""
    profile.pubkey = identity.public_key_hex
    profile.sig = identity.sign(_canonical_bytes(profile.to_dict())).hex()
    return profile


def verify_profile(profile_dict: dict) -> bool:
    """True iff `profile_dict["sig"]` is a valid Ed25519 signature by
    `profile_dict["pubkey"]` over the canonical bytes of every other field.
    False (never an exception) for anything malformed -- a bad signature
    from an untrusted peer is an expected, not exceptional, outcome."""
    pubkey = profile_dict.get("pubkey")
    sig_hex = profile_dict.get("sig")
    if not pubkey or not sig_hex:
        return False
    try:
        signature = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    return verify(pubkey, _canonical_bytes(profile_dict), signature)


def save_profile(profile: Profile, path: Path | None = None) -> None:
    path = path or default_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.to_dict(), sort_keys=True), encoding="utf-8")


def load_profile(path: Path | None = None) -> Profile | None:
    """None if no profile has been saved yet -- distinct from an empty/
    default one, so callers can tell "never set anything" from "set the
    default explicitly"."""
    path = path or default_profile_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Profile.from_dict(data)


def load_or_default_profile(identity: Identity, path: Path | None = None) -> Profile:
    """The caller's own saved profile, or a fresh (unsaved) one seeded from
    `identity` and `usernames.default_display_name` if none exists yet."""
    path = path or default_profile_path()
    existing = load_profile(path)
    if existing is not None:
        return existing
    pubkey = identity.public_key_hex
    return Profile(pubkey=pubkey, name=default_display_name(pubkey))


def update_and_sign(
    identity: Identity,
    *,
    name: str | None = None,
    machine_key: str | None = None,
    machine_display: str | None = None,
    likes: list[str] | None = None,
    path: Path | None = None,
) -> Profile:
    """Load the existing profile (or start a fresh default one), apply any
    given field overrides -- `None` means "leave unchanged" -- stamp
    `updated_at`, re-sign, and save. Returns the saved Profile."""
    path = path or default_profile_path()
    profile = load_or_default_profile(identity, path)
    if name is not None:
        profile.name = name
    if machine_key is not None:
        profile.machine_key = machine_key
    if machine_display is not None:
        profile.machine_display = machine_display
    if likes is not None:
        profile.likes = list(likes)
    profile.updated_at = datetime.now(timezone.utc).isoformat()
    sign_profile(identity, profile)
    save_profile(profile, path)
    return profile
