"""Tests for executor/gates.py — dynamic-stock tradability bypass and the
direction_restriction safety check. Static-instrument behavior must be
unaffected by either change."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executor import gates


# ── _check_option_tradable — static-cache bypass for dynamic intents ────────

def test_check_option_tradable_static_intent_requires_cache_membership():
    intent = {"tradingsymbol": "NIFTY25710724500CE"}
    r = MagicMock()
    kite = MagicMock()
    with patch("executor.utils.auth.get_option_cache", return_value={}):
        ok, reason = gates._check_option_tradable(intent, r, kite, "NFO")
    assert ok is False
    assert "option token cache" in reason
    kite.get_ltp.assert_not_called()   # never reaches the LTP check


def test_check_option_tradable_static_intent_passes_when_in_cache_and_ltp_positive():
    intent = {"tradingsymbol": "NIFTY25710724500CE"}
    r = MagicMock()
    kite = MagicMock()
    kite.get_ltp.return_value = {"NFO:NIFTY25710724500CE": 120.5}
    with patch("executor.utils.auth.get_option_cache",
               return_value={"NIFTY25710724500CE": {"token": 1}}):
        ok, reason = gates._check_option_tradable(intent, r, kite, "NFO")
    assert ok is True


def test_check_option_tradable_dynamic_intent_skips_cache_membership_check():
    intent = {"tradingsymbol": "TATASTEEL25710722750PE", "is_dynamic": True}
    r = MagicMock()
    kite = MagicMock()
    kite.get_ltp.return_value = {"NFO:TATASTEEL25710722750PE": 15.0}
    with patch("executor.utils.auth.get_option_cache", return_value={}) as mock_cache:
        ok, reason = gates._check_option_tradable(intent, r, kite, "NFO")
    assert ok is True
    mock_cache.assert_not_called()   # static cache never consulted for dynamic


def test_check_option_tradable_dynamic_intent_still_requires_positive_ltp():
    """Live LTP check remains mandatory and unchanged for dynamic intents."""
    intent = {"tradingsymbol": "TATASTEEL25710722750PE", "is_dynamic": True}
    r = MagicMock()
    kite = MagicMock()
    kite.get_ltp.return_value = {"NFO:TATASTEEL25710722750PE": 0.0}
    ok, reason = gates._check_option_tradable(intent, r, kite, "NFO")
    assert ok is False
    assert "LTP is zero" in reason


def test_check_option_tradable_dynamic_intent_missing_tradingsymbol_fails():
    intent = {"is_dynamic": True}
    ok, reason = gates._check_option_tradable(intent, MagicMock(), MagicMock(), "NFO")
    assert ok is False
    assert "tradingsymbol missing" in reason


# ── check_all — direction_restriction safety check ──────────────────────────

@pytest.fixture
def _isolate_other_checks():
    """Force every other check_all() step to pass so only the
    direction_restriction check under test can fail the gate. Scoped only to
    the check_all() tests below — must NOT apply to the _check_option_tradable
    unit tests above, which exercise that function directly."""
    with patch("executor.state.get_kill_switch", return_value=False), \
         patch("executor.state.entries_blocked", return_value=False), \
         patch("executor.gates._check_cooldown", return_value=(True, "")), \
         patch("executor.gates._check_no_open_position", return_value=(True, "")), \
         patch("executor.gates._check_time", return_value=(True, "")), \
         patch("executor.gates._check_option_tradable", return_value=(True, "")):
        yield


def test_check_all_dynamic_ce_only_with_pe_direction_fails(_isolate_other_checks):
    intent = {
        "instrument": "RELIANCE", "is_dynamic": True,
        "direction_restriction": "CE_ONLY", "direction": "PE",
    }
    ok, reason = gates.check_all(intent, MagicMock(), MagicMock(), "NFO")
    assert ok is False
    assert reason == "dynamic direction_restriction violated"


def test_check_all_dynamic_pe_only_with_ce_direction_fails(_isolate_other_checks):
    intent = {
        "instrument": "TATASTEEL", "is_dynamic": True,
        "direction_restriction": "PE_ONLY", "direction": "CE",
    }
    ok, reason = gates.check_all(intent, MagicMock(), MagicMock(), "NFO")
    assert ok is False
    assert reason == "dynamic direction_restriction violated"


def test_check_all_dynamic_matching_direction_passes(_isolate_other_checks):
    intent = {
        "instrument": "RELIANCE", "is_dynamic": True,
        "direction_restriction": "CE_ONLY", "direction": "CE",
    }
    ok, reason = gates.check_all(intent, MagicMock(), MagicMock(), "NFO")
    assert ok is True


def test_check_all_static_intent_unaffected_by_direction_restriction_check(_isolate_other_checks):
    """Static intents never carry is_dynamic/direction_restriction — the new
    check must be a no-op for them."""
    intent = {"instrument": "NIFTY", "direction": "CE"}
    ok, reason = gates.check_all(intent, MagicMock(), MagicMock(), "NFO")
    assert ok is True
