"""models.py: small pure-logic helpers with no existing dedicated test file."""
from __future__ import annotations

from roastmesh.models import weight_loss_pct


def test_weight_loss_pct_basic() -> None:
    assert weight_loss_pct(350.0, 297.5) == 15.0


def test_weight_loss_pct_none_when_either_weight_missing() -> None:
    assert weight_loss_pct(None, 297.5) is None
    assert weight_loss_pct(350.0, None) is None
    assert weight_loss_pct(None, None) is None


def test_weight_loss_pct_none_when_batch_in_is_zero() -> None:
    assert weight_loss_pct(0.0, 0.0) is None


def test_weight_loss_pct_allows_zero_batch_out() -> None:
    assert weight_loss_pct(350.0, 0.0) == 100.0
