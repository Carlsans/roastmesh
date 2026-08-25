"""Map a raw parsed .alog dict into a normalized RoastRecord."""
from __future__ import annotations

from roastnet.alog.events import extract_milestones
from roastnet.alog.machine import normalize_machine_key
from roastnet.alog.notes_tagger import tag_notes
from roastnet.alog.parser import SourceMeta
from roastnet.alog.phase_profile import compute_phase_profile
from roastnet.alog.roast_level import classify_roast_level, guess_roast_type_from_text
from roastnet.models import (
    Milestone,
    RoastRecord,
    density_to_g_per_l,
    temp_to_celsius,
    weight_to_grams,
)


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_density(raw: dict) -> float | None:
    # .alog's `density`/`density_roasted` fields are [value, weight_unit,
    # count, volume_unit]; prefer roasted (post-roast) density when present
    # since that's what a peer browsing profiles would want to compare, and
    # fall back to green-bean density otherwise.
    for key in ("density_roasted", "density"):
        entry = raw.get(key) or []
        if len(entry) >= 4:
            value = density_to_g_per_l(entry[0], entry[1], entry[2], entry[3])
            if value is not None:
                return value
    return None


def to_roast_record(
    raw: dict,
    source: SourceMeta,
    is_user_log: bool = False,
    roast_type_override: str | None = None,
    filename_hint: str | None = None,
) -> RoastRecord:
    warnings: list[str] = []

    weight = raw.get("weight") or []
    unit = weight[2] if len(weight) > 2 else "g"
    batch_in_g = weight_to_grams(weight[0], unit) if len(weight) > 0 else None
    batch_out_g = weight_to_grams(weight[1], unit) if len(weight) > 1 else None
    density = _extract_density(raw)

    roaster_type_raw = _clean(raw.get("roastertype"))
    machine_key, mechanism_family, _display = normalize_machine_key(roaster_type_raw)

    # Every .alog records its own temperature unit in `mode` ('F'/'C') --
    # some exports (e.g. Hottop) are Fahrenheit, others (e.g. Kaleido) are
    # Celsius. Everything temperature-related must be converted to Celsius
    # here, before it's stored, so cross-record comparison never silently
    # mixes units.
    mode = raw.get("mode")
    timex_s = list(raw.get("timex") or [])
    et_c = [temp_to_celsius(v, mode) for v in (raw.get("temp1") or [])]
    bt_c = [temp_to_celsius(v, mode) for v in (raw.get("temp2") or [])]
    if not timex_s:
        warnings.append("no timex array present")

    raw_milestones = extract_milestones(raw, warnings)
    milestones = [
        Milestone(name=m.name, time_s=m.time_s,
                  bt_c=temp_to_celsius(m.bt_c, mode), et_c=temp_to_celsius(m.et_c, mode))
        for m in raw_milestones
    ]
    phase_profile = compute_phase_profile(milestones)
    if phase_profile is None:
        warnings.append("could not compute phase_profile (missing CHARGE/DROP)")

    roasting_notes = _clean(raw.get("roastingnotes"))
    cupping_notes = _clean(raw.get("cuppingnotes"))
    beans_text = _clean(raw.get("beans"))

    # Precedence: an explicit level token in the filename/beans_text is
    # per-roast evidence and outranks a blanket override; the DROP-temperature
    # heuristic is the last resort for anything with neither signal.
    drop = next((m for m in milestones if m.name == "DROP"), None)
    roast_type = (
        guess_roast_type_from_text(filename_hint, beans_text)
        or _clean(roast_type_override)
        or classify_roast_level(drop.bt_c if drop else None)
    )

    return RoastRecord(
        roast_id=RoastRecord.new_roast_id(),
        source_type=source.source_type,
        source_ref=source.source_ref,
        source_url=source.source_url,
        fetched_at=RoastRecord.now(),
        roast_uuid=_clean(raw.get("roastUUID")),
        roaster_type_raw=roaster_type_raw,
        machine_key=machine_key,
        mechanism_family=mechanism_family,
        batch_weight_in_g=batch_in_g,
        batch_weight_out_g=batch_out_g,
        density_g_per_l=density,
        beans_text=beans_text,
        roast_date=_clean(raw.get("roastisodate") or raw.get("roastdate")),
        roast_epoch=raw.get("roastepoch"),
        roast_type=roast_type,
        timex_s=timex_s,
        bt_c=bt_c,
        et_c=et_c,
        milestones=milestones,
        phase_profile=phase_profile,
        roasting_notes=roasting_notes,
        cupping_notes=cupping_notes,
        note_tags=tag_notes(roasting_notes, cupping_notes),
        is_user_log=is_user_log,
        parse_warnings=warnings,
        extra_raw=raw,
    )
