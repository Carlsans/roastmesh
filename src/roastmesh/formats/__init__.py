"""Turn any supported roasting file's bytes into an Artisan-shaped dict.

roastmesh's whole downstream pipeline -- milestone extraction, phase profile,
RoR, roast-type classification, the GUI graph, every stat -- runs on the
normalized RoastRecord that `alog.record.to_roast_record` builds from an
Artisan-shaped dict (keys `timex`, `temp1`/ET, `temp2`/BT, `timeindex`, `mode`,
plus metadata). So supporting another format is just an *adapter* that maps its
bytes into that same dict; nothing after this point, and nothing in the P2P /
feed / replication layers, has to change.

Dispatch is by CONTENT, not extension: files arrive as content-addressed blobs
named `<hash>.alog` regardless of what produced them, so the extension carries
no information. Each adapter's `parse(raw_bytes)` returns the Artisan dict if it
recognizes the bytes, or None if they are not its format.

SECURITY (ARCHITECTURE.md, non-negotiable): a corpus of files from strangers is
parsed on every peer's machine, so every adapter uses a *safe* reader only --
`ast.literal_eval`, `json.loads`, `csv` -- never `eval` or `pickle`. Adapters
must not raise on hostile input; they return None and let the next one try.
"""
from __future__ import annotations

from roastmesh.formats import artisan_alog, csv_roast, roasttime


# File-name patterns worth *looking at* in a folder (watch-folder / `ingest`
# of a directory). Dispatch itself is by content, so this only decides which
# files to try, not how they're parsed.
SUPPORTED_GLOBS = ("*.alog", "*.json", "*.csv")


class RoastParseError(Exception):
    """No registered format could parse these bytes as a roast file."""


# Tried in order; first adapter whose parse() returns non-None wins. Artisan
# comes first (the primary format), then RoasTime JSON, then CSV -- and each
# adapter's parse() is discriminating (a .alog is never read as CSV, a RoasTime
# JSON is never read as an Artisan export), so the order only decides ties that
# in practice never occur.
_ADAPTERS = [artisan_alog, roasttime, csv_roast]


def detect_and_parse(raw_bytes: bytes) -> tuple[dict, str]:
    """Return (artisan_shaped_dict, format_name). Raises RoastParseError if no
    registered adapter recognizes the bytes."""
    for adapter in _ADAPTERS:
        try:
            result = adapter.parse(raw_bytes)
        except Exception:  # noqa: BLE001 -- a blow-up on hostile input means "not mine", not a crash
            result = None
        if result is not None:
            return result, adapter.FORMAT_NAME
    raise RoastParseError("unrecognized roast file format")
