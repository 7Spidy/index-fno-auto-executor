"""Tests for executor/run.py's intra-minute trailing-SL sub-loop —
_run_trailing_subloop / _manage_open_positions_pass. Scoped to OPEN-position
management only: entry gates / try_enter must never be exercised here."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from executor import run
from executor.utils.calendar_nse import IST

_FAR_FUTURE_IST = IST.localize(datetime(2099, 12, 31, 23, 59, 59))


def _inst_cfg(name):
    return {"name": name, "fno_exchange": "NFO"}


@pytest.fixture(autouse=True)
def _patch_instruments():
    with patch("executor.config.INDICES", [_inst_cfg("NIFTY")]), \
         patch("executor.config.STOCKS", [_inst_cfg("RELIANCE")]):
        yield


def test_subloop_noop_when_EXEC_SUBLOOPS_is_1():
    """EXEC_SUBLOOPS=1 must fully revert to single-pass behaviour — no
    sub-loop passes, no sleeping."""
    with patch("executor.config.TRACKER_SUBLOOPS", 1), \
         patch("executor.run._manage_open_positions_pass") as mock_pass, \
         patch("executor.run.time.sleep") as mock_sleep:
        run._run_trailing_subloop(
            MagicMock(), MagicMock(), MagicMock(),
            past_squareoff=False, squareoff_time=_FAR_FUTURE_IST,
        )
    mock_pass.assert_not_called()
    mock_sleep.assert_not_called()


def test_subloop_skips_when_past_squareoff():
    with patch("executor.config.TRACKER_SUBLOOPS", 4), \
         patch("executor.run._manage_open_positions_pass") as mock_pass, \
         patch("executor.run.time.sleep") as mock_sleep:
        run._run_trailing_subloop(
            MagicMock(), MagicMock(), MagicMock(),
            past_squareoff=True, squareoff_time=_FAR_FUTURE_IST,
        )
    mock_pass.assert_not_called()
    mock_sleep.assert_not_called()


def test_subloop_stops_early_when_no_open_positions():
    r = MagicMock()
    with patch("executor.config.TRACKER_SUBLOOPS", 4), \
         patch("executor.state.load_position", return_value=None), \
         patch("executor.run._manage_open_positions_pass") as mock_pass, \
         patch("executor.run.time.sleep") as mock_sleep:
        run._run_trailing_subloop(
            r, MagicMock(), MagicMock(),
            past_squareoff=False, squareoff_time=_FAR_FUTURE_IST,
        )
    mock_pass.assert_not_called()
    mock_sleep.assert_not_called()


def test_subloop_runs_at_most_TRACKER_SUBLOOPS_minus_1_passes():
    r = MagicMock()
    open_pos = {"phase": "OPEN", "tradingsymbol": "NIFTYXXX"}
    with patch("executor.config.TRACKER_SUBLOOPS", 4), \
         patch("executor.config.TRACKER_SUBLOOP_SECS", 15.0), \
         patch("executor.config.TRACKER_JOB_BUDGET_SECS", 1000.0), \
         patch("executor.state.load_position", return_value=open_pos), \
         patch("executor.run.time.time", return_value=0.0), \
         patch("executor.run.time.sleep") as mock_sleep, \
         patch("executor.run._manage_open_positions_pass",
               return_value=[("NIFTY", "NFO")]) as mock_pass:
        run._run_trailing_subloop(
            r, MagicMock(), MagicMock(),
            past_squareoff=False, squareoff_time=_FAR_FUTURE_IST,
        )
    assert mock_pass.call_count == 3          # TRACKER_SUBLOOPS - 1
    assert mock_sleep.call_count == 3
    mock_sleep.assert_called_with(15.0)


def test_subloop_stops_early_when_budget_exhausted():
    r = MagicMock()
    open_pos = {"phase": "OPEN", "tradingsymbol": "NIFTYXXX"}
    # remaining budget (100 - 90 = 10s) is less than SUBLOOP_SECS (15s) —
    # must stop before the first pass.
    with patch("executor.config.TRACKER_SUBLOOPS", 4), \
         patch("executor.config.TRACKER_SUBLOOP_SECS", 15.0), \
         patch("executor.config.TRACKER_JOB_BUDGET_SECS", 100.0), \
         patch("executor.state.load_position", return_value=open_pos), \
         patch.dict("os.environ", {"EXEC_JOB_START_EPOCH": "0"}), \
         patch("executor.run.time.time", return_value=90.0), \
         patch("executor.run.time.sleep") as mock_sleep, \
         patch("executor.run._manage_open_positions_pass") as mock_pass:
        run._run_trailing_subloop(
            r, MagicMock(), MagicMock(),
            past_squareoff=False, squareoff_time=_FAR_FUTURE_IST,
        )
    mock_pass.assert_not_called()
    mock_sleep.assert_not_called()


def test_subloop_stops_when_positions_close_mid_loop():
    """If _manage_open_positions_pass reports no OPEN instruments remain
    (all closed/exited), further passes must not run."""
    r = MagicMock()
    open_pos = {"phase": "OPEN", "tradingsymbol": "NIFTYXXX"}
    with patch("executor.config.TRACKER_SUBLOOPS", 4), \
         patch("executor.config.TRACKER_SUBLOOP_SECS", 15.0), \
         patch("executor.config.TRACKER_JOB_BUDGET_SECS", 1000.0), \
         patch("executor.state.load_position", return_value=open_pos), \
         patch("executor.run.time.time", return_value=0.0), \
         patch("executor.run.time.sleep"), \
         patch("executor.run._manage_open_positions_pass", return_value=[]) as mock_pass:
        run._run_trailing_subloop(
            r, MagicMock(), MagicMock(),
            past_squareoff=False, squareoff_time=_FAR_FUTURE_IST,
        )
    assert mock_pass.call_count == 1   # stops after the first pass reports flat


# ── _manage_open_positions_pass — scope guard ────────────────────────────────

def test_manage_open_positions_pass_never_calls_entry_gate_or_try_enter():
    """Sub-loop passes must only touch OPEN-position management — never
    gates.check_all() or manager.try_enter()."""
    r = MagicMock()
    kite = MagicMock()
    kite.get_ltp.return_value = {"NFO:NIFTYXXX": 105.0}
    gateway = MagicMock()  # not a PaperGateway instance -> isinstance check False
    pos = {"phase": "OPEN", "tradingsymbol": "NIFTYXXX", "entry_premium": 100.0}

    with patch("executor.state.load_position", return_value=pos), \
         patch("executor.state.save_position"), \
         patch("executor.run._get_rsi_snapshot", return_value=None), \
         patch("executor.manager.manage_position") as mock_manage, \
         patch("executor.gates.check_all") as mock_gate, \
         patch("executor.manager.try_enter") as mock_try_enter:
        gateway.reconcile.return_value = pos
        still_open = run._manage_open_positions_pass(r, kite, gateway, [("NIFTY", "NFO")])

    mock_manage.assert_called_once()
    mock_gate.assert_not_called()
    mock_try_enter.assert_not_called()
    assert still_open == [("NIFTY", "NFO")]


def test_manage_open_positions_pass_drops_instrument_that_exits():
    r = MagicMock()
    kite = MagicMock()
    kite.get_ltp.return_value = {"NFO:NIFTYXXX": 80.0}
    gateway = MagicMock()
    open_pos = {"phase": "OPEN", "tradingsymbol": "NIFTYXXX", "entry_premium": 100.0}
    exiting_pos = {"phase": "EXITING", "tradingsymbol": "NIFTYXXX"}

    with patch("executor.state.load_position", side_effect=[open_pos, open_pos, exiting_pos]), \
         patch("executor.state.save_position"), \
         patch("executor.run._get_rsi_snapshot", return_value=None), \
         patch("executor.manager.manage_position"), \
         patch("executor.manager.check_exit_complete") as mock_check_exit, \
         patch("executor.run._journal_if_cooldown") as mock_journal:
        gateway.reconcile.return_value = open_pos
        still_open = run._manage_open_positions_pass(r, kite, gateway, [("NIFTY", "NFO")])

    mock_check_exit.assert_called_once()
    mock_journal.assert_called_once()
    assert still_open == []
