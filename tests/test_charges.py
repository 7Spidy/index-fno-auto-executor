"""Tests for executor/charges.py — verbatim port of Repo 1's src/charges.py."""

from __future__ import annotations

import pytest

from executor import charges


def test_net_pnl_profit_direction_does_not_flip_sign():
    pnl = charges.net_pnl(entry=100.0, exit_price=120.0, lot_size=75, direction="CE")
    gross = (120.0 - 100.0) * 75
    assert pnl < gross  # charges reduce net vs gross
    assert pnl > 0


def test_net_pnl_pe_direction_same_formula_as_ce():
    ce_pnl = charges.net_pnl(entry=100.0, exit_price=120.0, lot_size=75, direction="CE")
    pe_pnl = charges.net_pnl(entry=100.0, exit_price=120.0, lot_size=75, direction="PE")
    assert ce_pnl == pe_pnl


def test_net_pnl_invalid_direction_raises():
    with pytest.raises(ValueError):
        charges.net_pnl(entry=100.0, exit_price=120.0, lot_size=75, direction="XX")


def test_net_pnl_loss_is_more_negative_than_gross():
    pnl = charges.net_pnl(entry=100.0, exit_price=80.0, lot_size=75, direction="CE")
    gross = (80.0 - 100.0) * 75
    assert pnl < gross
