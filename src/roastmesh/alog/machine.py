"""Machine identity and roast-level normalization.

Machine identity is tricky in practice, and the .alog `roastertype` field is
a weaker signal than it looks. An audit of Artisan's own 273 machine strings
found only half name a machine at all: the rest name the PID, the wiring, or
the cable. "Kaleido Serial" is the clearest case -- it encodes hardware
generation and connection method, and is written identically by an M1 and an
M10, so the model genuinely cannot be recovered from the file. Across this
project's own .alog fixtures, most carry an empty `roastertype` outright.

So the order here is: ask the catalogue (which knows every literal string
Artisan writes, and which machine each one really describes), and only fall
back to pattern rules for a string the catalogue has never seen. The
catalogue owns those rules -- `effective_key`/`effective_family` are
imported, not restated, so the two cannot drift.
"""
from __future__ import annotations

from roastmesh.machines import (
    NOT_A_ROASTER,
    effective_family,
    effective_key,
    find_by_roastertype,
)

# Checked only for a roastertype string the catalogue does not list.
# (machine_key, mechanism_family, display_name) per substring; first wins.
# "roaster scope" is Artisan's own generic profile, not a machine.
MACHINE_ALIASES: list[tuple[str, str, str, str]] = [
    ("roaster scope", "unknown", "unknown", "Unspecified/generic profile"),
    ("hottop", "hottop", "hottop_drum", "Hottop"),
    ("behmor", "behmor", "behmor_drum", "Behmor"),
    ("bullet", "aillio_bullet", "aillio_fluidbed", "Aillio Bullet"),
]


def normalize_machine_key(roastertype: str | None) -> tuple[str, str, str]:
    """Map a raw roastertype string to (machine_key, mechanism_family, display_name)."""
    raw = (roastertype or "").strip()
    text = raw.lower()
    if not text:
        return "unknown", "unknown", "Unknown"

    # An interface board or a plugin is not a machine. Without this it would
    # slugify into a facet of its own ("phidget_2xrtd"), which is exactly the
    # bucket the catalogue was cleaned up to stop offering.
    if text in NOT_A_ROASTER:
        return "unknown", "unknown", "Unspecified/generic profile"

    machine = find_by_roastertype(raw)
    if machine is not None:
        return machine.key, machine.mechanism_family, machine.display_name

    # A Kaleido string the catalogue does not list -- a model token Artisan
    # itself never writes, but which a hand-edited or third-party file may.
    if "kaleido" in text:
        key = effective_key(raw)
        if key == "kaleido_legacy":
            display = "Kaleido Legacy (model unspecified)"
        elif key == "kaleido_serial":
            display = "Kaleido (model unspecified)"
        else:
            display = f"Kaleido {key.removeprefix('kaleido_').upper()}"
        return key, "kaleido_drum", display

    for substring, machine_key, family, display in MACHINE_ALIASES:
        if substring in text:
            return machine_key, family, display

    return effective_key(raw), effective_family(raw), raw or "Unknown"


# DROP bean-temperature bands (Celsius), following
# thecaptainscoffee.com/pages/roast-levels exactly: each cutoff below is
# that level's own printed upper bound on that page (City 205C, City+
# 207-210C, Full City 210-221C, Full City+ 218-224C, Vienna 221-227C,
# French 227-235C, Italian/Spanish over 235C) -- not a value derived from
# where the next level happens to start. That source's own ranges overlap
# between adjacent levels (typical of these guides -- roast level is a
# continuum, not a hard cutoff: Full City's 210-221C already overlaps
# Full City+'s 218-224C, which overlaps Vienna's 221-227C); checked in
# ascending order, a temperature in one of those overlaps resolves to
# whichever named level's own range starts lower. There is no band below
# "light" or above "italian/spanish" because the page doesn't define one --
# it starts at Cinnamon (196C) and light-roast territory below that isn't
# named on it at all, so "light" (this project's own pre-existing catch-all
# for "cooler than City", not one of the page's named levels) is kept as
# the floor rather than inventing a boundary the source doesn't state.
ROAST_LEVEL_BANDS_C: list[tuple[float, str]] = [
    (205.0, "light"),
    (207.0, "city"),
    (210.0, "city+"),
    (221.0, "full city"),
    (224.0, "full city+"),
    (227.0, "vienna"),
    (235.0, "french"),
    (float("inf"), "italian/spanish"),
]
