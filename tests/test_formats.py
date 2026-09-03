"""Format registry + adapters: any supported roast file becomes an Artisan-shaped
dict, so the graph and every stat work for it exactly as for `.alog`.

The RoasTime and CSV fixtures are synthesized (no real export was on hand); the
`.alog` path is exercised against the real fixtures and must stay byte-identical.
"""
import ast
import glob
import json
from pathlib import Path

import pytest

from roastmesh import formats
from roastmesh.alog.parser import SourceMeta
from roastmesh.alog.record import to_roast_record
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_file

FIXTURES = Path(__file__).parent / "fixtures"
ALOGS = sorted(glob.glob(str(FIXTURES / "*.alog")))


def _names(rec):
    return {m.name: m.time_s for m in rec.milestones}


# --- dispatch / routing -----------------------------------------------------

def test_alog_routes_to_artisan_and_is_returned_unchanged():
    for path in ALOGS:
        raw = Path(path).read_bytes()
        d, fmt = formats.detect_and_parse(raw)
        assert fmt == "artisan"
        # The artisan adapter must return the dict byte-for-byte as
        # ast.literal_eval would -- backward compatibility for every .alog.
        assert d == ast.literal_eval(raw.decode("utf-8", "replace"))


def test_csv_routes_to_csv_adapter():
    raw = (FIXTURES / "formats" / "artisan_export.csv").read_bytes()
    d, fmt = formats.detect_and_parse(raw)
    assert fmt == "csv"
    assert d["timex"] and len(d["temp2"]) == len(d["timex"])


def test_roasttime_routes_to_roasttime_adapter():
    raw = (FIXTURES / "formats" / "roasttime_sample.json").read_bytes()
    d, fmt = formats.detect_and_parse(raw)
    assert fmt == "roasttime"
    assert d["roastertype"] == "Aillio Bullet"


# --- CSV: graph data + stats ------------------------------------------------

def test_csv_produces_milestones_phases_and_type():
    raw = (FIXTURES / "formats" / "artisan_export.csv").read_bytes()
    d, _ = formats.detect_and_parse(raw)
    rec = to_roast_record(d, SourceMeta("local", "x"))
    ms = _names(rec)
    assert ms["CHARGE"] == 0.0
    assert ms["DRY_END"] == 120.0     # 02:00 - 00:00
    assert ms["FC_START"] == 360.0    # 06:00
    assert ms["DROP"] == 450.0        # 07:30
    assert rec.phase_profile is not None and rec.phase_profile["dtr_pct"] is not None
    assert rec.roast_type is not None
    assert rec.timex_s and rec.bt_c  # curve is present for the graph


def test_csv_autodetects_fahrenheit_and_converts():
    # No unit in the header; BT peaks at 420 -> must be read as Fahrenheit.
    csv = (b"Time,BT,ET,Event\n"
           b"00:00,310,330,CHARGE\n04:00,330,360,DRY END\n"
           b"08:00,395,410,FCs\n10:00,420,430,DROP\n")
    d, fmt = formats.detect_and_parse(csv)
    assert fmt == "csv" and d["mode"] == "F"
    rec = to_roast_record(d, SourceMeta("local", "x"))
    drop_bt = next(m.bt_c for m in rec.milestones if m.name == "DROP")
    assert 210 < drop_bt < 220     # 420F ~= 215.6C, converted


def test_csv_teeth_check_event_row_maps_to_milestone_time():
    # Move DROP one row later -> its milestone time must move with it.
    base = ("Time,BT,Event\n00:00,200,CHARGE\n02:00,150,\n"
            "04:00,196,FCs\n06:00,205,{drop}\n08:00,210,{late}\n")
    early = base.format(drop="DROP", late="")
    late = base.format(drop="", late="DROP")
    d_e, _ = formats.detect_and_parse(early.encode())
    d_l, _ = formats.detect_and_parse(late.encode())
    r_e = to_roast_record(d_e, SourceMeta("local", "x"))
    r_l = to_roast_record(d_l, SourceMeta("local", "x"))
    assert _names(r_e)["DROP"] == 360.0
    assert _names(r_l)["DROP"] == 480.0


# --- RoasTime: graph data + stats -------------------------------------------

def test_roasttime_produces_milestones_machine_and_weights():
    raw = (FIXTURES / "formats" / "roasttime_sample.json").read_bytes()
    d, _ = formats.detect_and_parse(raw)
    rec = to_roast_record(d, SourceMeta("p2p", "pk:00000000"))
    ms = _names(rec)
    assert ms["CHARGE"] == 0.0
    assert ms["FC_START"] == 270.0     # index 9 * 30s
    assert ms["DROP"] == 300.0         # index 10 * 30s
    assert rec.machine_key == "aillio_bullet"
    assert rec.batch_weight_in_g == 100.0 and rec.batch_weight_out_g == 84.5
    assert rec.timex_s and rec.bt_c


def test_roasttime_accepts_object_arrays_and_explicit_time():
    obj = {
        "beanTemperature": [{"time": 0, "value": 200}, {"time": 60, "value": 150},
                            {"time": 120, "value": 196}, {"time": 180, "value": 205}],
        "firstCrackTime": 120, "dropTime": 180, "chargeTime": 0,
    }
    d, fmt = formats.detect_and_parse(json.dumps(obj).encode())
    assert fmt == "roasttime"
    rec = to_roast_record(d, SourceMeta("p2p", "x"))
    assert _names(rec)["FC_START"] == 120.0
    assert _names(rec)["DROP"] == 180.0


# --- security / robustness --------------------------------------------------

def test_garbage_raises_roastparseerror():
    with pytest.raises(formats.RoastParseError):
        formats.detect_and_parse(b"\x00\x01 not a roast file \xff\xfe")


def test_non_roast_json_is_not_misclaimed():
    # A JSON object with no bean-temperature array is not a roast we can read.
    with pytest.raises(formats.RoastParseError):
        formats.detect_and_parse(b'{"hello": "world", "n": 5}')


def test_alog_is_never_read_as_csv():
    for path in ALOGS:
        _, fmt = formats.detect_and_parse(Path(path).read_bytes())
        assert fmt == "artisan"


def test_oversized_json_array_is_refused_not_materialized():
    # A declared curve far beyond any real roast must be rejected, not looped over.
    huge = {"beanTemperature": list(range(600_000))}
    with pytest.raises(formats.RoastParseError):
        formats.detect_and_parse(json.dumps(huge).encode())


def test_adapters_never_raise_into_the_dispatcher():
    # Each adapter returns None (not an exception) on input that isn't its format.
    from roastmesh.formats import artisan_alog, csv_roast, roasttime
    junk = b"\xff\xfe\x00 random"
    assert artisan_alog.parse(junk) is None
    assert csv_roast.parse(junk) is None
    assert roasttime.parse(junk) is None


# --- end to end through ingest ---------------------------------------------

@pytest.mark.parametrize("fixture", ["artisan_export.csv", "roasttime_sample.json"])
def test_foreign_file_ingests_and_is_searchable_with_curve(tmp_path, fixture):
    conn = connect(tmp_path / "index.sqlite3")
    result = ingest_file(conn, FIXTURES / "formats" / fixture)
    assert result.error is None and result.record is not None
    from roastmesh.index import repository as repo
    rows = repo.search_roasts(conn)
    assert len(rows) == 1
    full = repo.load_full_record(conn, rows[0].roast_id)
    assert full["timex_s"] and full["bt_c"]          # graph data survived to the index
    assert any(m["name"] == "DROP" for m in full["milestones"])
    conn.close()
