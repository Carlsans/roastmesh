"""Machine identity and roast-level normalization.

Machine identity is tricky in practice: the .alog `roastertype` field is
often generic ("Kaleido Serial") but sometimes carries a real model token
("Kaleido M2", "Kaleido Sniper M2", "Kaleido Legacy") -- `normalize_machine_key`
extracts a model number when present instead of collapsing everything to one
generic "kaleido_serial" bucket. It never encodes sub-variants like "Lite"
vs. "Pro" though (no observed file has ever included that).
"""
from __future__ import annotations

import re

# roastertype substring (case-insensitive) -> (machine_key, mechanism_family, display_name)
# Seeded from what's actually observed in real corpora; extend as more
# roastertype strings are seen. First matching substring wins. Kaleido is
# handled separately below since its model token varies per file.
MACHINE_ALIASES: list[tuple[str, str, str, str]] = [
    ("hottop", "hottop", "hottop_drum", "Hottop"),
    ("behmor", "behmor", "behmor_drum", "Behmor"),
    ("bullet", "aillio_bullet", "aillio_fluidbed", "Aillio Bullet"),
    ("roaster scope", "unknown", "unknown", "Unspecified/generic profile"),
]

_KALEIDO_MODEL_RE = re.compile(r"\bm(\d+)\b")


def normalize_machine_key(roastertype: str | None) -> tuple[str, str, str]:
    """Map a raw roastertype string to (machine_key, mechanism_family, display_name)."""
    text = (roastertype or "").strip().lower()
    if not text:
        return "unknown", "unknown", "Unknown"

    if "kaleido" in text:
        model = _KALEIDO_MODEL_RE.search(text)
        if model:
            key = f"kaleido_m{model.group(1)}"
            return key, "kaleido_drum", f"Kaleido M{model.group(1)}"
        if "legacy" in text:
            return "kaleido_legacy", "kaleido_drum", "Kaleido Legacy"
        return "kaleido_serial", "kaleido_drum", "Kaleido (model unspecified)"

    for substring, machine_key, family, display in MACHINE_ALIASES:
        if substring in text:
            return machine_key, family, display
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug or "unknown", "unknown", roastertype or "Unknown"


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
