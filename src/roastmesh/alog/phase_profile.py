"""Phase-relative (% of total roast time) milestone summary.

Raw absolute times/temperatures don't transfer across machines or batch
sizes, so this expresses everything as a percentage instead. All
percentages are relative to total CHARGE->DROP time.

The primary output is the industry-standard three-phase breakdown --
drying / Maillard / development: drying runs CHARGE->DRY_END (beans losing
free moisture, no flavor created but sets the thermal foundation), Maillard
runs DRY_END->FC_START (browning reactions), development runs
FC_START->DROP (aka DTR, Development Time Ratio -- most specialty roasters
target ~15-25%). This intentionally does NOT involve TP (turning point),
which isn't part of the standard 3-phase model -- a secondary, TP-inclusive
breakdown is computed separately below and doesn't gate the primary one.
"""
from __future__ import annotations

from roastmesh.models import Milestone


def _valid_or_none(pct: float | None, lower_bound: float) -> float | None:
    """pct if it falls in [lower_bound, 100], else None (out-of-order or
    out-of-range data -- some real files record e.g. FCs_time slightly
    *after* DROP_time -- must not silently produce a negative or >100%
    phase percentage)."""
    if pct is None:
        return None
    return pct if lower_bound <= pct <= 100.0 else None


def compute_phase_profile(milestones: list[Milestone]) -> dict[str, float] | None:
    by_name = {m.name: m for m in milestones}
    charge = by_name.get("CHARGE")
    drop = by_name.get("DROP")
    if charge is None or drop is None or drop.time_s is None:
        return None

    total_s = drop.time_s
    if not total_s or total_s <= 0:
        return None

    def pct_of_total(t: float | None) -> float | None:
        return (t / total_s * 100.0) if t is not None else None

    dry_end = by_name.get("DRY_END")
    fc_start = by_name.get("FC_START")
    tp = by_name.get("TP")

    profile: dict[str, float] = {"total_time_s": total_s}

    # --- Primary: drying / Maillard / development, independent of TP ---
    dry_pct = _valid_or_none(pct_of_total(dry_end.time_s) if dry_end else None, 0.0)
    fcs_pct = _valid_or_none(pct_of_total(fc_start.time_s) if fc_start else None, dry_pct or 0.0)

    if dry_pct is not None:
        profile["drying_pct"] = dry_pct
    if dry_pct is not None and fcs_pct is not None:
        profile["dry_end_to_fc_pct"] = fcs_pct - dry_pct  # aka "Maillard phase %"
    if fcs_pct is not None:
        development_pct = 100.0 - fcs_pct  # aka DTR, "Development phase %"
        profile["dtr_pct"] = development_pct
        profile["fc_to_drop_pct"] = development_pct

    # --- Secondary: TP-inclusive sub-split of the drying phase (optional,
    # finer-grained; validated independently so a bad TP can't suppress
    # the primary breakdown above) ---
    tp_pct = _valid_or_none(pct_of_total(tp.time_s) if tp else None, 0.0)
    if tp_pct is not None:
        profile["charge_to_tp_pct"] = tp_pct
        if dry_pct is not None and dry_pct >= tp_pct:
            profile["tp_to_dry_end_pct"] = dry_pct - tp_pct

    return profile
