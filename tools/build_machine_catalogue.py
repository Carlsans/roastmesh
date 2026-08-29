#!/usr/bin/env python3
"""Dev tool: regenerate the GENERATED_MACHINES block in roastmesh.machines
from a local Artisan checkout.

Artisan ships one `.aset` QSettings file per machine, under
`src/includes/Machines/<Manufacturer>/<Model>.aset`. Each carries a
`roastertype_setup=<string>` key somewhere in the file (usually under
`[General]`, but a handful of real files -- Craftsmith, Has Garanti, Hive
Roaster, Roastmax -- carry it under `[Device]` instead, so this searches the
whole file rather than gating on section) -- that string is exactly what
Artisan writes into a profile's `roastertype` field, so matching against it
is exact, not heuristic.

Usage:
    .venv/bin/python tools/build_machine_catalogue.py <path-to-Machines-dir>

Prints the generated Python source for GENERATED_MACHINES to stdout (and a
one-line summary to stderr). This script is a *dev-time* tool only -- its
output gets pasted into src/roastmesh/machines.py by hand and committed;
nothing at runtime reads an Artisan checkout or touches the network.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Same escaping QSettings itself uses when writing a non-ASCII value: each
# `\xNN` is one Latin-1 byte (e.g. `B\xfchler` -> `Bühler`).
_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def decode_qsettings_value(raw: str) -> str:
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), raw)


def extract_roastertype(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("roastertype_setup="):
            return decode_qsettings_value(stripped[len("roastertype_setup="):])
    return None


def slugify(text: str) -> str:
    """Must match alog/machine.py's existing fallback slugification exactly,
    so catalogue keys stay consistent with what an unrecognized roastertype
    string has always produced."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "unknown"


def build_catalogue(machines_dir: Path) -> list[tuple[str, str, str]]:
    """Returns (machine_key, roastertype_string, manufacturer) tuples, one
    per unique roastertype string, sorted for stable, readable output."""
    entries: dict[str, tuple[str, str]] = {}  # roastertype -> (key, manufacturer)
    for aset_path in sorted(machines_dir.glob("*/*.aset")):
        manufacturer = aset_path.parent.name
        text = aset_path.read_text(encoding="utf-8", errors="replace")
        roastertype = extract_roastertype(text)
        if not roastertype:
            continue
        # First file wins if two manufacturers' files happen to carry the
        # exact same roastertype string (observed for a few OEM-badged
        # controllers, e.g. "Probat G/UG" under both Probat and
        # Kirsch+Mausser) -- sorted() makes that deterministic.
        entries.setdefault(roastertype, (slugify(roastertype), manufacturer))

    return sorted(
        ((key, roastertype, manufacturer) for roastertype, (key, manufacturer) in entries.items()),
        key=lambda e: (e[2].lower(), e[1].lower()),
    )


def render(entries: list[tuple[str, str, str]]) -> str:
    lines = []
    for key, display_name, manufacturer in entries:
        lines.append(f"    Machine({key!r}, {display_name!r}, {manufacturer!r}),")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-Machines-dir>", file=sys.stderr)
        raise SystemExit(2)

    machines_dir = Path(sys.argv[1])
    entries = build_catalogue(machines_dir)
    manufacturers = {e[2] for e in entries}
    print(
        f"# {len(entries)} unique roastertype strings across {len(manufacturers)} manufacturers",
        file=sys.stderr,
    )
    print(render(entries))


if __name__ == "__main__":
    main()
