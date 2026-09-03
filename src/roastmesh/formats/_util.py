"""Shared helpers for format adapters, in their own module so adapters don't
import from the package __init__ mid-initialization (circular import)."""
from __future__ import annotations


def decode_text(raw_bytes: bytes) -> str:
    """UTF-8, then Latin-1 -- the same fallback ingest.ingest_file and
    alog.parser use, since some real exports aren't UTF-8."""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("latin-1")
