"""Tests for executor/sizing.py — fixed-lot, capital-availability model."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executor import sizing


LOT_SIZE = 75


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("executor.sizing.get_lot_size", return_value=LOT_SIZE):
        yield


@pytest.fixture
def redis_mock():
    return MagicMock()


def test_compute_qty_paper_mode_within_capital(redis_mock):
    with patch("executor.state.committed_premium", return_value=0.0), \
         patch("executor.config.CAPITAL_RS", 1_00_000):
        qty = sizing.compute_qty(redis_mock, "NIFTYTEST", entry_ltp=100.0, paper_mode=True)
    assert qty == LOT_SIZE


def test_compute_qty_paper_mode_exceeds_capital(redis_mock):
    with patch("executor.state.committed_premium", return_value=0.0), \
         patch("executor.config.CAPITAL_RS", 1000):
        qty = sizing.compute_qty(redis_mock, "NIFTYTEST", entry_ltp=100.0, paper_mode=True)
    assert qty == 0


def test_compute_qty_live_mode_uses_kite_margins(redis_mock):
    kite = MagicMock()
    kite.get_margins.return_value = 1_00_000.0
    with patch("executor.state.committed_premium", return_value=0.0):
        qty = sizing.compute_qty(
            redis_mock, "NIFTYTEST", entry_ltp=100.0, paper_mode=False, kite=kite,
        )
    kite.get_margins.assert_called_once_with(redis_mock)
    assert qty == LOT_SIZE


def test_compute_qty_live_mode_no_kite_client_skips(redis_mock):
    qty = sizing.compute_qty(
        redis_mock, "NIFTYTEST", entry_ltp=100.0, paper_mode=False, kite=None,
    )
    assert qty == 0


def test_get_daily_loss_limit_paper_mode():
    with patch("executor.config.CAPITAL_RS", 1_00_000), \
         patch("executor.config.DAILY_LOSS_PCT", 0.15):
        limit = sizing.get_daily_loss_limit(paper_mode=True)
    assert limit == -(1_00_000 * 0.15)


def test_get_daily_loss_limit_live_mode_uses_kite_margins():
    kite = MagicMock()
    kite.get_margins.return_value = 50_000.0
    with patch("executor.config.DAILY_LOSS_PCT", 0.15):
        limit = sizing.get_daily_loss_limit(paper_mode=False, kite=kite)
    assert limit == -(50_000.0 * 0.15)


def test_get_daily_loss_limit_live_mode_no_kite_falls_back():
    with patch("executor.config.CAPITAL_RS", 1_00_000), \
         patch("executor.config.DAILY_LOSS_PCT", 0.15):
        limit = sizing.get_daily_loss_limit(paper_mode=False, kite=None)
    assert limit == -(1_00_000 * 0.15)
