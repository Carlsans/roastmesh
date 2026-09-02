#!/usr/bin/env python3
"""Dev tool: regenerate the ARTISAN_MACHINES block in roastmesh.machines
from a local Artisan checkout.

Artisan ships one `.aset` QSettings file per machine, under
`src/includes/Machines/<Manufacturer>/<Model>.aset`. Each carries a
`roastertype_setup=<string>` key somewhere in the file (usually under
`[General]`, but a handful of real files -- Craftsmith, Has Garanti, Hive
Roaster, Roastmax -- carry it under `[Device]` instead, so this searches the
whole file rather than gating on section) -- that string is exactly what
Artisan writes into a profile's `roastertype` field, so matching against it
is exact, not heuristic.

That string is *not*, however, a model name. An audit of all 254 of them
found only half name a machine at all; the rest name the PID brand, the
wiring harness, the cable, or -- in four cases -- hardware that is not a
roaster. So this tool does two things beyond extraction: it drops the
non-roasters, and it applies COLLAPSE below, which maps each string onto
the machine it actually describes. Five separate strings described one
Coffed SR5, differing only in whether its fans were EBM-Papst or Honeywell.

COLLAPSE is hand-curated and is the part that needs review on regeneration.
Deriving it automatically was tried and is not good enough: stripping known
controller vocabulary is a fine way to *find* the groups, but it produced
"Diedrich 4" and "North C" as machine names. A value of None means the
string names no machine at all, and its brand gets one honest
"(model unspecified)" entry instead of an invented model.

Usage:
    .venv/bin/python tools/build_machine_catalogue.py <path-to-Machines-dir>

Prints the generated Python source for ARTISAN_MACHINES to stdout (and a
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


COLLAPSE = {
 "ARC 800 RTD": "Arc 800",
 "ARC S RTD": "Arc S",
 "Atilla GOLD plus II Control": "Atilla GOLD plus II",
 "Atilla GOLD plus II Control Auto": "Atilla GOLD plus II",
 "Besca BSC auto": "Besca BSC", "Besca BSC full-auto": "Besca BSC",
 "Besca BSC manual v1": "Besca BSC", "Besca BSC manual v2": "Besca BSC",
 "Bühler RM 20 Simatic": "Bühler RM 20", "Bühler RM 20 Simatic Legacy": "Bühler RM 20",
 "Bühler RM 20 Playone": "Bühler RM 20",
 "Carmomaq Caloratto/Materattor Legacy": "Carmomaq Caloratto",
 "Coffed SR15 automatic": "Coffed SR15", "Coffed SR15 manual delta": "Coffed SR15",
 "Coffed SR3 manual": "Coffed SR3", "Coffed SR3 manual delta": "Coffed SR3",
 "Coffed SR3 manual delta+ EBM-Papst": "Coffed SR3",
 "Coffed SR3 manual delta+ Honeywell": "Coffed SR3",
 "Coffed SR5 automatic": "Coffed SR5", "Coffed SR5 manual": "Coffed SR5",
 "Coffed SR5 manual delta": "Coffed SR5",
 "Coffed SR5 manual delta+ EBM-Papst": "Coffed SR5",
 "Coffed SR5 manual delta+ Honeywell": "Coffed SR5",
 "CTE Silon USB": "CTE Silon", "CTE Silon Touch": "CTE Silon",
 "GR Automatic": "Golden Roasters GR", "GR Manual": "Golden Roasters GR",
 "GR Delta": "Golden Roasters GR", "GR Legacy": "Golden Roasters GR",
 "GR 2xEMKO": "Golden Roasters GR",
 "IMF RM Auto": "IMF RM", "IMF RM Control": "IMF RM", "IMF RM legacy": "IMF RM",
 "Probat G/UG control": "Probat G/UG", "Probat G/UG WebSockets": "Probat G/UG",
 "Kuban Supreme Automatic": "Kuban Supreme", "Kuban Supreme Manual": "Kuban Supreme",
 "Phoenix ORO PXF": "Phoenix ORO",
 "R&R R/RV Automatic": "R&R R/RV", "R&R R/RV Manual": "R&R R/RV",
 "Santoker Cube BT": "Santoker Cube", "Santoker Cube PID": "Santoker Cube",
 "Sivetz SRM legacy": "Sivetz SRM",
 "TRINITAS T2 legacy": "TRINITAS T2", "TRINITAS T7 legacy": "TRINITAS T7",
 "Toper TKM-SX Control": "Toper TKM-SX",
 "Wintop WS 2in1": "Wintop WS", "Wintop WS Fuji": "Wintop WS",
 "Santoker R Series BT": "Santoker R Series", "Santoker R Series USB": "Santoker R Series",
 "Santoker R Master Series BT": "Santoker R Master Series",
 "Santoker R Master Series WiFi": "Santoker R Master Series",
 "Santoker Q + X Series BT": "Santoker Q + X Series",
 "Santoker Q + X Series WiFi": "Santoker Q + X Series",
 "Cogen Series C v2": "Cogen Series C",
 "Besca Bee v2": "Besca Bee",
 "Easyster 3Temp": None, "Easyster AirPressure": None,
 "Proaster 3Temp": None, "Proaster AirPressure": None,
 "Diedrich 4-Sensor": None, "Diedrich 6-Sensor": None,
 "Diedrich 6-Sensor (Pre-2018)": None, "Diedrich CR": None, "Diedrich DR": None,
 "Nordic Delta DTA": None, "Nordic Delta DTK": None, "Nordic PLC": None,
 "North Standard Control Panel (Fotek)": None,
 "North Standard Control Panel (Fotek) C": None,
 "VNT Phidget": None, "VNT PID": None,
 "Berto Autonics Control": None, "Caparao PLC": None, "Joper PLC": None,
 "Kraffe PLC": None, "Pratter Autonics": None, "Pratter PLC": None,
 "Prisma PLC": None, "Prisma USB": None, "Schuilenburg PLC": None,
 "Sedona Elite PXF": None, "San Franciscan Eurotherm": None,
 "Petroncini ASEM": None, "Toper USB": None, "Hive Roaster Data Dome": "Hive Cascabel",
 "Loring Auto": None, "NOR Extension MODBUS": None, "Hottop TC4": None,
 "KapoK Inlet": None,
 # every MCR string names a control panel or its wiring, never a machine
 **{s: None for s in [
   "MCR Digital Control Panel 1000","MCR Digital Control Panel 1000 C",
   "MCR Digital Control Panel 1200 C","MCR Phidget",
   "MCR Phidget & Delta controls (port on the back)",
   "MCR Phidget & Delta controls (port on the right)",
   "MCR Phidget & Delta controls (port on the right) C",
   "MCR Phidget & Shihlin controls (port on the back)",
   "MCR Standard Control Panel (Delta)","MCR Standard Control Panel (Delta) C",
   "MCR Standard Control Panel (Fotek)","MCR Standard Control Panel (Fotek) C"]},
}
NOT_ROASTERS = {"Phidget 2xRTD", "Phidget 2xTC", "Phidget Databridge", "Plugin Roast"}


def build_catalogue(machines_dir: Path) -> list[tuple[str, str, str, tuple[str, ...]]]:
    """Returns (machine_key, display_name, manufacturer, artisan_strings)
    tuples, one per *machine* -- not one per string -- sorted for stable,
    readable output."""
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

    from collections import defaultdict

    from roastmesh.machines import effective_key

    groups: dict[str, dict] = defaultdict(lambda: {"man": "", "strings": []})
    unspecified: dict[str, list[str]] = defaultdict(list)
    for roastertype, (_key, manufacturer) in entries.items():
        if roastertype in NOT_ROASTERS:
            continue
        target = COLLAPSE.get(roastertype, roastertype)
        if target is None:
            unspecified[manufacturer].append(roastertype)
            continue
        group = groups[target]
        group["man"] = group["man"] or manufacturer
        group["strings"].append(roastertype)

    # A brand whose every string was electronics gets one honest entry --
    # joining its bare-brand row if it already has one, rather than adding a
    # second row with the same key.
    for manufacturer, strings in unspecified.items():
        bare = next((d for d in groups if d.lower() == manufacturer.lower()), None)
        display = bare or f"{manufacturer} (model unspecified)"
        groups[display]["man"] = manufacturer
        groups[display]["strings"].extend(strings)

    out = []
    for display, group in groups.items():
        # Never slugify blindly: the alias rules own the key for Hottop,
        # Behmor, Bullet and Kaleido, and "(model unspecified)" is a label,
        # not part of any machine's name.
        key = effective_key(display.replace(" (model unspecified)", ""))
        out.append((key, display, group["man"], tuple(sorted(set(group["strings"])))))
    return sorted(out, key=lambda e: (e[2].lower(), e[1].lower()))


def render(entries: list[tuple[str, str, str, tuple[str, ...]]]) -> str:
    lines = []
    for key, display_name, manufacturer, strings in entries:
        lines.append(f"    Machine({key!r}, {display_name!r}, {manufacturer!r}, "
                     f"artisan_strings={strings!r}),")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-Machines-dir>", file=sys.stderr)
        raise SystemExit(2)

    machines_dir = Path(sys.argv[1])
    entries = build_catalogue(machines_dir)
    manufacturers = {e[2] for e in entries}
    strings = sum(len(e[3]) for e in entries)
    print(
        f"# {strings} roastertype strings collapsed onto {len(entries)} machines "
        f"across {len(manufacturers)} manufacturers "
        f"({len(NOT_ROASTERS)} non-roasters dropped)",
        file=sys.stderr,
    )
    print(render(entries))


if __name__ == "__main__":
    main()
