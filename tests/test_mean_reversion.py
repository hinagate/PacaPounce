"""Frozen-policy tests for the second Paper strategy."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from veto import mean_reversion as mr

ET = ZoneInfo("America/New_York")


def test_committed_validation_card_is_paper_only_and_order_free():
    path = mr.config.ROOT / "data" / "ndx30_mr_validation.json"
    card = json.loads(path.read_text(encoding="utf-8"))

    assert card["status"] == "PAPER_STAGING"
    assert card["order_calls"] == 0
    assert card["oos_2024"]["trades"] == 146
    assert card["oos_2024"]["profit_factor"] >= 1.35
    assert card["screen_2023"]["strict_card_passed"] is False


def test_wilder_rsi_and_position_size_are_deterministic(monkeypatch):
    monkeypatch.setattr(mr.config, "STOCK_MR_EQUITY_RISK_PCT", 0.005)
    monkeypatch.setattr(mr.config, "STOCK_MR_MAX_NOTIONAL_PCT", 0.20)
    monkeypatch.setattr(mr.config, "STOCK_MR_STOP_ATR_MULTIPLE", 2.0)

    assert mr.wilder_rsi([10, 9, 8, 7], 2) == 0.0
    # Risk: $500 / $4 = 125 shares; notional: $20k / $100 = 200.
    assert mr.position_size(100_000, 100_000, 100, 2) == 125


def test_accepted_envelope_does_not_override_rejected_broker_order():
    assert not mr.broker_submission_confirmed(None)
    assert not mr.broker_submission_confirmed({"id": "1", "status": "rejected"})
    assert mr.broker_submission_confirmed({"id": "1", "status": "accepted"})
    assert mr.broker_submission_confirmed({"id": "1", "status": "filled"})


def test_signal_uses_completed_1530_bar_and_frozen_rules():
    start = date(2025, 1, 1)
    closes = [100 + i * 0.5 for i in range(210)]
    closes[-1] = closes[-2] - 3
    daily = [{
        "t": (start + timedelta(days=i)).isoformat() + "T21:00:00Z",
        "o": close - 0.2, "h": close + 1, "l": close - 1, "c": close,
    } for i, close in enumerate(closes)]
    today = start + timedelta(days=211)
    now = datetime.combine(today, datetime.min.time(), tzinfo=ET).replace(
        hour=15, minute=45
    )
    current = closes[-1] - 3
    intraday = [{
        "t": now.replace(hour=15, minute=30).isoformat(),
        "o": current + 0.5, "h": current + 1, "l": current - 1, "c": current,
    }]

    signal = mr.signal_from_bars("AAPL", daily, intraday, now)

    assert signal is not None
    assert signal["signal_time"].startswith(today.isoformat())
    assert signal["rsi2"] < 10
    assert signal["price"] > signal["sma200"] > signal["previous_sma200"]
    assert signal["passes"] is True


def test_signal_atr_uses_completed_prior_sessions_only():
    start = date(2025, 1, 1)
    daily = [{
        "t": (start + timedelta(days=i)).isoformat() + "T21:00:00Z",
        "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0,
    } for i in range(210)]
    today = start + timedelta(days=211)
    now = datetime.combine(today, datetime.min.time(), tzinfo=ET).replace(
        hour=15, minute=45
    )
    intraday = [{
        "t": now.replace(hour=15, minute=30).isoformat(),
        "o": 100.0, "h": 150.0, "l": 50.0, "c": 100.0,
    }]

    signal = mr.signal_from_bars("AAPL", daily, intraday, now)

    assert signal is not None
    assert signal["atr14"] == 2.0


def test_alphabet_is_issuer_deduplicated_before_ranking():
    signals = [
        {"symbol": "GOOG", "passes": True, "rsi2": 4.0},
        {"symbol": "GOOGL", "passes": True, "rsi2": 2.0},
        {"symbol": "MSFT", "passes": True, "rsi2": 3.0},
    ]

    assert mr.select_candidate(signals, set())["symbol"] == "GOOGL"
    assert mr.select_candidate(signals, {"GOOGL"})["symbol"] == "MSFT"
    assert mr.select_candidate(signals, {"GOOG"})["symbol"] == "MSFT"


def test_bulk_bar_omission_is_repaired_per_symbol(monkeypatch):
    now = datetime(2026, 8, 28, 15, 45, tzinfo=ET)
    daily = {"bars": {"AAPL": [{}] * 200, "MSFT": [{}] * 200}}
    bulk_intraday = {"bars": {"AAPL": [{"c": 1}]}}
    repaired_msft = {"bars": {"MSFT": [{"c": 1}]}}
    responses = iter([[daily, bulk_intraday], [repaired_msft]])
    monkeypatch.setattr(mr.mcp_client, "call_many", lambda calls: calls)
    monkeypatch.setattr(mr.mcp_client, "run", lambda _calls: next(responses))
    monkeypatch.setattr(
        mr, "signal_from_bars",
        lambda symbol, _daily, intraday, _now: ({"symbol": symbol} if intraday else None),
    )

    signals, errors = mr.fetch_signals(["AAPL", "MSFT"], now)

    assert errors == {}
    assert [signal["symbol"] for signal in signals] == ["AAPL", "MSFT"]


def _snapshot(now: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        now_et=now,
        session_date=now.date().isoformat(),
        market_open=True,
        regular_close=now.replace(hour=16, minute=0),
        account_number="PAPER",
        account_status="ACTIVE",
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        open_spreads=0,
        pending_opening_orders=(),
        stock_positions=(),
        stock_entries_today=0,
        pending_stock_orders=(),
        equity=100_000,
        buying_power=100_000,
    )


def test_approved_entry_is_oto_and_requires_broker_reconciliation(tmp_path, monkeypatch):
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    snapshot = _snapshot(now)
    candidate = {
        "symbol": "AAPL", "signal_time": now.replace(minute=30).isoformat(),
        "price": 100.0, "sma200": 90.0, "previous_sma200": 89.9,
        "rsi2": 4.0, "atr14": 2.0, "ema5": 103.0, "passes": True,
    }
    calls = []
    monkeypatch.setattr(mr.config, "ALPACA_ACCOUNT_ID", "PAPER")
    monkeypatch.setattr(mr.config, "STOCK_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "STOCK_MR_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(mr.config, "STOCK_MR_UNIVERSE", ["AAPL"])
    monkeypatch.setattr(mr, "fetch_signals", lambda _symbols, _now: ([candidate], {}))
    monkeypatch.setattr(mr, "news_brief", lambda _symbol, _now: ("No articles returned.", 0))
    monkeypatch.setattr(
        mr.llm, "review_stock_candidate",
        lambda _brief: ({
            "decision": "approve", "thesis": "numeric dip with no supplied event risk",
            "event_risk": "none observed in supplied news", "invalidation": "stop",
        }, "{}"),
    )
    monkeypatch.setattr(mr.mcp_client, "call", lambda tool, **kwargs: (tool, kwargs))
    monkeypatch.setattr(
        mr.mcp_client, "run",
        lambda call: calls.append(call) or {"status": "accepted"},
    )
    monkeypatch.setattr(
        mr, "verify_order",
        lambda client_id: {"id": "broker-order", "client_order_id": client_id},
    )

    result = mr.maybe_enter(snapshot)

    assert result["status"] == "SUBMITTED"
    tool, kwargs = calls[0]
    assert tool == "place_stock_order"
    assert kwargs["order_class"] == "oto"
    assert kwargs["type"] == "market"
    assert kwargs["stop_loss_stop_price"] == "96.00"
    assert result["earnings_calendar_verified"] is False
    assert mr.load_state()["positions"]["AAPL"]["status"] == "entry_pending"


def test_early_close_never_opens_1545_window():
    now = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    snapshot = _snapshot(now)
    snapshot.regular_close = now

    allowed, detail = mr._normal_entry_window(snapshot)

    assert not allowed
    assert "early-close" in detail


def test_holding_sessions_include_entry_day_and_ignore_early_close(monkeypatch):
    calendar = [
        {"date": "2026-08-31", "close": "16:00"},
        {"date": "2026-09-01", "close": "13:00"},
        {"date": "2026-09-02", "close": "16:00"},
        {"date": "2026-09-03", "close": "16:00"},
    ]
    monkeypatch.setattr(mr.mcp_client, "call", lambda tool, **kwargs: (tool, kwargs))
    monkeypatch.setattr(mr.mcp_client, "run", lambda _call: calendar)

    assert mr._holding_sessions("2026-08-31", date(2026, 9, 2)) == 2
    assert mr._holding_sessions("2026-08-31", date(2026, 9, 3)) == 3
