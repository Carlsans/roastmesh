"""machines.py: the Artisan-derived catalogue, the researched model names,
the hand-written home-roaster supplement, and the exact-match lookup
alog/machine.py's normalize_machine_key leans on.

The load-bearing tests here are the two invariants at the bottom -- that no
two entries fight over one Artisan string, and that every entry's key is the
key ingest would actually store. Everything above them is a count or a
spot-check that exists to catch a bad regeneration.
"""
from __future__ import annotations

from collections import Counter

from roastmesh.alog.machine import normalize_machine_key
from roastmesh.machines import (
    ARTISAN_MACHINES,
    HOME_ROASTER_SUPPLEMENT,
    MACHINES,
    RESEARCHED_MACHINES,
    effective_key,
    find_by_roastertype,
    list_machines,
    slugify,
)


def test_the_catalogue_covers_every_artisan_string_but_the_non_roasters() -> None:
    """Artisan ships 254 unique roastertype strings. Four of them name
    hardware that is not a roaster at all -- three Phidget thermocouple
    interface boards and an Artisan plugin -- and are deliberately absent,
    so a file carrying one falls through to the "unknown" bucket instead of
    offering an interface board as a machine.
    """
    strings = [s for m in ARTISAN_MACHINES for s in m.artisan_strings]
    assert len(strings) == 250
    assert len({s.lower() for s in strings}) == 250, "an Artisan string is claimed twice"
    for not_a_roaster in ("Phidget 2xRTD", "Phidget 2xTC", "Phidget Databridge",
                          "Plugin Roast"):
        assert find_by_roastertype(not_a_roaster) is None


def test_a_non_roaster_lands_in_unknown_rather_than_a_facet_of_its_own() -> None:
    """Absence from the catalogue is not sufficient on its own.

    An unrecognised roastertype falls through to slugify, so simply dropping
    these four would have moved the problem rather than fixed it: a file
    naming a thermocouple board would still get `phidget_2xrtd` as its
    machine facet, which is the exact thing the cleanup exists to stop.
    Caught by running the catalogue on Windows and reading the output.
    """
    for not_a_roaster in ("Phidget 2xRTD", "phidget databridge", "Plugin Roast"):
        assert normalize_machine_key(not_a_roaster)[0] == "unknown"
    # ...while an unknown string that merely starts with the same word is a
    # machine we have not heard of, not a board -- it keeps its own key.
    assert normalize_machine_key("Phidget Something Else")[0] == "phidget_something_else"


def test_block_sizes_are_pinned() -> None:
    assert len(ARTISAN_MACHINES) == 182     # 250 strings collapsed onto 182 machines
    assert len(RESEARCHED_MACHINES) == 156
    assert len(HOME_ROASTER_SUPPLEMENT) == 19
    assert len(MACHINES) == 357


def test_every_display_name_is_unique() -> None:
    """Two rows with the same label are indistinguishable in a picker. (Two
    rows sharing a *key* is fine and expected -- see the Kaleido editions.)
    """
    dupes = [d for d, n in Counter(m.display_name for m in MACHINES).items() if n > 1]
    assert not dupes, f"duplicate display names: {dupes}"


def test_noisy_strings_collapse_onto_the_machine_they_describe() -> None:
    """The single clearest case in the whole catalogue: five Artisan strings
    described one Coffed SR5, differing only in whether its fans were
    EBM-Papst or Honeywell and whether its panel was automatic or manual.
    A user searching for their SR5 had five buckets to choose from, four of
    which were wrong.
    """
    sr5 = find_by_roastertype("Coffed SR5 manual delta+ Honeywell")
    assert sr5 is not None
    assert sr5.display_name == "Coffed SR5"
    assert len(sr5.artisan_strings) == 5
    for variant in ("Coffed SR5 automatic", "Coffed SR5 manual",
                    "Coffed SR5 manual delta", "Coffed SR5 manual delta+ EBM-Papst"):
        assert find_by_roastertype(variant) is sr5


def test_a_brand_whose_every_string_is_electronics_says_so() -> None:
    """Diedrich's five Artisan strings name thermocouple counts and control
    boxes -- 4-Sensor, 6-Sensor, CR, DR. None is a model. Inventing one
    would be worse than admitting we don't know, so the catalogue carries a
    single honest entry, and the real Diedrich line (IR-1, IR-5, IR-12,
    DR-3...) lives in RESEARCHED_MACHINES for the picker to offer.
    """
    hit = find_by_roastertype("Diedrich 6-Sensor (Pre-2018)")
    assert hit is not None
    assert hit.display_name == "Diedrich (model unspecified)"
    assert hit.key == "diedrich", "the key must be the bare brand, not the label"
    assert len(hit.artisan_strings) == 5
    researched = {m.display_name for m in RESEARCHED_MACHINES}
    assert {"Diedrich IR-1", "Diedrich IR-12"} <= researched


def test_kaleido_admits_the_model_is_not_in_the_file() -> None:
    """Artisan's three Kaleido strings encode hardware generation and
    connection method. Capacity (M1..M10) and edition (Standard/Pro/Lite/
    Dual) are orthogonal to all three -- an M1 and an M10 bought the same
    year write the identical string. The catalogue must not pretend
    otherwise, and the display name is what a user reads.
    """
    for wire in ("Kaleido Serial", "Kaleido Network"):
        hit = find_by_roastertype(wire)
        assert hit is not None
        assert hit.key == "kaleido_serial"
        assert hit.display_name == "Kaleido (model unspecified)"
    assert find_by_roastertype("Kaleido Legacy").key == "kaleido_legacy"
    # ...while the real models are offered for a user to pick.
    models = {m.display_name for m in RESEARCHED_MACHINES}
    assert {"Kaleido Sniper M1", "Kaleido Sniper M10"} <= models


def test_researched_entries_carry_no_artisan_string() -> None:
    """They exist because Artisan has no string for them. If one ever
    acquired an artisan_strings entry it would start claiming a wire format
    it was never seen in, so this is worth pinning rather than assuming.
    """
    assert all(m.artisan_strings == () for m in RESEARCHED_MACHINES)


def test_a_current_manufacturer_spelling_matches_the_same_machine() -> None:
    """Kaffelogic's own site now sells the Nano 7 as the "Nano 7e". Both
    strings are in circulation at retailers; they are one machine."""
    assert find_by_roastertype("Kaffelogic Nano 7e") is find_by_roastertype("Kaffelogic Nano 7")


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
    # substring/heuristic fallback.
    assert find_by_roastertype("Kaleido Serial 2") is None


def test_find_by_roastertype_returns_none_for_unknown_empty_or_none() -> None:
    assert find_by_roastertype("not a real roaster at all") is None
    assert find_by_roastertype("") is None
    assert find_by_roastertype(None) is None  # type: ignore[arg-type]


def test_non_ascii_qsettings_escape_decoded_correctly() -> None:
    # The source .aset files spell this "B\xfchler" (QSettings' own escape
    # for a raw Latin-1 byte) -- it must have decoded to an actual "ü", not
    # stayed escaped or been mangled into something else.
    buhler = [m for m in ARTISAN_MACHINES if m.manufacturer == "Bühler"]
    assert buhler
    assert all("ü" in m.display_name for m in buhler)
    assert all("\\x" not in m.display_name for m in buhler)


def test_slugify_matches_documented_examples() -> None:
    assert slugify("Aillio Bullet R1") == "aillio_bullet_r1"
    assert slugify("  Multiple   Spaces -- Here!! ") == "multiple_spaces_here"
    assert slugify("") == "unknown"
    assert slugify("---") == "unknown"


def test_slugify_drops_a_trailing_plus_and_this_is_known() -> None:
    """Documented wart, pinned so it is a decision rather than a surprise:
    "Giesen WxA" and "Giesen WxA+" are different machines that share a key.
    Fixing it would re-key already-shipped entries.
    """
    assert slugify("Giesen WxA+") == slugify("Giesen WxA") == "giesen_wxa"


def test_list_machines_is_sorted_for_a_picker() -> None:
    listed = list_machines()
    assert len(listed) == len(MACHINES)
    keys = [(m.manufacturer.lower(), m.display_name.strip().lower()) for m in listed]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# The two invariants everything else rests on.
# ---------------------------------------------------------------------------

def test_no_two_machines_claim_the_same_string() -> None:
    """`find_by_roastertype` resolves a string to exactly one machine, and
    builds its index with `setdefault` -- so a string claimed twice would
    silently resolve to whichever entry happens to come first in the tuple,
    and the loser would be unreachable no matter what a user picked.
    """
    claims: dict[str, list[str]] = {}
    for machine in MACHINES:
        for string in machine.match_strings:
            claims.setdefault(string.strip().lower(), []).append(machine.display_name)
    contested = {s: names for s, names in claims.items() if len(names) > 1}
    assert not contested, f"strings claimed by more than one machine: {contested}"


def test_every_catalogue_key_is_the_key_ingest_would_actually_store() -> None:
    """The catalogue must not invent a second machine_key vocabulary.

    A picker offers `display_name` and stores its `key`; ingest derives a key
    from the .alog's own `roastertype`. If those two disagree, a user who
    picks their own machine in Settings matches none of their own roasts --
    which is the entire point of the machine facet. Regression: the catalogue
    advertised "aillio_bullet_r1" while every ingested Bullet roast is stored
    as "aillio_bullet", and `profile set --machine aillio_bullet` was rejected
    as an unknown machine despite being the only key the index contained.

    Checked over every string each entry claims, not just its display name --
    a collapsed entry answers to several, and each one has to land on the
    same key.
    """
    mismatched = [
        (string, machine.key, normalize_machine_key(string)[0])
        for machine in MACHINES
        for string in machine.match_strings
        if normalize_machine_key(string)[0] != machine.key
    ]
    assert not mismatched, (
        f"catalogue entries whose key differs from the key ingest would store: {mismatched}"
    )


def test_every_key_is_what_the_fallback_rules_would_derive() -> None:
    """The check above is weaker than it looks, and this one carries the
    weight it lost.

    normalize_machine_key now asks the catalogue *first*, so for any string
    the catalogue lists, "ingest agrees with the catalogue" is true by
    construction -- it is reading the key back out of the row it just found.
    Hand-editing a key to something wrong would sail straight through it.

    What still has content is that a key agrees with the rules that would
    have produced it had the catalogue never listed the string: slugify,
    plus the alias/Kaleido rules that own certain brands outright. That is
    the property the aillio_bullet_r1 regression actually violated.
    """
    wrong = [
        (m.display_name, m.key, effective_key(m.display_name))
        for m in MACHINES
        # "(model unspecified)" is a label for a brand with no model, not
        # part of any machine's name -- the key is the bare brand.
        if "unspecified" not in m.display_name
        and effective_key(m.display_name) != m.key
    ]
    assert not wrong, f"keys that the fallback rules would not produce: {wrong}"


def test_the_brands_with_pre_existing_keys_collapse_onto_them() -> None:
    """The specific rows the invariant above had to reconcile -- several
    models legitimately share one searchable key, with the precise model
    preserved in display_name (which is what users.machine_display holds).
    """
    by_display = {m.display_name: m.key for m in MACHINES}
    assert by_display["Aillio Bullet R1"] == "aillio_bullet"
    assert by_display["Aillio Bullet R2"] == "aillio_bullet"
    assert by_display["Hottop KN-8828B-2K+"] == "hottop"
    assert by_display["Behmor 1600 Plus"] == "behmor"
    assert by_display["Kaleido (model unspecified)"] == "kaleido_serial"
    # Four Kaleido M1 editions are one searchable machine, four pickable rows.
    m1 = [m for m in MACHINES if m.display_name.startswith("Kaleido Sniper M1 ")]
    assert len(m1) >= 2 and {m.key for m in m1} == {"kaleido_m1"}
    # ...while everything the alias rules never claimed keeps its own key.
    assert by_display["Giesen W6A"] == "giesen_w6a"


def test_effective_key_is_the_single_implementation_of_the_fallback_rules() -> None:
    """alog/machine.py imports these rather than restating them. If it ever
    grew its own copy, this is where the drift would show.
    """
    for text, expected in [("Kaleido Sniper M2 Lite", "kaleido_m2"),
                           ("Kaleido K3", "kaleido_k3"),
                           ("Kaleido Legacy", "kaleido_legacy"),
                           ("Some Hottop Variant", "hottop"),
                           ("Aillio Bullet R2 Pro", "aillio_bullet"),
                           ("Giesen W15A", "giesen_w15a")]:
        assert effective_key(text) == expected
        assert normalize_machine_key(text)[0] == expected
