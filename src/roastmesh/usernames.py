"""Deterministic, cosmetic default display names.

ARCHITECTURE.md:147-149: "Display names live in optional per-feed metadata
and are cosmetic only. Never trusted for uniqueness -- collisions are a
rendering problem, not a security one." `default_display_name` turns a
user's pubkey into a two-word coffee-flavoured name (adjective + noun) so
every known peer -- even one never synced with directly -- renders as
something more readable than a 64-character hex string, from the moment
it's first seen.

Deterministic beats random-at-first-run on purpose: the same pubkey must
render the same name on every machine with no network exchange at all, so
the mapping is pure -- given a pubkey, always the same two words, computed
locally. A name only ever changes when its owner deliberately edits their
own profile (see profile.py); this module never invents uniqueness, and the
UI is expected to show the pubkey's own short prefix alongside the name for
exactly that reason.
"""
from __future__ import annotations

import hashlib

# ~96 entries each, coffee/roasting-flavoured, no duplicates within a list.
# Order matters (it's part of the deterministic mapping) but is otherwise
# arbitrary -- append new words to the end if the list ever needs to grow,
# never reorder or remove an existing entry, or every already-known peer's
# name would change out from under it.
_ADJECTIVES: tuple[str, ...] = (
    'Amber', 'Toasted', 'Smoky', 'Nutty', 'Caramel', 'Chocolatey', 'Fruity', 'Floral',
    'Earthy', 'Bright', 'Bold', 'Mellow', 'Rich', 'Velvety', 'Silky', 'Syrupy',
    'Buttery', 'Spicy', 'Citrusy', 'Berry', 'Winey', 'Malty', 'Cocoa', 'Honeyed',
    'Roasted', 'Charred', 'Woody', 'Herbal', 'Grassy', 'Nectary', 'Sweet', 'Tangy',
    'Zesty', 'Smooth', 'Robust', 'Delicate', 'Vibrant', 'Sundrenched', 'Wild', 'Rustic',
    'Golden', 'Coppery', 'Bronzed', 'Umber', 'Sepia', 'Chestnut', 'Hazel', 'Cinnamon',
    'Clove', 'Nutmeg', 'Vanilla', 'Toffee', 'Molasses', 'Bruleed', 'Roasty', 'Snappy',
    'Crisp', 'Clean', 'Balanced', 'Juicy', 'Jammy', 'Ripe', 'Verdant', 'Blonde',
    'Dark', 'Deep', 'Misty', 'Dusky', 'Fragrant', 'Aromatic', 'Perfumed', 'Musky',
    'Peaty', 'Tarry', 'Resinous', 'Tannic', 'Tart', 'Sour', 'Funky', 'Fermented',
    'Boozy', 'Cordial', 'Praline', 'Marzipan', 'Fudgy', 'Creamy', 'Frothy', 'Foamy',
    'Silty', 'Grainy', 'Toasty', 'Baked', 'Sundried', 'Highland', 'Volcanic', 'Mineral',
)

_NOUNS: tuple[str, ...] = (
    'Bean', 'Roast', 'Crema', 'Aroma', 'Blend', 'Origin', 'Batch', 'Grind',
    'Brew', 'Cup', 'Pot', 'Press', 'Filter', 'Drip', 'Espresso', 'Latte',
    'Mocha', 'Macchiato', 'Cortado', 'Pourover', 'Chemex', 'Siphon', 'Percolator', 'Kettle',
    'Grinder', 'Roaster', 'Drum', 'Chaff', 'Husk', 'Cherry', 'Pulp', 'Parchment',
    'Silverskin', 'Peaberry', 'Varietal', 'Cultivar', 'Terroir', 'Altitude', 'Harvest', 'Farm',
    'Estate', 'Cooperative', 'Ferment', 'Cupping', 'Tasting', 'Palate', 'Finish', 'Body',
    'Acidity', 'Sweetness', 'Bitterness', 'Balance', 'Bloom', 'Degas', 'Extraction', 'Ratio',
    'Dose', 'Yield', 'Tamp', 'Portafilter', 'Basket', 'Burr', 'Hopper', 'Chamber',
    'Probe', 'Thermocouple', 'Curve', 'Milestone', 'Charge', 'Turnaround', 'Development', 'Crack',
    'Snap', 'Pop', 'Cooling', 'Tray', 'Sack', 'Silo', 'Warehouse', 'Cargo',
    'Freighter', 'Trader', 'Roastery', 'Cafe', 'Barista', 'Farmer', 'Picker', 'Sorter',
    'Taster', 'Blender', 'Merchant', 'Wholesaler', 'Exporter', 'Importer', 'Seedling', 'Sapling',
)


def default_display_name(pubkey_hex: str) -> str:
    """A deterministic "Adjective Noun" name derived from `pubkey_hex`.

    Hashed rather than indexed directly off `bytes.fromhex(pubkey_hex)`: it
    keeps this pure and total for *any* string input (an Ed25519 pubkey is
    always 32 bytes / 64 hex chars in practice, but this must never raise
    just because it was handed something shorter, oddly-cased, or not valid
    hex at all), while remaining exactly as deterministic -- the same input
    string always hashes to the same two words.
    """
    digest = hashlib.sha256(pubkey_hex.encode("utf-8")).digest()
    adjective = _ADJECTIVES[digest[0] % len(_ADJECTIVES)]
    noun = _NOUNS[digest[1] % len(_NOUNS)]
    return f"{adjective} {noun}"
