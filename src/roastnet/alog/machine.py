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


# DROP bean-temperature bands (Celsius) mapped to common home-roasting
# roast-level names. There is no universal agreement on exact boundaries --
# one roaster's "Vienna" is another's "Full City+" -- these are a reasonable
# midpoint of published guides, used only as a last-resort fallback when no
# explicit roast-level text is available.
ROAST_LEVEL_BANDS_C: list[tuple[float, str]] = [
    (205.0, "light"),
    (215.0, "city"),
    (221.0, "city+"),
    (227.0, "full city"),
    (230.0, "full city+"),
    (238.0, "vienna"),
    (float("inf"), "french"),
]
