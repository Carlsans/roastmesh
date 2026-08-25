"""classify_roast_level's DROP-temperature bands, which had zero test
coverage before this -- exactly how a real misclassification (212C and
217C both landing below Full City) shipped and went unnoticed until a
user checked their own roasts against a published reference
(thecaptainscoffee.com/pages/roast-levels). Boundaries here follow that
source; see machine.ROAST_LEVEL_BANDS_C's own comment for the reasoning.
"""
from __future__ import annotations

from roastnet.alog.roast_level import classify_roast_level


def test_classify_roast_level_returns_none_when_drop_temp_is_none() -> None:
    assert classify_roast_level(None) is None


def test_classify_roast_level_light() -> None:
    assert classify_roast_level(200.0) == "light"
    assert classify_roast_level(205.0) == "light"


def test_classify_roast_level_city() -> None:
    assert classify_roast_level(206.0) == "city"
    assert classify_roast_level(207.0) == "city"


def test_classify_roast_level_city_plus() -> None:
    assert classify_roast_level(208.0) == "city+"
    assert classify_roast_level(210.0) == "city+"


def test_classify_roast_level_full_city() -> None:
    # The real bug: these two temperatures, both real DROP points from a
    # user's own roasts, were misclassified as "city"/"city+" before the
    # bands were corrected against the published reference -- and the
    # first correction still used the wrong number for this band's own
    # cutoff (218, derived from where "Full City+" starts) instead of the
    # page's own stated upper bound for Full City itself (221).
    assert classify_roast_level(212.0) == "full city"
    assert classify_roast_level(217.0) == "full city"
    assert classify_roast_level(211.0) == "full city"
    assert classify_roast_level(221.0) == "full city"


def test_classify_roast_level_full_city_plus() -> None:
    assert classify_roast_level(222.0) == "full city+"
    assert classify_roast_level(224.0) == "full city+"


def test_classify_roast_level_vienna() -> None:
    assert classify_roast_level(225.0) == "vienna"
    assert classify_roast_level(227.0) == "vienna"


def test_classify_roast_level_french() -> None:
    assert classify_roast_level(228.0) == "french"
    assert classify_roast_level(235.0) == "french"


def test_classify_roast_level_italian_spanish() -> None:
    assert classify_roast_level(236.0) == "italian/spanish"
    assert classify_roast_level(260.0) == "italian/spanish"


def test_classify_roast_level_bands_are_contiguous_and_ascending() -> None:
    from roastnet.alog.machine import ROAST_LEVEL_BANDS_C

    thresholds = [band[0] for band in ROAST_LEVEL_BANDS_C]
    assert thresholds == sorted(thresholds)
    assert len(set(thresholds)) == len(thresholds)  # no duplicate cutoffs
