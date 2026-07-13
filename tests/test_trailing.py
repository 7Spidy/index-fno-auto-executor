"""Tests for executor/trailing.py — Repo 1's ladder SL, ported verbatim."""

from __future__ import annotations

import pytest

from executor import trailing


def test_compute_ladder_sl_below_half_progress_keeps_prior_sl():
    # entry=100, T=20 -> at progress 0.4 (ltp=108), stays at prior_sl
    sl = trailing.compute_ladder_sl(entry=100, T=20, current_price=108, direction="CE", prior_sl=90)
    assert sl == 90


def test_compute_ladder_sl_first_rung():
    # progress = (110-100)/20 = 0.5 -> sl_fraction 0.25 -> sl = 100 + 0.25*20 = 105
    sl = trailing.compute_ladder_sl(entry=100, T=20, current_price=110, direction="CE", prior_sl=90)
    assert sl == 105


def test_compute_ladder_sl_never_loosens():
    # A later, lower progress query must not loosen an already-ratcheted SL.
    sl = trailing.compute_ladder_sl(entry=100, T=20, current_price=105, direction="CE", prior_sl=105)
    assert sl == 105


def test_compute_ladder_sl_pe_direction_mirrors_ce():
    # PE: progress = (entry - current_price) / T
    sl = trailing.compute_ladder_sl(entry=100, T=20, current_price=90, direction="PE", prior_sl=110)
    assert sl == 95  # 100 - 0.25*20


def test_compute_ladder_sl_invalid_direction_raises():
    with pytest.raises(ValueError):
        trailing.compute_ladder_sl(entry=100, T=20, current_price=110, direction="XX", prior_sl=90)


def test_compute_ladder_sl_zero_t_returns_prior_sl():
    sl = trailing.compute_ladder_sl(entry=100, T=0, current_price=110, direction="CE", prior_sl=90)
    assert sl == 90


def test_compute_ai_adjusted_sl_tightens_on_rsi_reversal():
    snapshot = {"rsi_last3": [70, 60, 50], "progress": 0.8, "current_price": 115, "T": 20}
    sl = trailing.compute_ai_adjusted_sl(ladder_sl=105, direction="CE", market_snapshot=snapshot)
    assert sl == max(115 - 0.05 * 20, 105)


def test_compute_ai_adjusted_sl_no_reversal_returns_ladder_sl():
    snapshot = {"rsi_last3": [50, 60, 70], "progress": 0.8, "current_price": 115, "T": 20}
    sl = trailing.compute_ai_adjusted_sl(ladder_sl=105, direction="CE", market_snapshot=snapshot)
    assert sl == 105


def test_compute_ai_adjusted_sl_low_progress_returns_ladder_sl():
    snapshot = {"rsi_last3": [70, 60, 50], "progress": 0.5, "current_price": 110, "T": 20}
    sl = trailing.compute_ai_adjusted_sl(ladder_sl=105, direction="CE", market_snapshot=snapshot)
    assert sl == 105


def test_compute_final_sl_ce_takes_max():
    assert trailing.compute_final_sl(ladder_sl=100, ai_sl=105, direction="CE") == 105


def test_compute_final_sl_pe_takes_min():
    assert trailing.compute_final_sl(ladder_sl=100, ai_sl=95, direction="PE") == 95
