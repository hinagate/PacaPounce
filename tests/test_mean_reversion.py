"""Frozen-signal tests for the options-only second Paper strategy."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from veto import config, mean_reversion as mr

ET = ZoneInfo("America/New_York")


def test_committed_validation_card_labels_stock_result_as_signal_proxy():
    path = mr.config.ROOT / "data" / "ndx30_option_mr_validation.json"
    card = json.loads(path.read_text(encoding="utf-8"))

    assert card["strategy_id"] == "NDX30_CALL_MR_01"
    assert card["status"] == "PAPER_STAGING_OPTION_EXPRESSION_NOT_VALIDATED"
    assert card["order_calls"] == 0
    assert card["underlying_signal_oos_2024"]["trades"] == 146
    assert card["underlying_signal_oos_2024"]["is_option_pnl"] is False
    assert card["options_execution"]["instrument"] == "single long call"


def test_validation_card_reports_the_option_level_result_not_only_the_proxy():
    """The stock proxy is positive; the option expression of it is not. The card
    has to carry both, or the lane reads as validated when it is not."""
    path = mr.config.ROOT / "data" / "ndx30_option_mr_validation.json"
    card = json.loads(path.read_text(encoding="utf-8"))

    option = card["option_expression_2026_ytd"]
    assert option["is_option_pnl"] is True
    zero_friction = next(
        s for s in option["scenarios"] if s["one_way_friction_pct"] == 0.0
    )
    # Flat before friction is the load-bearing number: the negative conclusion
    # does not rest on any friction assumption.
    assert zero_friction["profit_factor"] < 1.0
    assert all(s["return_pct"] <= 0 for s in option["scenarios"])
    assert "DO_NOT_PROMOTE_LONG_CALL_EXPRESSION" in card["decision"]

    # And the gate that acts on it must match the shipped configuration.
    assert (
        card["options_execution"]["maximum_relative_bid_ask"]
        == config.OPTION_MR_MAX_SPREAD_PCT
    )
    assert card["underlying_signal_2026_ytd"][
        "mean_underlying_move_pct_per_trade"
    ] == round(config.OPTION_MR_SIGNAL_EDGE_PCT * 100, 3)


def test_wilder_rsi_and_option_position_size_are_deterministic(monkeypatch):
    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", False)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "risk_budget")
    monkeypatch.setattr(mr.config, "OPTION_MR_EQUITY_RISK_PCT", 0.005)
    monkeypatch.setattr(mr.config, "OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT", 0.02)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_PREMIUM_PCT", 0.20)
    monkeypatch.setattr(mr.config, "OPTION_MR_STOP_ATR_MULTIPLE", 2.0)

    assert mr.wilder_rsi([10, 9, 8, 7], 2) == 0.0
    sized = mr.option_position_size(100_000, 100_000, 10.0, 0.70, 2.0)
    assert sized["contracts"] == 1
    assert sized["modeled_stop_loss_per_contract"] == 280.0
    assert sized["premium_per_contract"] == 1000.0


def test_single_contract_discreteness_can_use_bounded_risk_exception(monkeypatch):
    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", False)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "risk_budget")
    monkeypatch.setattr(mr.config, "OPTION_MR_EQUITY_RISK_PCT", 0.005)
    monkeypatch.setattr(mr.config, "OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT", 0.02)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_PREMIUM_PCT", 0.20)
    monkeypatch.setattr(mr.config, "OPTION_MR_STOP_ATR_MULTIPLE", 2.0)

    sized = mr.option_position_size(100_000, 100_000, 32.50, 0.7035, 9.3196)

    assert sized["contracts"] == 1
    assert sized["discrete_one_contract"] is True
    assert sized["modeled_stop_loss_per_contract"] == 1311.27
    assert sized["one_contract_risk_cap"] == 2000.0


def test_single_contract_exception_still_has_a_hard_risk_ceiling(monkeypatch):
    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", False)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "risk_budget")
    monkeypatch.setattr(mr.config, "OPTION_MR_EQUITY_RISK_PCT", 0.005)
    monkeypatch.setattr(mr.config, "OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT", 0.02)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_PREMIUM_PCT", 0.20)
    monkeypatch.setattr(mr.config, "OPTION_MR_STOP_ATR_MULTIPLE", 2.0)

    sized = mr.option_position_size(100_000, 100_000, 40.00, 0.80, 15.0)

    assert sized["contracts"] == 0
    assert sized["discrete_one_contract"] is False


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
    monkeypatch.setattr(mr.mcp_client, "call_many_time_windows", lambda calls: calls)
    monkeypatch.setattr(mr.mcp_client, "run", lambda _calls: next(responses))
    monkeypatch.setattr(
        mr, "signal_from_bars",
        lambda symbol, _daily, intraday, _now: ({"symbol": symbol} if intraday else None),
    )

    signals, errors = mr.fetch_signals(["AAPL", "MSFT"], now)

    assert errors == {}
    assert [signal["symbol"] for signal in signals] == ["AAPL", "MSFT"]


def _chain(monkeypatch, snapshots, hold_sessions=3):
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_HOLD_SESSIONS", hold_sessions)
    monkeypatch.setattr(mr.config, "OPTION_MR_CARRY_EDGE_MULTIPLE", 1.0)
    monkeypatch.setattr(
        mr.config, "OPTION_MR_CARRY_CEILING_PCT", mr.config.OPTION_MR_SIGNAL_EDGE_PCT
    )
    monkeypatch.setattr(
        mr.mcp_client, "call_all_pages", lambda tool, **kwargs: (tool, kwargs)
    )
    monkeypatch.setattr(mr.mcp_client, "run", lambda _call: {"snapshots": snapshots})


def test_contract_selection_requires_liquid_fresh_target_delta_call(monkeypatch):
    """Quotes are the ones measured on the live AAPL chain: spot 313.89, an
    18-DTE 310 call at 13.49/13.95 carries 0.62% against a 0.795% edge."""
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    _chain(monkeypatch, {
        "AAPL260918C00310000": {
            "latestQuote": {"bp": 13.49, "ap": 13.95, "t": now.isoformat()},
            "greeks": {"delta": 0.69}, "impliedVolatility": 0.28,
        },
        "AAPL260918C00340000": {
            "latestQuote": {"bp": 4.00, "ap": 4.10, "t": now.isoformat()},
            "greeks": {"delta": 0.40}, "impliedVolatility": 0.30,
        },
    })

    contract, error = mr.select_long_call_contract(
        {"symbol": "AAPL", "price": 313.89}, now
    )

    assert error == ""
    assert contract["symbol"] == "AAPL260918C00310000"
    assert contract["entry_limit"] == 13.95
    assert contract["delta"] == 0.69
    assert contract["required_move_pct"] < config.OPTION_MR_SIGNAL_EDGE_PCT


def test_contract_selection_prefers_the_widest_margin_over_the_measured_edge(monkeypatch):
    """Both contracts are affordable; the cheaper carry wins even though the
    other sits marginally closer to the 0.70 delta target."""
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    _chain(monkeypatch, {
        "AMGN260918C00415000": {
            "latestQuote": {"bp": 30.90, "ap": 32.50, "t": now.isoformat()},
            "greeks": {"delta": 0.7035},
        },
        "AMGN260918C00410000": {
            "latestQuote": {"bp": 34.90, "ap": 35.60, "t": now.isoformat()},
            "greeks": {"delta": 0.7241},
        },
    })

    contract, error = mr.select_long_call_contract(
        {"symbol": "AMGN", "price": 431.46}, now
    )

    assert error == ""
    assert contract["symbol"] == "AMGN260918C00410000"
    assert contract["edge_margin_pct"] > 0


def test_contract_whose_carry_exceeds_the_measured_edge_is_rejected(monkeypatch):
    """The economic filter the operational ones omit.

    This AMGN pair passes every operational check that existed before - real
    strikes, live greeks, target delta, fresh quotes - and still cannot be paid
    for: crossing the 4.30 spread alone needs a bigger move than the signal has
    ever averaged. On the live chain, 48% of contracts inside the old 15%
    spread gate failed exactly this test.
    """
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    _chain(monkeypatch, {
        "AMGN260918C00415000": {
            "latestQuote": {"bp": 28.20, "ap": 32.50, "t": now.isoformat()},
            "greeks": {"delta": 0.7035},
        },
    })

    contract, error = mr.select_long_call_contract(
        {"symbol": "AMGN", "price": 431.46}, now
    )

    assert contract is None
    assert "carry inside" in error


def test_carry_to_break_even_prices_crossing_and_theta():
    carry = mr.carry_to_break_even(
        ask=13.95, bid=13.49, spot=313.89, strike=310.0, delta=0.69, dte=18,
        hold_sessions=3,
    )
    # 0.46 of crossing plus square-root decay of 10.06 extrinsic over 3 of 18
    # sessions, expressed as the underlying move that offsets it.
    assert carry["crossing_usd"] == 46.0
    assert 85.0 < carry["theta_usd"] < 90.0
    assert 0.0060 < carry["required_move_pct"] < 0.0063

    # A wide quote on the same contract is arithmetically unpayable.
    wide = mr.carry_to_break_even(
        ask=15.50, bid=11.20, spot=313.89, strike=310.0, delta=0.69, dte=18,
        hold_sessions=3,
    )
    assert wide["required_move_pct"] > config.OPTION_MR_SIGNAL_EDGE_PCT


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
        options_approved_level=3,
        options_trading_level=3,
        options_buying_power=100_000,
        option_positions=(),
        non_option_positions=(),
        option_mr_entries_today=0,
        pending_opening_orders=(),
        pending_closing_orders=(),
        equity=100_000,
    )


def test_approved_entry_is_single_leg_option_limit_and_broker_reconciled(
    tmp_path, monkeypatch
):
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    snapshot = _snapshot(now)
    candidate = {
        "symbol": "AAPL", "signal_time": now.replace(minute=30).isoformat(),
        "price": 100.0, "sma200": 90.0, "previous_sma200": 89.9,
        "rsi2": 4.0, "atr14": 2.0, "ema5": 103.0, "passes": True,
    }
    carry = mr.carry_to_break_even(
        ask=10.2, bid=10.0, spot=100.0, strike=95.0, delta=0.69, dte=18,
        hold_sessions=3,
    )
    contract = {
        "symbol": "AAPL260918C00095000", "underlying": "AAPL",
        "expiry": "2026-09-18", "strike": 95.0, "dte": 18,
        "bid": 10.0, "ask": 10.2, "mid": 10.1, "entry_limit": 10.2,
        "delta": 0.69, "iv": 0.28, "rel_spread": 0.0198, "quote_age": 1.0,
        "carry": carry,
        "required_move_pct": round(carry["required_move_pct"], 6),
        "edge_margin_pct": round(
            config.OPTION_MR_SIGNAL_EDGE_PCT - carry["required_move_pct"], 6
        ),
    }
    calls = []
    monkeypatch.setattr(mr.config, "ALPACA_ACCOUNT_ID", "PAPER")
    monkeypatch.setattr(mr.config, "OPTION_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "OPTION_MR_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(mr.config, "OPTION_MR_UNIVERSE", ["AAPL"])
    monkeypatch.setattr(
        mr.config, "OPTION_MR_SIGNAL_EDGE_PCT", carry["required_move_pct"] + 1e-4
    )
    monkeypatch.setattr(mr, "fetch_signals", lambda _symbols, _now: ([candidate], {}))
    monkeypatch.setattr(mr, "select_long_call_contract", lambda _candidate, _now: (contract, ""))
    monkeypatch.setattr(mr, "news_brief", lambda _symbol, _now: ("No articles returned.", 0))
    monkeypatch.setattr(
        mr.llm, "review_option_mr_candidate",
        lambda _brief: ({
            "decision": "approve", "thesis": "numeric dip with no supplied event risk",
            "event_risk": "none observed in supplied news", "invalidation": "stop",
        }, "{}"),
    )
    monkeypatch.setattr(mr.mcp_client, "call", lambda tool, **kwargs: (tool, kwargs))
    monkeypatch.setattr(
        mr.mcp_client, "run", lambda call: calls.append(call) or {"status": "accepted"},
    )
    monkeypatch.setattr(
        mr, "verify_order",
        lambda client_id: {"id": "broker-order", "client_order_id": client_id,
                           "status": "accepted"},
    )

    result = mr.maybe_enter(snapshot)

    assert result["status"] == "SUBMITTED"
    tool, kwargs = calls[0]
    assert tool == "place_option_order"
    assert kwargs["symbol"] == contract["symbol"]
    assert kwargs["position_intent"] == "buy_to_open"
    assert kwargs["type"] == "limit"
    assert kwargs["limit_price"] == "10.20"
    assert result["earnings_calendar_verified"] is False
    assert mr.load_state()["positions"][contract["symbol"]]["status"] == "entry_pending"


def test_monitor_stop_sells_option_to_close_at_executable_bid(tmp_path, monkeypatch):
    now = datetime(2026, 8, 31, 10, 0, tzinfo=ET)
    contract = "AAPL260918C00095000"
    monkeypatch.setattr(mr.config, "OPTION_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "OPTION_MR_LOG", tmp_path / "decisions.jsonl")
    mr.save_state({"positions": {contract: {
        "status": "open", "contract_symbol": contract, "underlying": "AAPL",
        "signal_date": "2026-08-31", "underlying_stop": 96.0,
        "entry_client_order_id": "paca-callmr-open-test", "qty": 1,
    }}})
    calls = []
    monkeypatch.setattr(mr.mcp_client, "call", lambda tool, **kwargs: (tool, kwargs))
    monkeypatch.setattr(mr.mcp_client, "call_many", lambda calls_: ("many", calls_))

    def run(call):
        if call[0] == "many":
            return [
                {"quotes": {contract: {"bp": 7.0, "ap": 7.2}}},
                {"quotes": {"AAPL": {"bp": 94.9, "ap": 95.1}}},
            ]
        calls.append(call)
        return {"status": "accepted"}

    monkeypatch.setattr(mr.mcp_client, "run", run)
    monkeypatch.setattr(
        mr, "verify_order",
        lambda client_id, **_kwargs: {"id": "exit-order", "status": "accepted",
                                      "client_order_id": client_id},
    )
    positions = [{
        "symbol": contract, "asset_class": "us_option", "qty": "1",
        "avg_entry_price": "10.0", "unrealized_pl": "-300",
    }]

    result = mr.monitor_cycle(
        {"timestamp": now.isoformat(), "is_open": True}, [], positions, {}, True
    )

    tool, kwargs = calls[0]
    assert tool == "place_option_order"
    assert kwargs["symbol"] == contract
    assert kwargs["position_intent"] == "sell_to_close"
    assert kwargs["type"] == "limit"
    assert kwargs["limit_price"] == "7.00"
    assert result["managed_contracts"] == [contract]
    assert mr.load_state()["positions"][contract]["exit_reason"] == "underlying_stop"


def test_early_close_never_opens_1545_window():
    now = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    snapshot = _snapshot(now)
    snapshot.regular_close = now

    allowed, detail = mr._normal_entry_window(snapshot)

    assert not allowed
    assert "early-close" in detail


def test_transient_market_data_can_retry_without_consuming_the_day(
    tmp_path, monkeypatch
):
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    snapshot = _snapshot(now)
    monkeypatch.setattr(mr.config, "ALPACA_ACCOUNT_ID", "PAPER")
    monkeypatch.setattr(mr.config, "OPTION_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "OPTION_MR_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(mr.config, "OPTION_MR_UNIVERSE", ["AAPL"])
    monkeypatch.setattr(mr, "fetch_signals", lambda _symbols, _now: ([], {"daily": "lag"}))

    result = mr.maybe_enter(snapshot)

    assert result["status"] == "waiting"
    assert result["reason"] == "sip_data_pending"
    assert mr.load_state() == {}


def test_transient_market_data_records_one_final_veto_near_window_end(
    tmp_path, monkeypatch
):
    now = datetime(2026, 8, 31, 15, 53, tzinfo=ET)
    snapshot = _snapshot(now)
    monkeypatch.setattr(mr.config, "ALPACA_ACCOUNT_ID", "PAPER")
    monkeypatch.setattr(mr.config, "OPTION_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "OPTION_MR_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(mr.config, "OPTION_MR_UNIVERSE", ["AAPL"])
    monkeypatch.setattr(mr, "fetch_signals", lambda _symbols, _now: ([], {"daily": "lag"}))

    result = mr.maybe_enter(snapshot)

    assert result["status"] == "VETOED"
    assert result["reason"] == "incomplete_sip_data"
    assert mr.load_state()["last_scan_date"] == "2026-08-31"


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


def test_signal_bars_are_completed_by_time_windows_never_by_page_token(monkeypatch):
    # get_stock_bars reports next_page_token but rejects page_token, so every
    # bars request must be sliced into windows that each reach EOF on their own.
    now = datetime(2026, 8, 28, 15, 45, tzinfo=ET)
    batches = []

    def refuse(*_args, **_kwargs):
        raise AssertionError("bars must not use a page_token path")

    monkeypatch.setattr(mr.mcp_client, "call", refuse)
    monkeypatch.setattr(mr.mcp_client, "call_many", refuse)
    monkeypatch.setattr(mr.mcp_client, "call_all_pages", refuse)
    monkeypatch.setattr(mr.mcp_client, "call_many_all_pages", refuse)
    monkeypatch.setattr(
        mr.mcp_client, "call_many_time_windows",
        lambda calls, **_kwargs: batches.append(calls) or calls,
    )
    monkeypatch.setattr(mr.mcp_client, "run", lambda calls: [
        {"bars": {"AAPL": [{"c": 1}] * 200}} for _ in calls
    ])
    monkeypatch.setattr(
        mr, "signal_from_bars", lambda symbol, _daily, _intraday, _now: {"symbol": symbol}
    )

    signals, errors = mr.fetch_signals(["AAPL"], now)

    assert errors == {} and [s["symbol"] for s in signals] == ["AAPL"]
    calls = batches[0]
    assert [tool for tool, *_ in calls] == ["get_stock_bars", "get_stock_bars"]
    # Each entry carries its own window width, and both stay inside one page.
    daily_window, intraday_window = calls[0][2], calls[1][2]
    assert daily_window == mr.mcp_client.bar_window_days(1, 1.0)
    assert intraday_window == mr.mcp_client.bar_window_days(
        1, mr.INTRADAY_BARS_PER_SESSION
    )
    assert intraday_window < daily_window


def test_two_lane_budgets_are_taken_from_equity_not_remaining_buying_power():
    """Sizing off remaining BP made a lane's allocation depend on fill order."""
    from veto import sizing as sizing_mod

    equity, full_bp = 100_000.0, 100_000.0
    assert sizing_mod.spread_budget(full_bp, equity, 0.20) == 20_000.0
    assert sizing_mod.option_mr_budget(full_bp, equity, 0.70) == 70_000.0

    # After the spread lane fills, only $80k of BP remains - but the long-call
    # lane's own budget is unchanged, because it is a share of equity.
    assert sizing_mod.option_mr_budget(80_000.0, equity, 0.70) == 70_000.0

    # Premium already deployed is subtracted, and BP still bounds the result.
    assert sizing_mod.option_mr_budget(80_000.0, equity, 0.70, 50_000.0) == 20_000.0
    assert sizing_mod.option_mr_budget(5_000.0, equity, 0.70) == 5_000.0
    assert sizing_mod.option_mr_budget(80_000.0, equity, 0.70, 90_000.0) == 0.0


def test_tournament_sizing_uses_the_premium_budget_not_the_atr_risk_model(monkeypatch):
    """The objective change is explicit: a leaderboard rewards the upper tail,
    so size comes from the premium budget rather than the 2xATR stop model."""
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_PREMIUM_PCT", 0.35)
    monkeypatch.setattr(mr.config, "MAX_CONTRACTS", 1000)

    args = dict(equity=100_000.0, options_buying_power=100_000.0,
                premium=24.35, delta=0.82, atr=5.0)

    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", False)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "risk_budget")
    monkeypatch.setattr(mr.config, "OPTION_MR_EQUITY_RISK_PCT", 0.005)
    conservative = mr.option_position_size(**args, premium_budget=70_000.0)

    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", True)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "tournament")
    tournament = mr.option_position_size(**args, premium_budget=70_000.0)

    # floor(min(35% position cap, 70k budget, BP) / $2,435 premium) = 14
    assert tournament["contracts"] == 14
    assert tournament["mode"] == "tournament"
    assert tournament["contracts"] > conservative["contracts"]
    # The modeled stop is still computed and recorded, it just is not the input.
    assert tournament["modeled_stop_loss_per_contract"] > 0
    assert tournament["risk_budget"] == 500.0

    # The remaining portfolio budget still binds when it is the smaller number.
    capped = mr.option_position_size(**args, premium_budget=10_000.0)
    assert capped["contracts"] == 4


def test_deployed_premium_counts_only_live_positions():
    state = {"positions": {
        "A": {"status": "open", "max_premium": 20_000.0},
        "B": {"status": "entry_pending", "max_premium": 15_000.0},
        "C": {"status": "exit_pending", "max_premium": 5_000.0},
        "D": {"status": "closed", "max_premium": 99_000.0},
        "E": "not-a-record",
    }}
    assert mr.deployed_premium(state) == 40_000.0
    assert mr.deployed_premium({}) == 0.0


def _mr_candidate(symbol, now, rsi):
    return {
        "symbol": symbol, "signal_time": now.replace(minute=30).isoformat(),
        "price": 100.0, "sma200": 90.0, "previous_sma200": 89.9,
        "rsi2": rsi, "atr14": 2.0, "ema5": 103.0, "passes": True,
    }


def _mr_contract(symbol, now):
    carry = mr.carry_to_break_even(
        ask=10.2, bid=10.0, spot=100.0, strike=95.0, delta=0.69, dte=18,
        hold_sessions=3,
    )
    return {
        "symbol": f"{symbol}260918C00095000", "underlying": symbol,
        "expiry": "2026-09-18", "strike": 95.0, "dte": 18,
        "bid": 10.0, "ask": 10.2, "mid": 10.1, "entry_limit": 10.2,
        "delta": 0.69, "iv": 0.28, "rel_spread": 0.0198, "quote_age": 1.0,
        "carry": carry,
        "required_move_pct": round(carry["required_move_pct"], 6),
        "edge_margin_pct": 0.001,
    }


def test_one_window_opens_several_positions_each_with_its_own_ai_review(
    tmp_path, monkeypatch
):
    """Deploying a portfolio must be a portfolio of separate AI decisions, not
    one bulk allocation: every entry gets its own news pull and event review."""
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    snapshot = _snapshot(now)
    candidates = [
        _mr_candidate("AAPL", now, 4.0),
        _mr_candidate("MSFT", now, 6.0),
        _mr_candidate("AMZN", now, 8.0),
    ]
    contracts = {c["symbol"]: _mr_contract(c["symbol"], now) for c in candidates}
    reviewed, orders = [], []

    monkeypatch.setattr(mr.config, "ALPACA_ACCOUNT_ID", "PAPER")
    monkeypatch.setattr(mr.config, "OPTION_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "OPTION_MR_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(mr.config, "OPTION_MR_UNIVERSE", ["AAPL", "MSFT", "AMZN"])
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_POSITIONS", 3)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_ENTRIES_PER_DAY", 2)
    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", True)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "tournament")
    monkeypatch.setattr(mr.config, "OPTION_MR_TOTAL_PREMIUM_PCT", 0.70)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_PREMIUM_PCT", 0.35)
    monkeypatch.setattr(mr.config, "OPTION_MR_CARRY_CEILING_PCT", 1.0)
    monkeypatch.setattr(mr, "fetch_signals", lambda _symbols, _now: (candidates, {}))
    monkeypatch.setattr(
        mr, "select_long_call_contract",
        lambda candidate, _now: (contracts[candidate["symbol"]], ""),
    )
    monkeypatch.setattr(
        mr, "news_brief",
        lambda symbol, _now: (reviewed.append(symbol) or "No articles.", 0),
    )
    monkeypatch.setattr(
        mr.llm, "review_option_mr_candidate",
        lambda _brief: ({
            "decision": "approve", "thesis": "t",
            "event_risk": "none", "invalidation": "stop",
        }, "{}"),
    )
    monkeypatch.setattr(mr.mcp_client, "call", lambda tool, **kwargs: (tool, kwargs))
    monkeypatch.setattr(
        mr.mcp_client, "run",
        lambda call: orders.append(call) or {"status": "accepted"},
    )
    monkeypatch.setattr(
        mr, "verify_order",
        lambda client_id: {"id": f"broker-{client_id}", "client_order_id": client_id,
                           "status": "accepted"},
    )

    result = mr.maybe_enter(snapshot)

    # Two entries: the daily cap, not the candidate supply, is what stopped it.
    assert result["status"] == "SUBMITTED"
    assert len(result["entries"]) == 2
    assert [entry["underlying"] for entry in result["entries"]] == ["AAPL", "MSFT"]
    assert all(entry["status"] == "SUBMITTED" for entry in result["entries"])

    # One news pull and one AI review per entry, and one order per entry.
    assert reviewed == ["AAPL", "MSFT"]
    tools = [tool for tool, _ in orders]
    assert tools.count("place_option_order") == 2
    # Alpaca reserves an accepted order's collateral before it fills, so the
    # second entry must size against a re-read of live buying power rather than
    # against the snapshot the window opened with.
    assert "get_account_info" in tools
    assert tools.index("get_account_info") == 1  # after entry one, before entry two

    positions = mr.load_state()["positions"]
    assert len(positions) == 2
    # Each position is independently sized against the shared premium budget,
    # and the budget shrinks as it is consumed.
    assert result["premium_budget_remaining"] < 70_000.0
    assert sum(record["max_premium"] for record in positions.values()) <= 70_000.0


def test_window_stops_at_the_portfolio_premium_budget(tmp_path, monkeypatch):
    """The total budget, not the per-position cap, is the portfolio ceiling."""
    now = datetime(2026, 8, 31, 15, 45, tzinfo=ET)
    snapshot = _snapshot(now)
    candidates = [_mr_candidate(s, now, i) for i, s in enumerate(["AAPL", "MSFT"], 1)]
    contracts = {c["symbol"]: _mr_contract(c["symbol"], now) for c in candidates}

    monkeypatch.setattr(mr.config, "ALPACA_ACCOUNT_ID", "PAPER")
    monkeypatch.setattr(mr.config, "OPTION_MR_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr.config, "OPTION_MR_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(mr.config, "OPTION_MR_UNIVERSE", ["AAPL", "MSFT"])
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_POSITIONS", 3)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_ENTRIES_PER_DAY", 3)
    monkeypatch.setattr(mr.config, "OPTION_MR_TOURNAMENT", True)
    monkeypatch.setattr(mr.config, "OPTION_MR_SIZING_MODE", "tournament")
    monkeypatch.setattr(mr.config, "OPTION_MR_CARRY_CEILING_PCT", 1.0)
    # One position may take the whole budget, so the second gets nothing.
    monkeypatch.setattr(mr.config, "OPTION_MR_TOTAL_PREMIUM_PCT", 0.10)
    monkeypatch.setattr(mr.config, "OPTION_MR_MAX_PREMIUM_PCT", 0.10)
    monkeypatch.setattr(mr, "fetch_signals", lambda _symbols, _now: (candidates, {}))
    monkeypatch.setattr(
        mr, "select_long_call_contract",
        lambda candidate, _now: (contracts[candidate["symbol"]], ""),
    )
    monkeypatch.setattr(mr, "news_brief", lambda _symbol, _now: ("No articles.", 0))
    monkeypatch.setattr(
        mr.llm, "review_option_mr_candidate",
        lambda _brief: ({"decision": "approve", "thesis": "t",
                         "event_risk": "none", "invalidation": "stop"}, "{}"),
    )
    monkeypatch.setattr(mr.mcp_client, "call", lambda tool, **kwargs: (tool, kwargs))
    monkeypatch.setattr(mr.mcp_client, "run", lambda _call: {"status": "accepted"})
    monkeypatch.setattr(
        mr, "verify_order",
        lambda client_id: {"id": "b", "client_order_id": client_id,
                           "status": "accepted"},
    )

    result = mr.maybe_enter(snapshot)

    assert len(result["entries"]) == 1
    total = sum(r["max_premium"] for r in mr.load_state()["positions"].values())
    assert total <= 10_000.0
