"""Parsing for Artisan .alog roast profile files.

.alog files are NOT valid JSON despite superficially resembling it: they are
the Python repr() of a dict (single-quoted strings, capitalized True/False,
bare None). The only safe way to parse them is ast.literal_eval, which
evaluates literal Python data structures without executing arbitrary code.

This file corpus comes from strangers on a P2P network and is parsed on
every peer's machine -- never replace ast.literal_eval with eval() or
pickle.load() here, that turns the whole network into a remote code
execution pipeline. See ARCHITECTURE.md's SECURITY section.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


class AlogParseError(Exception):
    """Raised when a file cannot be parsed as an Artisan .alog literal dict."""


@dataclass
class SourceMeta:
    source_type: str  # "local" | "p2p" (p2p added once feed ingestion exists)
    source_ref: str
    source_url: str | None = None


def parse_alog_text(text: str) -> dict:
    """Parse the raw text of a .alog file into a plain dict.

    Uses ast.literal_eval (safe: only literals, no function calls/attribute
    access), never eval().
    """
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as exc:
        raise AlogParseError(f"could not parse as a Python literal: {exc}") from exc

    if not isinstance(value, dict):
        raise AlogParseError(f"expected a top-level dict, got {type(value).__name__}")

    return value


def parse_alog(path: Path) -> dict:
    """Read a .alog file from disk and parse it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    except OSError as exc:
        raise AlogParseError(f"could not read {path}: {exc}") from exc

    try:
        return parse_alog_text(text)
    except AlogParseError as exc:
        raise AlogParseError(f"{path}: {exc}") from exc
