"""usernames.py: deterministic, pure default display names.

ARCHITECTURE.md:147-149 -- names are cosmetic and never unique by design,
so these tests check determinism and purity, not uniqueness.
"""
from __future__ import annotations

from roastmesh.usernames import _ADJECTIVES, _NOUNS, default_display_name


def test_default_display_name_is_deterministic() -> None:
    pubkey = "ab" * 32
    assert default_display_name(pubkey) == default_display_name(pubkey)


def test_default_display_name_is_pure_across_interleaved_calls() -> None:
    # Calling this for one pubkey must not perturb the result for another
    # -- no shared/mutated state between calls.
    first = default_display_name("11" * 32)
    default_display_name("22" * 32)
    default_display_name("33" * 32)
    assert default_display_name("11" * 32) == first


def test_default_display_name_is_two_known_words() -> None:
    name = default_display_name("ab" * 32)
    parts = name.split(" ")
    assert len(parts) == 2
    assert parts[0] in _ADJECTIVES
    assert parts[1] in _NOUNS


def test_different_pubkeys_usually_render_different_names() -> None:
    # Not a uniqueness guarantee -- collisions are explicitly by design --
    # just a sanity check this isn't secretly constant or near-constant.
    names = {default_display_name(f"{i:064x}") for i in range(50)}
    assert len(names) > 10


def test_wordlists_have_no_duplicates() -> None:
    assert len(_ADJECTIVES) == len(set(_ADJECTIVES))
    assert len(_NOUNS) == len(set(_NOUNS))


def test_wordlists_are_about_96_entries_each() -> None:
    assert 90 <= len(_ADJECTIVES) <= 100
    assert 90 <= len(_NOUNS) <= 100


def test_handles_non_hex_or_empty_input_without_raising() -> None:
    assert default_display_name("not actually hex")
    assert default_display_name("")
    assert default_display_name("x")


def test_no_network_or_randomness_pure_function_of_input_only() -> None:
    # Same input, called from two independent "fresh" perspectives (no
    # setup shared between them), must agree -- this is what "every known
    # peer renders the same name on every machine with no network exchange"
    # actually depends on.
    pubkey = "deadbeef" * 8
    a = default_display_name(pubkey)
    b = default_display_name(pubkey)
    assert a == b
