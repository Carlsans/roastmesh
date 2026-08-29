"""ResultsTable._sort_key: pure logic behind click-to-sort column headers.
A staticmethod, so this needs `import tkinter` (for the module to load at
all) but no live display -- no Tk() root is created here.

set_rows()/header-label tests further down DO need a live Treeview, so
they're individually Xvfb-guarded rather than gating the whole file --
the tests above this comment must keep working with no display at all."""
from __future__ import annotations

import os
import shutil

import pytest

pytest.importorskip("tkinter")

from roastmesh.gui import widgets
from roastmesh.gui.widgets import AutocompleteField, ResultsTable


class _FakeScreen:
    """A stand-in for a tk.Widget, for detect_ui_scale/resolve_ui_scale --
    these only ever call .winfo_screenwidth() on what's passed in, so a
    real display/Xvfb isn't needed to test the bucketing/precedence logic
    itself (unlike set_rows()/header-label tests further down, which do
    need a live Treeview)."""

    def __init__(self, width: int) -> None:
        self._width = width

    def winfo_screenwidth(self) -> int:
        return self._width


def test_detect_ui_scale_buckets_by_screen_width() -> None:
    assert widgets.detect_ui_scale(_FakeScreen(1920)) == 1.0  # 1080p laptop
    assert widgets.detect_ui_scale(_FakeScreen(1366)) == 1.0  # smaller laptop
    assert widgets.detect_ui_scale(_FakeScreen(2560)) == 1.6  # 1440p/QHD
    assert widgets.detect_ui_scale(_FakeScreen(3840)) == 3.0  # 4K


def test_resolve_ui_scale_precedence(monkeypatch) -> None:
    monkeypatch.delenv("ROASTMESH_UI_SCALE", raising=False)
    screen = _FakeScreen(1920)  # would detect to 1.0 on its own

    # configured (persisted) overrides detection
    assert widgets.resolve_ui_scale(screen, 2.0) == 2.0
    # missing/invalid configured value falls through to detection
    assert widgets.resolve_ui_scale(screen, None) == 1.0
    assert widgets.resolve_ui_scale(screen, 0) == 1.0

    # env var overrides everything, including an explicit configured value
    monkeypatch.setenv("ROASTMESH_UI_SCALE", "2.5")
    assert widgets.resolve_ui_scale(screen, 2.0) == 2.5

    # a garbage env value falls through rather than sticking
    monkeypatch.setenv("ROASTMESH_UI_SCALE", "not-a-number")
    assert widgets.resolve_ui_scale(screen, 2.0) == 2.0


def test_set_scale_clamps_and_derives_line_scale() -> None:
    widgets.set_scale(3.0)
    assert widgets.UI_SCALE == 3.0
    assert widgets.LINE_SCALE == 2.0  # the original, already-tuned 4K pair

    widgets.set_scale(1.0)
    assert widgets.UI_SCALE == 1.0
    assert widgets.LINE_SCALE == 1.0

    widgets.set_scale(999)  # clamps to MAX_UI_SCALE rather than accepting anything
    assert widgets.UI_SCALE == widgets.MAX_UI_SCALE

    widgets.set_scale(-5)  # clamps to MIN_UI_SCALE rather than going negative/zero
    assert widgets.UI_SCALE == widgets.MIN_UI_SCALE

    widgets.set_scale(1.0)  # leave global state as found for other tests


def test_sort_key_numeric_values_sort_as_numbers_not_text() -> None:
    # "10" must sort after "9", not before it the way plain string
    # comparison would put it (DTR %/Drop °C columns are numeric).
    values = ["9", "10", "2"]
    assert sorted(values, key=ResultsTable._sort_key) == ["2", "9", "10"]


def test_sort_key_blank_values_sort_after_numeric_ones() -> None:
    values = ["9", "", "2"]
    assert sorted(values, key=ResultsTable._sort_key) == ["2", "9", ""]


def test_sort_key_text_values_sort_case_insensitively() -> None:
    values = ["Beta", "alpha", "Charlie"]
    assert sorted(values, key=ResultsTable._sort_key) == ["alpha", "Beta", "Charlie"]


def test_sort_key_mixed_numeric_and_text_groups_numbers_first() -> None:
    values = ["hottop", "9", "kaleido", "2"]
    assert sorted(values, key=ResultsTable._sort_key) == ["2", "9", "hottop", "kaleido"]


def test_autocomplete_field_filter_values_is_case_insensitive_substring_match() -> None:
    values = ["kaleido_serial", "hottop", "aillio_bullet", "Kaleido_M2"]
    assert AutocompleteField._filter_values(values, "kal") == ["kaleido_serial", "Kaleido_M2"]
    assert AutocompleteField._filter_values(values, "TOP") == ["hottop"]


def test_autocomplete_field_filter_values_with_empty_typed_text_returns_everything() -> None:
    values = ["a", "b", "c"]
    assert AutocompleteField._filter_values(values, "") == values


def test_autocomplete_field_filter_values_with_no_match_returns_empty_list() -> None:
    assert AutocompleteField._filter_values(["hottop", "kaleido"], "zzz") == []


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY")) or shutil.which("Xvfb") is not None


_needs_display = pytest.mark.skipif(not _has_display(), reason="no X display and no Xvfb")


@_needs_display
def test_set_rows_shows_roast_date_and_converts_drop_temp_to_selected_unit() -> None:
    import tkinter as tk

    from roastmesh.gui.units import FAHRENHEIT

    root = tk.Tk()
    try:
        table = ResultsTable(root)
        table.set_rows(
            [{"roast_id": "r1", "title": "Test", "roast_date": "2025-07-09",
              "machine_key": "kaleido", "roast_type": "full city",
              "dtr_pct": 20.0, "drop_bt_c": 100.0, "beans_text": "Ethiopia"}],
            unit=FAHRENHEIT,
        )
        row_id = table.tree.get_children()[0]
        values = table.tree.item(row_id)["values"]
        assert "2025-07-09" in values  # roast_date column, not a filename
        # Tk's Treeview hands numeric-looking strings back as real numbers
        # via the Tcl bridge, so compare as text either way.
        assert str(values[list(table.tree["columns"]).index("drop_bt_c")]) == "212"  # 100C -> 212F
        assert table.tree.heading("drop_bt_c")["text"] == "Drop °F"
    finally:
        root.destroy()


@_needs_display
def test_sorting_preserves_the_unit_aware_drop_column_header() -> None:
    import tkinter as tk

    from roastmesh.gui.units import FAHRENHEIT

    root = tk.Tk()
    try:
        table = ResultsTable(root)
        table.set_rows(
            [{"roast_id": "r1", "title": "B", "dtr_pct": 10.0, "drop_bt_c": 200.0},
             {"roast_id": "r2", "title": "A", "dtr_pct": 20.0, "drop_bt_c": 210.0}],
            unit=FAHRENHEIT,
        )
        table._on_heading_click("title")  # sorting must not clobber the "Drop °F" relabel
        assert table.tree.heading("drop_bt_c")["text"] == "Drop °F"
    finally:
        root.destroy()
