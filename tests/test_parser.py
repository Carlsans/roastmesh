from pathlib import Path

import pytest

from roastnet.alog.parser import AlogParseError, parse_alog, parse_alog_text

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.alog"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_parses_every_fixture_to_a_dict(path: Path) -> None:
    result = parse_alog(path)
    assert isinstance(result, dict)
    assert result  # non-empty


def test_rejects_non_literal_syntax() -> None:
    # A dict literal is fine...
    assert parse_alog_text("{'a': 1}") == {"a": 1}
    # ...but anything requiring evaluation (function calls, attribute
    # access, imports) must be rejected. This is the exact class of input
    # ast.literal_eval is chosen over eval()/pickle.load() to defend
    # against -- see ARCHITECTURE.md's SECURITY section.
    with pytest.raises(AlogParseError):
        parse_alog_text("{'a': __import__('os').system('echo pwned')}")
    with pytest.raises(AlogParseError):
        parse_alog_text("__import__('os').system('echo pwned')")


def test_rejects_non_dict_top_level() -> None:
    with pytest.raises(AlogParseError):
        parse_alog_text("[1, 2, 3]")


def test_rejects_garbage() -> None:
    with pytest.raises(AlogParseError):
        parse_alog_text("not even close to python")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AlogParseError):
        parse_alog(tmp_path / "does-not-exist.alog")
