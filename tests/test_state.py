"""Tests for executor/state.py — committed_premium()."""

from __future__ import annotations

from unittest.mock import patch

from executor import state


def test_committed_premium_no_position():
    with patch("executor.state.load_position", return_value=None):
        assert state.committed_premium(None) == 0.0


def test_committed_premium_open_position():
    pos = {"entry_premium": 120.5, "qty": 75}
    with patch("executor.state.load_position", return_value=pos):
        assert state.committed_premium(None) == 120.5 * 75
