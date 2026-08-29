"""machines.py: the generated Artisan catalogue, the hand-written
home-roaster supplement, and the exact-match lookup alog/machine.py's
normalize_machine_key leans on.
"""
from __future__ import annotations

from roastmesh.machines import (
    GENERATED_MACHINES,
    HOME_ROASTER_SUPPLEMENT,
    MACHINES,
    find_by_roastertype,
    list_machines,
    slugify,
)


def test_generated_catalogue_matches_artisans_own_counts() -> None:
    # tools/build_machine_catalogue.py against a fresh Artisan checkout
    # found 259/262 .aset files carrying roastertype_setup, yielding 254
    # unique strings across 90 manufacturers -- pin those counts so a
    # future regeneration that silently drops or duplicates entries is
    # caught here rather than downstream.
    assert len(GENERATED_MACHINES) == 254
    assert len({m.manufacturer for m in GENERATED_MACHINES}) == 90


def test_generated_entries_have_unique_display_names() -> None:
    names = [m.display_name for m in GENERATED_MACHINES]
    assert len(names) == len(set(names))


def test_non_ascii_qsettings_escape_decoded_correctly() -> None:
    # The source .aset files spell this "B\xfchler" (QSettings' own escape
    # for a raw Latin-1 byte) -- it must have decoded to an actual "ü", not
    # stayed escaped or been mangled into something else.
    buhler = [m for m in GENERATED_MACHINES if m.manufacturer == "Bühler"]
    assert buhler
    assert all("ü" in m.display_name for m in buhler)
    assert all("\\x" not in m.display_name for m in buhler)


def test_find_by_roastertype_is_case_insensitive_exact_match() -> None:
    hit = find_by_roastertype("kaleido serial")
    assert hit is not None
    assert hit.key == "kaleido_serial"
    assert hit.manufacturer == "Kaleido"
    assert find_by_roastertype("KALEIDO SERIAL").key == "kaleido_serial"


def test_find_by_roastertype_strips_whitespace() -> None:
    assert find_by_roastertype("  Behmor 1600 Plus  ") is not None


def test_find_by_roastertype_does_not_substring_match() -> None:
    # "Kaleido Serial 2" is not a catalogue entry -- exact match only, no
    # heuristic/substring fallback here (that's alog/machine.py's job).
    assert find_by_roastertype("Kaleido Serial 2") is None


def test_find_by_roastertype_returns_none_for_unknown_empty_or_none() -> None:
    assert find_by_roastertype("not a real roaster at all") is None
    assert find_by_roastertype("") is None
    assert find_by_roastertype(None) is None  # type: ignore[arg-type]


def test_home_roaster_supplement_keys_are_disjoint_from_generated() -> None:
    # The supplement is meant to add machines Artisan doesn't have, not
    # accidentally re-add ones it already does (e.g. Aillio Bullet, Hottop).
    generated_keys = {m.key for m in GENERATED_MACHINES}
    supplement_keys = {m.key for m in HOME_ROASTER_SUPPLEMENT}
    assert generated_keys.isdisjoint(supplement_keys)


def test_home_roaster_supplement_covers_the_named_popular_machines() -> None:
    supplement_names = " ".join(m.display_name for m in HOME_ROASTER_SUPPLEMENT)
    for expected in ("Behmor", "Gene Café", "Quest", "Kaffelogic", "Sandbox Smart",
                      "Cormorant", "Huky", "Fresh Roast", "Sonofresco", "Nesco"):
        assert expected in supplement_names


def test_list_machines_covers_the_full_catalogue_sorted_for_display() -> None:
    machines = list_machines()
    assert len(machines) == len(MACHINES) == len(GENERATED_MACHINES) + len(HOME_ROASTER_SUPPLEMENT)
    keys = [(m.manufacturer.lower(), m.display_name.strip().lower()) for m in machines]
    assert keys == sorted(keys)


def test_slugify_matches_documented_examples() -> None:
    assert slugify("Aillio Bullet R1") == "aillio_bullet_r1"
    assert slugify("  Multiple   Spaces -- Here!! ") == "multiple_spaces_here"
    assert slugify("") == "unknown"
    assert slugify("---") == "unknown"


def test_every_catalogue_key_is_the_key_ingest_would_actually_store() -> None:
    """The catalogue must not invent a second machine_key vocabulary.

    A picker offers `display_name` and stores its `key`; ingest derives a key
    from the .alog's own `roastertype`. If those two disagree, a user who
    picks their own machine in Settings matches none of their own roasts --
    which is the entire point of the machine facet. Regression: the catalogue
    advertised "aillio_bullet_r1" while every ingested Bullet roast is stored
    as "aillio_bullet", and `profile set --machine aillio_bullet` was rejected
    as an unknown machine despite being the only key the index contained.
    """
    from roastmesh.alog.machine import normalize_machine_key

    mismatched = [
        (m.display_name, m.key, normalize_machine_key(m.display_name)[0])
        for m in MACHINES
        if normalize_machine_key(m.display_name)[0] != m.key
    ]
    assert not mismatched, (
        "catalogue entries whose key differs from the key ingest would store: "
        f"{mismatched}"
    )


def test_the_brands_with_pre_existing_keys_collapse_onto_them() -> None:
    """The specific rows the invariant above had to reconcile -- several
    models legitimately share one searchable key, with the precise model
    preserved in display_name (which is what users.machine_display holds)."""
    by_display = {m.display_name: m.key for m in MACHINES}
    assert by_display["Aillio Bullet R1"] == "aillio_bullet"
    assert by_display["Aillio Bullet R2"] == "aillio_bullet"
    assert by_display["Hottop 2K+"] == "hottop"
    assert by_display["Behmor 1600 Plus"] == "behmor"
    assert by_display["Kaleido Network"] == "kaleido_serial"
    # ...while everything the alias table never claimed keeps its own key.
    assert by_display["Giesen W6A"] == "giesen_w6a"
