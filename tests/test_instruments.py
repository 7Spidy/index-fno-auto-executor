"""Tests for executor/instruments.py — 17-instrument universe."""

from __future__ import annotations

from executor.instruments import INDICES, STOCKS, ALL_INSTRUMENT_NAMES


def test_total_instrument_count_is_17():
    assert len(ALL_INSTRUMENT_NAMES) == 17


def test_no_duplicate_names():
    assert len(ALL_INSTRUMENT_NAMES) == len(set(ALL_INSTRUMENT_NAMES))


def test_indices_have_expected_lot_sizes():
    lot_sizes = {i["name"]: i["lot_size"] for i in INDICES}
    assert lot_sizes == {"NIFTY": 75, "BANKNIFTY": 15, "SENSEX": 20}


def test_sensex_is_bfo_others_nfo():
    exchanges = {i["name"]: i["fno_exchange"] for i in INDICES}
    assert exchanges["SENSEX"] == "BFO"
    assert exchanges["NIFTY"] == "NFO"
    assert exchanges["BANKNIFTY"] == "NFO"


def test_stocks_count_is_14():
    assert len(STOCKS) == 14


def test_every_stock_has_required_fields():
    required = {"name", "strike_step", "lot_size", "spot_exchange"}
    for s in STOCKS:
        assert required.issubset(s.keys())
