from pathlib import Path

import pytest

from roastmesh.alog.parser import SourceMeta, parse_alog
from roastmesh.alog.record import to_roast_record

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))
SOURCE = SourceMeta(source_type="local", source_ref="test")


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_every_fixture_normalizes_without_crashing(path: Path) -> None:
    raw = parse_alog(path)
    record = to_roast_record(raw, SOURCE)
    assert record.machine_key
    assert record.mechanism_family
    assert isinstance(record.milestones, list)


def test_title_extracted_from_raw_alog_dict() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE)
    assert record.title == raw["title"]


def test_title_is_none_when_absent_from_the_raw_dict() -> None:
    raw = dict(parse_alog(FIXTURES_DIR / "kaleido_1.alog"))
    del raw["title"]
    record = to_roast_record(raw, SOURCE)
    assert record.title is None


def test_roast_type_ignores_an_explicit_note_and_uses_peak_temperature_instead() -> None:
    # roast_type must always come from peak bean temperature -- a note in
    # beans_text claiming a level wildly inconsistent with the actual
    # temperature data (confirmed as a real, confusing case: a roast that
    # peaked at 196C, unambiguously "light" on any standard chart, showing
    # "full city+" because of an old note) must NOT be trusted over it.
    raw = dict(parse_alog(FIXTURES_DIR / "kaleido_1.alog"))
    raw["beans"] = "Some beans\nRoast: Full City+\n"
    record = to_roast_record(raw, SOURCE)
    assert record.roast_type != "full city+"


def test_roast_type_uses_peak_temperature_not_just_the_drop_milestones_own_value() -> None:
    # Probe thermal lag can mean BT keeps climbing briefly after DROP is
    # marked -- the true peak (used for classify_roast_level) should be
    # the array's actual maximum, not necessarily the DROP milestone's own
    # recorded value.
    raw = dict(parse_alog(FIXTURES_DIR / "kaleido_1.alog"))
    mode = raw.get("mode")
    assert mode == "C"  # so raw temp2 values need no unit conversion here
    raw["temp2"] = list(raw["temp2"])
    raw["temp2"][-1] = 300.0  # an implausible but unambiguous peak, past every DROP milestone
    record = to_roast_record(raw, SOURCE)
    assert record.roast_type == "italian/spanish"


def test_roast_type_is_none_with_no_temperature_data_at_all() -> None:
    raw = dict(parse_alog(FIXTURES_DIR / "kaleido_1.alog"))
    raw["temp1"] = []
    raw["temp2"] = []
    record = to_roast_record(raw, SOURCE)
    assert record.roast_type is None


def test_charge_and_drop_present_when_expected() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE)
    assert record.milestone("CHARGE") is not None
    assert record.milestone("DROP") is not None


def test_dtr_computed_when_charge_and_drop_present() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE)
    if record.phase_profile is not None:
        assert "dtr_pct" in record.phase_profile
        assert 0.0 <= record.phase_profile["dtr_pct"] <= 100.0


def test_hottop_fahrenheit_converted_to_celsius() -> None:
    # hottop_1.alog's own `mode` field is 'F'; a roast's DROP bean temp in
    # real Fahrenheit-mode exports is in the 400s F, i.e. clearly above 100
    # if left unconverted -- Celsius roast temps are always well under 300.
    raw = parse_alog(FIXTURES_DIR / "hottop_1.alog")
    assert raw.get("mode") == "F"
    record = to_roast_record(raw, SOURCE)
    drop = record.milestone("DROP")
    if drop is not None and drop.bt_c is not None:
        assert drop.bt_c < 280


def test_machine_key_normalization() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE)
    assert record.machine_key.startswith("kaleido")

    raw = parse_alog(FIXTURES_DIR / "hottop_1.alog")
    record = to_roast_record(raw, SOURCE)
    assert record.machine_key == "hottop"
    assert record.mechanism_family == "hottop_drum"


# Ground truth captured from this project's already-shipped behavior for
# every real-world fixture on hand, BEFORE roastmesh.machines' ~254-entry
# Artisan catalogue lookup was added to normalize_machine_key. Every one of
# these must still hold afterwards -- see the regression test below.
_EXPECTED_MACHINE_KEYS = {
    "alexzhu_1.alog": ("unknown", "unknown"),
    "beastroaster_1.alog": ("unknown", "unknown"),
    "hottop_1.alog": ("hottop", "hottop_drum"),
    "hottop_2.alog": ("hottop", "hottop_drum"),
    "kaleido_1.alog": ("kaleido_serial", "kaleido_drum"),
    "kaleido_2.alog": ("kaleido_serial", "kaleido_drum"),
    "kaleido_3.alog": ("kaleido_serial", "kaleido_drum"),
    "philstyle_1.alog": ("unknown", "unknown"),
    "philstyle_2.alog": ("unknown", "unknown"),
}


def test_catalogue_lookup_does_not_change_any_existing_fixtures_machine_key() -> None:
    """The hard constraint on adding roastmesh.machines' catalogue lookup
    ahead of the Kaleido special case and MACHINE_ALIASES: it must not
    rename a single already-shipped key. Artisan's own catalogue happens to
    contain the literal strings "Kaleido Serial"/"Network"/"Legacy" and, for
    Hottop's KN-8828B-2K+ model, the exact string "Hottop 2K+" that these
    fixtures themselves carry -- a naive catalogue-first exact match would
    slugify "Hottop 2K+" to "hottop_2k" (not "hottop"), breaking every
    already-ingested Hottop roast's machine_key. This pins every one of the
    9 real-world fixtures on hand to its already-shipped
    (machine_key, mechanism_family) pair.
    """
    assert set(_EXPECTED_MACHINE_KEYS) == {p.name for p in FIXTURES}, "fixtures dir changed -- update this table"
    for path in FIXTURES:
        raw = parse_alog(path)
        record = to_roast_record(raw, SOURCE)
        expected = _EXPECTED_MACHINE_KEYS[path.name]
        assert (record.machine_key, record.mechanism_family) == expected, path.name


def test_catalogue_lookup_resolves_a_machine_the_old_aliases_never_knew() -> None:
    """A roastertype string the pre-catalogue logic (Kaleido special case +
    the 4-entry MACHINE_ALIASES + a generic slugify fallback) had never
    heard of, and would have collapsed to a bare slug with no real
    manufacturer/display info -- the new catalogue lookup should now
    recognize it exactly, since it's one of Artisan's own ~254 strings and
    isn't in the excluded (kaleido/hottop/behmor/bullet/roaster scope)
    conflict set."""
    from roastmesh.alog.machine import normalize_machine_key

    key, family, display = normalize_machine_key("Giesen W6A")
    assert key == "giesen_w6a"
    assert display == "Giesen W6A"
    # No trustworthy drum/fluidbed data for a catalogue-only match --
    # mechanism_family must not be invented.
    assert family == "unknown"

    # Case-insensitive, same as roastmesh.machines.find_by_roastertype.
    assert normalize_machine_key("giesen w6a")[0] == "giesen_w6a"


def test_density_is_none_or_positive() -> None:
    for path in FIXTURES:
        raw = parse_alog(path)
        record = to_roast_record(raw, SOURCE)
        assert record.density_g_per_l is None or record.density_g_per_l > 0
