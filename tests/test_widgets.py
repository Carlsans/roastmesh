"""ResultsTable._sort_key: pure logic behind click-to-sort column headers.
A staticmethod, so this needs `import tkinter` (for the module to load at
all) but no live display -- no Tk() root is created here."""
from __future__ import annotations

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
