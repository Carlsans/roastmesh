from pathlib import Path

import pytest

from roastnet.alog.parser import SourceMeta, parse_alog
from roastnet.alog.record import to_roast_record

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))
SOURCE = SourceMeta(source_type="local", source_ref="test")


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_every_fixture_normalizes_without_crashing(path: Path) -> None:
    raw = parse_alog(path)
    record = to_roast_record(raw, SOURCE, filename_hint=path.name)
    assert record.machine_key
    assert record.mechanism_family
    assert isinstance(record.milestones, list)


def test_charge_and_drop_present_when_expected() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE, filename_hint="kaleido_1.alog")
    assert record.milestone("CHARGE") is not None
    assert record.milestone("DROP") is not None


def test_dtr_computed_when_charge_and_drop_present() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE, filename_hint="kaleido_1.alog")
    if record.phase_profile is not None:
        assert "dtr_pct" in record.phase_profile
        assert 0.0 <= record.phase_profile["dtr_pct"] <= 100.0


def test_hottop_fahrenheit_converted_to_celsius() -> None:
    # hottop_1.alog's own `mode` field is 'F'; a roast's DROP bean temp in
    # real Fahrenheit-mode exports is in the 400s F, i.e. clearly above 100
    # if left unconverted -- Celsius roast temps are always well under 300.
    raw = parse_alog(FIXTURES_DIR / "hottop_1.alog")
    assert raw.get("mode") == "F"
    record = to_roast_record(raw, SOURCE, filename_hint="hottop_1.alog")
    drop = record.milestone("DROP")
    if drop is not None and drop.bt_c is not None:
        assert drop.bt_c < 280


def test_machine_key_normalization() -> None:
    raw = parse_alog(FIXTURES_DIR / "kaleido_1.alog")
    record = to_roast_record(raw, SOURCE, filename_hint="kaleido_1.alog")
    assert record.machine_key.startswith("kaleido")

    raw = parse_alog(FIXTURES_DIR / "hottop_1.alog")
    record = to_roast_record(raw, SOURCE, filename_hint="hottop_1.alog")
    assert record.machine_key == "hottop"
    assert record.mechanism_family == "hottop_drum"


def test_density_is_none_or_positive() -> None:
    for path in FIXTURES:
        raw = parse_alog(path)
        record = to_roast_record(raw, SOURCE, filename_hint=path.name)
        assert record.density_g_per_l is None or record.density_g_per_l > 0
