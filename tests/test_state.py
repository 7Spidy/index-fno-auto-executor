"""Tests for executor/state.py — per-instrument namespacing, committed_premium()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from executor import state


def test_committed_premium_no_open_instruments():
    with patch("executor.state.list_open_instruments", return_value=[]):
        assert state.committed_premium(None) == 0.0


def test_committed_premium_sums_across_open_instruments():
    positions = {
        "NIFTY":      {"entry_premium": 120.5, "qty": 75},
        "ASIANPAINT": {"entry_premium": 40.0, "qty": 250},
    }
    with patch("executor.state.list_open_instruments", return_value=["NIFTY", "ASIANPAINT"]), \
         patch("executor.state.load_position", side_effect=lambda r, instrument: positions[instrument]):
        expected = 120.5 * 75 + 40.0 * 250
        assert state.committed_premium(None) == expected


def test_committed_premium_skips_instrument_with_no_position():
    with patch("executor.state.list_open_instruments", return_value=["NIFTY"]), \
         patch("executor.state.load_position", return_value=None):
        assert state.committed_premium(None) == 0.0


def test_load_position_uses_namespaced_key():
    r = MagicMock()
    r.get.return_value = None
    state.load_position(r, "banknifty")
    r.get.assert_called_once_with("executor:position:BANKNIFTY")


def test_save_position_writes_namespaced_key_and_updates_index():
    r = MagicMock()
    r.get.return_value = None   # empty open-instruments index
    state.save_position(r, "nifty", {"phase": "OPEN"})
    set_calls = [c.args[0] for c in r.set.call_args_list]
    assert "executor:position:NIFTY" in set_calls
    assert "executor:open_instruments" in set_calls


def test_delete_position_removes_namespaced_key_and_index_entry():
    r = MagicMock()
    r.get.return_value = b'["NIFTY", "SENSEX"]'
    state.delete_position(r, "nifty")
    r.delete.assert_called_once_with("executor:position:NIFTY")
    # SENSEX should remain in the saved index; NIFTY removed
    saved_index_call = [c for c in r.set.call_args_list if c.args[0] == "executor:open_instruments"]
    assert len(saved_index_call) == 1
    import json
    assert json.loads(saved_index_call[0].args[1]) == ["SENSEX"]


def test_last_signal_ts_is_per_instrument():
    r = MagicMock()
    r.get.return_value = None
    state.get_last_signal_ts(r, "niftybank_typo_ok")
    r.get.assert_called_once_with("executor:last_signal_ts:NIFTYBANK_TYPO_OK")
