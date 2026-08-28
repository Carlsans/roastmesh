"""gui/i18n.py: translation lookup, plurals, language resolution, and a
coverage check that every t()/tn() key used in the GUI source actually has
a French entry (and that fr.json carries no stale keys) -- this is what
stops silent drift as the UI changes without someone updating fr.json.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from roastmesh.gui import i18n

GUI_DIR = Path(__file__).parent.parent / "src" / "roastmesh" / "gui"
FR_CATALOG_PATH = GUI_DIR / "locales" / "fr.json"


@pytest.fixture(autouse=True)
def _reset_language():
    # Every test starts from a clean slate and leaves one behind --
    # set_language mutates module-level state, and Task/other GUI tests in
    # the same session must not inherit whatever language a previous test
    # left active.
    i18n.set_language(i18n.DEFAULT_LANGUAGE)
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_t_returns_french_for_a_known_key() -> None:
    i18n.set_language("fr")
    assert i18n.t("Search") == "Rechercher"


def test_t_returns_the_english_key_unchanged_for_an_unknown_one() -> None:
    i18n.set_language("fr")
    assert i18n.t("This string is not in any catalog") == "This string is not in any catalog"


def test_t_in_english_is_the_identity_lookup() -> None:
    i18n.set_language("en")
    assert i18n.t("Search") == "Search"


def test_t_formats_kwargs_into_the_translated_template() -> None:
    i18n.set_language("fr")
    assert i18n.t("exited with code {code}", code=3) == "terminé avec le code 3"


def test_t_falls_back_to_english_formatting_on_a_bad_translation_placeholder(monkeypatch) -> None:
    # A translator typo (wrong/missing {placeholder}) must degrade to
    # correct English, never raise and take the app down with it.
    i18n.set_language("fr")
    monkeypatch.setitem(i18n._catalogs, "fr", {"Hello {name}": "Bonjour {nom}"})  # mismatched key
    assert i18n.t("Hello {name}", name="Ada") == "Hello Ada"


def test_tn_plural_selection_differs_by_language() -> None:
    # tn() injects `n` into the format kwargs itself -- a caller passing
    # n= again would collide with the positional `n` parameter.
    i18n.set_language("en")
    assert i18n.tn(0, "{n} result", "{n} results") == "0 results"
    assert i18n.tn(1, "{n} result", "{n} results") == "1 result"
    assert i18n.tn(2, "{n} result", "{n} results") == "2 results"

    i18n.set_language("fr")
    assert i18n.tn(0, "{n} result", "{n} results") == "0 résultat"
    assert i18n.tn(1, "{n} result", "{n} results") == "1 résultat"
    assert i18n.tn(2, "{n} result", "{n} results") == "2 résultats"


def test_resolve_language_precedence(monkeypatch) -> None:
    monkeypatch.delenv("ROASTMESH_LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.delenv("LANG", raising=False)

    # env > configured
    monkeypatch.setenv("ROASTMESH_LANG", "fr")
    assert i18n.resolve_language("en") == "fr"
    monkeypatch.delenv("ROASTMESH_LANG")

    # configured > OS locale
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert i18n.resolve_language("en") == "en"

    # configured missing/unknown -> OS locale
    assert i18n.resolve_language(None) == "fr"
    assert i18n.resolve_language("xx") == "fr"
    monkeypatch.delenv("LANG")

    # nothing recognized anywhere -> default
    assert i18n.resolve_language(None) == i18n.DEFAULT_LANGUAGE


def test_resolve_language_falls_through_an_unrecognized_value_at_each_step(monkeypatch) -> None:
    monkeypatch.setenv("ROASTMESH_LANG", "xx")  # unrecognized env
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert i18n.resolve_language("xx") == "fr"  # unrecognized configured value skipped too


def test_set_language_with_an_unknown_code_falls_back_to_default() -> None:
    i18n.set_language("xx")
    assert i18n.current_language() == i18n.DEFAULT_LANGUAGE


def test_set_language_with_a_malformed_catalog_falls_back_to_english(monkeypatch, capsys) -> None:
    monkeypatch.setitem(i18n.LANGUAGES, "zz", ("Zed", lambda n: n != 1))
    monkeypatch.setattr(
        i18n.resources, "files",
        lambda pkg: type("F", (), {"joinpath": lambda self, name: _BadPath()})(),
    )
    i18n._catalogs.pop("zz", None)
    i18n.set_language("zz")
    assert i18n.t("Search") == "Search"  # degrades to English, doesn't raise
    assert "could not load" in capsys.readouterr().err


class _BadPath:
    def read_text(self, encoding=None):
        raise json.JSONDecodeError("bad", "{", 0)


def test_fr_catalog_is_valid_json_and_covers_every_key_used_in_the_gui_source() -> None:
    used_keys = _collect_translation_keys()
    catalog = json.loads(FR_CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(catalog, dict)

    catalog_keys = set(catalog.keys())
    missing = used_keys - catalog_keys
    stale = catalog_keys - used_keys
    assert not missing, f"fr.json is missing translations for: {sorted(missing)}"
    assert not stale, f"fr.json has stale keys no longer used in the code: {sorted(stale)}"


#  widgets.py's _COLUMNS/_PEER_COLUMNS store (key, label, width) tuples at
# module level and translate the label at render time via t(label) --
# label there is a *variable*, not a string literal, so the generic
# call-site scan below can never see it. Confirmed the hard way: a real
# missing-translation bug (Title/Beans/DTR %/Pubkey/Last seen/Via silently
# falling back to English) shipped past the call-site-only scan and was
# only caught by actually looking at a screenshot. "drop_bt_c"'s own label
# ("Drop °C") is deliberately excluded -- ResultsTable._column_label
# never passes that exact string through t(); it looks up "Drop" alone and
# appends the live unit, so "Drop °C" itself is never a real key.
_COLUMN_TUPLE_NAMES = {"_COLUMNS", "_PEER_COLUMNS"}
_COLUMN_LABELS_NEVER_LOOKED_UP_VERBATIM = {"Drop °C"}


def _collect_translation_keys() -> set[str]:
    """Every string literal passed as the key argument to t()/tn() anywhere
    under src/roastmesh/gui/*.py, plus the column-header labels in
    widgets.py's _COLUMNS/_PEER_COLUMNS (see note above)."""
    keys: set[str] = set()
    for path in GUI_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _assigns_to_any(node, _COLUMN_TUPLE_NAMES):
                keys |= _column_labels(node.value)
                continue
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id == "t" and node.args:
                literal = _string_literal(node.args[0])
                if literal is not None:
                    keys.add(literal)
            elif node.func.id == "tn" and len(node.args) >= 3:
                for arg in node.args[1:3]:
                    literal = _string_literal(arg)
                    if literal is not None:
                        keys.add(literal)
    return keys


def _assigns_to_any(node: ast.Assign, names: set[str]) -> bool:
    return any(isinstance(target, ast.Name) and target.id in names for target in node.targets)


def _column_labels(list_node: ast.expr) -> set[str]:
    labels: set[str] = set()
    if not isinstance(list_node, ast.List):
        return labels
    for element in list_node.elts:
        if isinstance(element, ast.Tuple) and len(element.elts) >= 2:
            label = _string_literal(element.elts[1])
            if label is not None and label not in _COLUMN_LABELS_NEVER_LOOKED_UP_VERBATIM:
                labels.add(label)
    return labels


def _string_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
