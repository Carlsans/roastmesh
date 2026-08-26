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

from roastnet.gui.widgets import ResultsTable


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


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY")) or shutil.which("Xvfb") is not None


_needs_display = pytest.mark.skipif(not _has_display(), reason="no X display and no Xvfb")


@_needs_display
def test_set_rows_shows_roast_date_and_converts_drop_temp_to_selected_unit() -> None:
    import tkinter as tk

    from roastnet.gui.units import FAHRENHEIT

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

    from roastnet.gui.units import FAHRENHEIT

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
