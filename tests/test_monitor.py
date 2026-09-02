"""Pure-policy tests for the intraday paper spread monitor."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.monitor import (  # noqa: E402
    annual_target_status,
    chase_opening_order,
    close_order_request,
    decide_exit,
    pair_spreads,
    parse_occ,
)
from veto import config  # noqa: E402


ET = ZoneInfo("America/New_York")


def _positions():
    return [
        {
            "symbol": "SPY260828P00751000",
            "qty": "5",
            "avg_entry_price": "0.65",
            "unrealized_pl": "-40",
        },
        {
            "symbol": "SPY260828P00756000",
            "qty": "-5",
            "avg_entry_price": "1.24",
            "unrealized_pl": "60",
        },
    ]


def _metrics(**overrides):
    values = {
        "quote_ready": True,
        "spot": 765.0,
        "right": "P",
        "short_strike": 756.0,
        "long_strike": 751.0,
        "is_expiry_day": False,
        "profit_captured": 0.10,
        "loss_used": 0.0,
    }
    values.update(overrides)
    return values


def test_parse_occ():
    parsed = parse_occ("SPY260828P00756000")
    assert parsed is not None
    assert parsed["underlying"] == "SPY"
    assert parsed["expiry"].isoformat() == "2026-08-28"
    assert parsed["right"] == "P"
    assert parsed["strike"] == 756.0


def test_pair_live_credit_spread_from_api_positions():
    spreads, errors = pair_spreads(_positions())
    assert errors == []
    assert len(spreads) == 1
    spread = spreads[0]
    assert spread["qty"] == 5
    assert spread["short_strike"] == 756.0
    assert spread["long_strike"] == 751.0
    assert abs(spread["entry_credit"] - 0.59) < 1e-9


def test_eight_percent_target_becomes_geometric_daily_benchmark():
    target = annual_target_status({"equity": "30561.53", "last_equity": "30561.78"})
    assert target["annual_rate"] == 0.08
    assert abs(target["daily_target_usd"] - 9.34) < 0.02
    assert target["daily_pnl"] == -0.25


def test_account_target_lock_requires_positive_executable_trade_pnl(monkeypatch):
    monkeypatch.setattr(config, "FULL_BUYING_POWER", False)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=ET)
    action, _ = decide_exit(
        _metrics(
            pnl_executable=10.0,
            account_daily_target_usd=9.34,
            projected_daily_pnl_after_exit=9.50,
        ),
        now,
        True,
    )
    assert action == "annual_target_lock"

    action, _ = decide_exit(
        _metrics(
            pnl_executable=-1.0,
            account_daily_target_usd=9.34,
            projected_daily_pnl_after_exit=10.0,
        ),
        now,
        True,
    )
    assert action is None


def test_full_buying_power_mode_does_not_exit_at_annual_target():
    action, _ = decide_exit(
        _metrics(
            pnl_executable=10.0,
            account_daily_target_usd=9.34,
            projected_daily_pnl_after_exit=9.50,
        ),
        datetime(2026, 8, 25, 12, 0, tzinfo=ET),
        True,
    )
    assert action is None


def test_profit_target_uses_executable_capture():
    action, _ = decide_exit(
        _metrics(profit_captured=0.50),
        datetime(2026, 8, 25, 12, 0, tzinfo=ET),
        True,
        profit_exits=True,
    )
    assert action == "profit_target"


def test_profit_ratchet_closes_only_after_persistent_policy_trigger():
    action, decision = decide_exit(
        _metrics(
            pnl_executable=690.0,
            ratchet_exit=True,
            ratchet_trailing_floor_pnl=717.60,
            ratchet_high_water_pnl=897.0,
            pnl_volatility_high=False,
        ),
        datetime(2026, 8, 25, 12, 0, tzinfo=ET),
        True,
        profit_exits=True,
    )
    assert action == "profit_ratchet"
    assert "$897.00 high" in decision


def test_profit_exits_off_holds_to_expiry_but_keeps_the_breach(monkeypatch):
    """The entry gate prices the spread on its terminal payoff; on a $2-wide
    spread the 50% target and the trail were closing the winners early and
    leaving the losers to the breach, which made the configured monitor
    negative-EV (simulated -$520 vs +$587 hold-to-expiry with breach)."""
    now = datetime(2026, 8, 25, 12, 0, tzinfo=ET)
    winner = _metrics(
        profit_captured=0.80, pnl_executable=690.0, ratchet_exit=True,
        ratchet_trailing_floor_pnl=717.60, ratchet_high_water_pnl=897.0,
    )
    action, decision = decide_exit(winner, now, True, profit_exits=False)
    assert action is None
    assert decision == "hold_to_expiry"

    # The defined-risk protections are not profit exits and stay armed.
    action, _ = decide_exit(_metrics(spot=750.5), now, True, profit_exits=False)
    assert action == "long_strike_breach"
    action, _ = decide_exit(_metrics(loss_used=0.70), now, True, profit_exits=False)
    assert action == "stop_loss"
    action, _ = decide_exit(
        _metrics(is_expiry_day=True, spot=754.0),
        datetime(2026, 8, 28, 15, 35, tzinfo=ET), True, profit_exits=False,
    )
    assert action == "pin_risk"

    # The switch defaults to the configured value.
    monkeypatch.setattr(config, "MONITOR_PROFIT_EXIT_ENABLED", False)
    assert decide_exit(winner, now, True) == (None, "hold_to_expiry")
    monkeypatch.setattr(config, "MONITOR_PROFIT_EXIT_ENABLED", True)
    assert decide_exit(winner, now, True)[0] == "profit_target"


def test_non_expiry_stop_and_long_strike_breach():
    now = datetime(2026, 8, 25, 12, 0, tzinfo=ET)
    action, _ = decide_exit(_metrics(loss_used=0.70), now, True)
    assert action == "stop_loss"
    action, _ = decide_exit(_metrics(spot=750.5), now, True)
    assert action == "long_strike_breach"


def test_expiry_day_suppresses_midday_stop():
    action, decision = decide_exit(
        _metrics(is_expiry_day=True, loss_used=0.95, spot=753.0),
        datetime(2026, 8, 28, 12, 0, tzinfo=ET),
        True,
    )
    assert action is None
    assert "expiry" in decision


def test_expiry_pin_risk_closes_in_final_thirty_minutes():
    action, _ = decide_exit(
        _metrics(is_expiry_day=True, spot=754.0),
        datetime(2026, 8, 28, 15, 35, tzinfo=ET),
        True,
    )
    assert action == "pin_risk"


def test_closed_market_never_submits_exit():
    action, decision = decide_exit(
        _metrics(profit_captured=0.90, loss_used=0.90),
        datetime(2026, 8, 25, 16, 1, tzinfo=ET),
        False,
    )
    assert action is None
    assert decision == "market_closed"


def test_close_order_is_atomic_and_has_correct_intents():
    spread, _ = pair_spreads(_positions())
    request = close_order_request(spread[0], "profit_target", 0.29, "test-close-id")
    assert request["order_class"] == "mleg"
    assert request["type"] == "limit"
    assert float(request["limit_price"]) > 0
    assert request["legs"][0]["position_intent"] == "buy_to_close"
    assert request["legs"][1]["position_intent"] == "sell_to_close"
    assert request["qty"] == "5"


def test_risk_close_uses_market_order_for_immediacy():
    spread, _ = pair_spreads(_positions())
    request = close_order_request(spread[0], "stop_loss", 3.70, "test-stop-id")
    assert request["type"] == "market"
    assert "limit_price" not in request


def test_annual_target_resize_uses_atomic_limit_close():
    spread, _ = pair_spreads(_positions())
    resize = {**spread[0], "qty": 4}
    request = close_order_request(
        resize, "annual_target_resize", 0.60, "test-resize-id"
    )
    assert request["qty"] == "4"
    assert request["type"] == "limit"
    assert float(request["limit_price"]) > 0


def test_opening_chase_releases_collateral_and_reprices_one_cent(monkeypatch, tmp_path):
    calls = []
    context = {
        "spot": 768.72,
        "short_k": 763.0,
        "long_k": 758.0,
        "dte": 1,
        "smile": {758.0: 0.16, 763.0: 0.15, 769.0: 0.14},
        "ratio": 0.72,
        "friction": 1.0,
        "natural_credit": 0.46,
        "is_put": True,
    }
    order = {
        "id": "old-order",
        "client_order_id": "veto-open-20260827-decision-r0",
        "qty": "220",
        "limit_price": "-0.47",
        "legs": [
            {
                "symbol": "SPY260828P00763000",
                "side": "sell",
                "ratio_qty": "1",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": "SPY260828P00758000",
                "side": "buy",
                "ratio_qty": "1",
                "position_intent": "buy_to_open",
            },
        ],
    }
    monkeypatch.setattr(
        "scripts.monitor._build_opening_context", lambda _order: context
    )
    ev_calls = []

    def fake_ev(*args, **kwargs):
        ev_calls.append((args, kwargs))
        return 4.72

    monkeypatch.setattr("scripts.monitor.ev_at_credit", fake_ev)
    monkeypatch.setattr("scripts.monitor.CHASE_STEP", 0.01)
    monkeypatch.setattr("scripts.monitor.LOG", tmp_path / "session.jsonl")
    monkeypatch.setattr("scripts.monitor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "scripts.monitor.mcp_client.call",
        lambda tool, **kwargs: {"tool": tool, **kwargs},
    )

    def fake_run(request):
        calls.append(request)
        if request["tool"] == "get_account_info":
            return {"options_buying_power": "100000"}
        if request["tool"] == "get_orders":
            return [{
                "id": "replacement-order",
                "client_order_id": "veto-open-20260827-decision-r1",
                "status": "new",
                "filled_qty": "0",
            }]
        return {"status": "accepted"}

    monkeypatch.setattr("scripts.monitor.mcp_client.run", fake_run)

    # Alpaca reports no remaining BP while the full-capital old order is open.
    result = chase_opening_order(order, options_buying_power=0.0)

    replacement = next(call for call in calls if call["tool"] == "place_option_order")
    assert replacement["qty"] == "220"
    assert replacement["limit_price"] == "-0.46"
    assert ev_calls[0][1]["friction"] == 0.0
    assert result["old_qty"] == 220
    assert result["new_qty"] == 220
    assert result["natural_credit"] == 0.46
    assert result["released_collateral"] == 99_660.0
    assert result["refreshed_options_buying_power"] == 100_000.0
    assert result["max_loss_per_contract"] == 454.0
    assert result["total_defined_loss"] == 99_880.0
    assert result["kind"] == "chase"
    assert result["broker_order_id"] == "replacement-order"
    assert result["broker_status"] == "new"
    assert any(call["tool"] == "get_orders" for call in calls)


def test_opening_chase_rejects_accepted_envelope_without_broker_order(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        "scripts.monitor._build_opening_context",
        lambda _order: {
            "spot": 768.72,
            "short_k": 763.0,
            "long_k": 758.0,
            "dte": 1,
            "smile": {758.0: 0.16, 763.0: 0.15},
            "ratio": 0.72,
            "friction": 1.0,
            "natural_credit": 0.46,
            "is_put": True,
        },
    )
    monkeypatch.setattr("scripts.monitor.ev_at_credit", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr("scripts.monitor.CHASE_STEP", 0.01)
    monkeypatch.setattr("scripts.monitor.CHASE_VERIFY_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.monitor.LOG", tmp_path / "session.jsonl")
    monkeypatch.setattr("scripts.monitor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "scripts.monitor.mcp_client.call",
        lambda tool, **kwargs: {"tool": tool, **kwargs},
    )

    def fake_run(request):
        calls.append(request)
        if request["tool"] == "get_account_info":
            return {"options_buying_power": "1000"}
        if request["tool"] == "get_orders":
            return []
        return {"status": "accepted"}

    monkeypatch.setattr("scripts.monitor.mcp_client.run", fake_run)
    order = {
        "id": "ghost-order",
        "client_order_id": "veto-open-20260827-ghost-r0",
        "qty": "1",
        "limit_price": "-0.47",
        "legs": [],
    }

    result = chase_opening_order(order, options_buying_power=1.0)

    assert result["kind"] == "chase_stop"
    assert result["reason"] == "replacement not found in Alpaca get_orders"
    assert result["client_order_id"] == "veto-open-20260827-ghost-r1"
    assert "broker_order_id" not in result
    assert len([call for call in calls if call["tool"] == "get_orders"]) == 2


def test_opening_chase_keeps_existing_order_when_replacement_is_unaffordable(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        "scripts.monitor._build_opening_context",
        lambda _order: {
            "spot": 768.72,
            "short_k": 763.0,
            "long_k": 758.0,
            "dte": 1,
            "smile": {758.0: 0.16, 763.0: 0.15},
            "ratio": 0.72,
            "friction": 1.0,
            "natural_credit": 0.46,
            "is_put": True,
        },
    )
    monkeypatch.setattr("scripts.monitor.ev_at_credit", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr("scripts.monitor.CHASE_STEP", 0.01)
    monkeypatch.setattr("scripts.monitor.LOG", tmp_path / "session.jsonl")
    monkeypatch.setattr(
        "scripts.monitor.mcp_client.call",
        lambda tool, **kwargs: calls.append((tool, kwargs)),
    )
    order = {
        "id": "safe-order",
        "qty": "1",
        "limit_price": "-0.47",
        "legs": [],
    }

    result = chase_opening_order(order, options_buying_power=0.0)

    assert result["kind"] == "chase_stop"
    assert result["reason"] == "replacement cannot be collateralized"
    assert calls == []


def test_opening_chase_stays_flat_when_bp_does_not_return_after_cancel(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        "scripts.monitor._build_opening_context",
        lambda _order: {
            "spot": 768.72,
            "short_k": 763.0,
            "long_k": 758.0,
            "dte": 1,
            "smile": {758.0: 0.16, 763.0: 0.15},
            "ratio": 0.72,
            "friction": 1.0,
            "natural_credit": 0.46,
            "is_put": True,
        },
    )
    monkeypatch.setattr("scripts.monitor.ev_at_credit", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr("scripts.monitor.CHASE_STEP", 0.01)
    monkeypatch.setattr("scripts.monitor.LOG", tmp_path / "session.jsonl")
    monkeypatch.setattr("scripts.monitor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "scripts.monitor.mcp_client.call",
        lambda tool, **kwargs: {"tool": tool, **kwargs},
    )

    def fake_run(request):
        calls.append(request)
        if request["tool"] == "get_account_info":
            return {"options_buying_power": "100"}
        return {"status": "accepted"}

    monkeypatch.setattr("scripts.monitor.mcp_client.run", fake_run)
    order = {
        "id": "cancel-then-flat",
        "client_order_id": "veto-open-20260827-decision-r0",
        "qty": "1",
        "limit_price": "-0.47",
        "legs": [],
    }

    result = chase_opening_order(order, options_buying_power=1.0)

    assert result["kind"] == "chase_stop"
    assert result["reason"] == "collateral unavailable after cancellation"
    assert any(call["tool"] == "cancel_order_by_id" for call in calls)
    assert not any(call["tool"] == "place_option_order" for call in calls)
