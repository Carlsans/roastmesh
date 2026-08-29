"""profile.py: your own signed profile -- sign/verify round trip, tamper
detection, load/save, and the deterministic-name fallback.
"""
from __future__ import annotations

from pathlib import Path

from roastmesh.identity import generate_identity
from roastmesh.profile import (
    Profile,
    default_profile_path,
    load_or_default_profile,
    load_profile,
    save_profile,
    sign_profile,
    update_and_sign,
    verify_profile,
)
from roastmesh.usernames import default_display_name


def _fake_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_default_profile_path_lives_beside_identity_json_not_in_gui_config(
    monkeypatch, tmp_path: Path
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    assert default_profile_path() == home / ".config" / "roastmesh" / "profile.json"


def test_default_profile_path_is_computed_fresh_each_call(monkeypatch, tmp_path: Path) -> None:
    # Same convention identity.default_identity_path() relies on: a cached
    # module-level constant would ignore a later HOME monkeypatch.
    _fake_home(monkeypatch, tmp_path)
    first = default_profile_path()
    other_home = tmp_path / "elsewhere"
    other_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: other_home))
    second = default_profile_path()
    assert first != second
    assert second == other_home / ".config" / "roastmesh" / "profile.json"


def test_sign_and_verify_round_trip() -> None:
    identity = generate_identity()
    profile = Profile(
        pubkey="", name="Amber Chaff", machine_key="aillio_bullet", machine_display="Aillio Bullet R1"
    )
    sign_profile(identity, profile)

    assert profile.pubkey == identity.public_key_hex
    assert profile.sig
    assert verify_profile(profile.to_dict()) is True


def test_tampered_name_fails_verification() -> None:
    identity = generate_identity()
    profile = Profile(pubkey="", name="Amber Chaff")
    sign_profile(identity, profile)

    tampered = profile.to_dict()
    tampered["name"] = "Someone Else Entirely"
    assert verify_profile(tampered) is False


def test_tampered_likes_fails_verification() -> None:
    identity = generate_identity()
    profile = Profile(pubkey="", name="Amber Chaff", likes=["ab" * 32])
    sign_profile(identity, profile)

    tampered = profile.to_dict()
    tampered["likes"] = tampered["likes"] + ["cd" * 32]
    assert verify_profile(tampered) is False


def test_tampered_machine_fails_verification() -> None:
    identity = generate_identity()
    profile = Profile(pubkey="", name="Amber Chaff", machine_key="aillio_bullet")
    sign_profile(identity, profile)

    tampered = profile.to_dict()
    tampered["machine_key"] = "kaleido_m2"
    assert verify_profile(tampered) is False


def test_verify_fails_when_sig_or_pubkey_missing() -> None:
    assert verify_profile({}) is False
    assert verify_profile({"pubkey": "ab" * 32}) is False
    assert verify_profile({"sig": "00" * 64}) is False


def test_verify_fails_on_garbage_sig_hex() -> None:
    assert verify_profile({"pubkey": "ab" * 32, "sig": "not-hex-at-all"}) is False


def test_verify_fails_when_signed_by_a_different_identity() -> None:
    identity = generate_identity()
    other = generate_identity()
    profile = Profile(pubkey="", name="Amber Chaff")
    sign_profile(identity, profile)

    claimed_as_other = profile.to_dict()
    claimed_as_other["pubkey"] = other.public_key_hex
    assert verify_profile(claimed_as_other) is False


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    identity = generate_identity()
    profile = Profile(pubkey="", name="Amber Chaff", machine_key="aillio_bullet")
    sign_profile(identity, profile)
    save_profile(profile, path)

    loaded = load_profile(path)
    assert loaded is not None
    assert loaded.name == "Amber Chaff"
    assert loaded.machine_key == "aillio_bullet"
    assert verify_profile(loaded.to_dict()) is True


def test_load_profile_returns_none_when_no_file_exists(tmp_path: Path) -> None:
    assert load_profile(tmp_path / "does_not_exist.json") is None


def test_name_falls_back_to_default_display_name_when_unset() -> None:
    pubkey = "ab" * 32
    profile = Profile.from_dict({"pubkey": pubkey})
    assert profile.name == default_display_name(pubkey)


def test_name_falls_back_to_default_display_name_when_blank_string() -> None:
    pubkey = "cd" * 32
    profile = Profile.from_dict({"pubkey": pubkey, "name": ""})
    assert profile.name == default_display_name(pubkey)


def test_load_or_default_profile_uses_deterministic_default_when_none_saved(tmp_path: Path) -> None:
    identity = generate_identity()
    path = tmp_path / "profile.json"

    profile = load_or_default_profile(identity, path)

    assert profile.pubkey == identity.public_key_hex
    assert profile.name == default_display_name(identity.public_key_hex)
    assert not path.exists()  # a default is not itself saved


def test_load_or_default_profile_returns_the_saved_one_once_it_exists(tmp_path: Path) -> None:
    identity = generate_identity()
    path = tmp_path / "profile.json"
    update_and_sign(identity, name="Amber Chaff", path=path)

    profile = load_or_default_profile(identity, path)
    assert profile.name == "Amber Chaff"


def test_update_and_sign_round_trips_through_disk(tmp_path: Path) -> None:
    identity = generate_identity()
    path = tmp_path / "profile.json"

    update_and_sign(
        identity,
        name="Amber Chaff",
        machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1",
        likes=["cd" * 32],
        path=path,
    )

    loaded = load_profile(path)
    assert loaded is not None
    assert loaded.name == "Amber Chaff"
    assert loaded.machine_key == "aillio_bullet"
    assert loaded.machine_display == "Aillio Bullet R1"
    assert loaded.likes == ["cd" * 32]
    assert loaded.updated_at
    assert verify_profile(loaded.to_dict()) is True


def test_update_and_sign_leaves_unspecified_fields_unchanged(tmp_path: Path) -> None:
    identity = generate_identity()
    path = tmp_path / "profile.json"
    update_and_sign(identity, name="Amber Chaff", machine_key="aillio_bullet", path=path)

    update_and_sign(identity, machine_display="Aillio Bullet R1 IBTS", path=path)

    loaded = load_profile(path)
    assert loaded is not None
    assert loaded.name == "Amber Chaff"  # untouched by the second call
    assert loaded.machine_key == "aillio_bullet"  # untouched by the second call
    assert loaded.machine_display == "Aillio Bullet R1 IBTS"
    assert verify_profile(loaded.to_dict()) is True


def test_profile_json_is_written_with_encoding_utf8(tmp_path: Path) -> None:
    identity = generate_identity()
    path = tmp_path / "profile.json"
    update_and_sign(identity, name="Amber Chaff", path=path)
    # Would raise if written with a platform-default (e.g. cp1252) encoding
    # and this decode disagreed with it.
    path.read_text(encoding="utf-8")
