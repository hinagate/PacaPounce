"""Gate tests. The headline case is S13 — a real strategy that passed every
operational check in production and still lost money."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto import config  # noqa: E402
from veto.gates import GATE_CATALOG, evaluate, expected_value, friction_usd  # noqa: E402


def _spread(**kw):
    base = dict(
        underlying="SPY", qty=1, legs_short=1, legs_long=1,
        short_symbol="SPY260828P00640000", long_symbol="SPY260828P00635000",
        credit=0.30, width=5.0, short_delta=0.15, long_delta=0.08,
        short_quote_age=5, long_quote_age=5,
        short_rel_spread=0.03, long_rel_spread=0.04,
        friction_usd=6.0, order_type="limit",
    )
    base.update(kw)
    return base


CTX = {
    "open_positions": 0,
    "trades_today": 0,
    "held_symbols": [],
    "account_status": "ACTIVE",
    "trading_blocked": False,
    "account_blocked": False,
    "trade_suspended_by_user": False,
    "options_approved_level": 3,
    "options_trading_level": 3,
    "options_buying_power": 10_000.0,
    "equity": 100_000.0,
}


def test_every_gate_runs_in_documented_order():
    verdict = evaluate(_spread(width=2.0, long_delta=0.05,
                               long_symbol="SPY260828P00638000"), CTX)
    assert [check.name for check in verdict.checks] == [
        gate["name"] for gate in GATE_CATALOG
    ]
    assert len(verdict.checks) == 16


def test_s13_passes_operational_but_fails_economic():
    """The exact production trade: $5 wide, $0.30 credit."""
    v = evaluate(_spread(), CTX)
    names = {c.name for c in v.failures}
    assert names == {"economic_ev"}, f"expected only economic_ev to fail, got {names}"
    assert not v.approved
    assert v.economics["ev_net_usd"] < 0


def test_narrow_spread_same_credit_passes():
    """Same $0.30 credit on a $2 spread is a different trade entirely."""
    v = evaluate(_spread(width=2.0, long_delta=0.05,
                         long_symbol="SPY260828P00638000"), CTX)
    assert v.approved, v.reason
    assert v.economics["ev_net_usd"] > 0


def test_illiquid_leg_rejected():
    v = evaluate(_spread(width=2.0, long_delta=0.05, short_rel_spread=0.60), CTX)
    assert "liquidity" in {c.name for c in v.failures}


def test_stale_quote_rejected():
    v = evaluate(_spread(width=2.0, long_delta=0.05, short_quote_age=900), CTX)
    assert "quote_freshness" in {c.name for c in v.failures}


def test_friction_from_real_quotes():
    assert friction_usd(1.00, 1.04, 0.60, 0.66) == 5.0


def test_breakeven_matches_payoff_arithmetic():
    e = expected_value(0.30, 5.0, 0.15, 0.08)
    assert abs(e["breakeven_wr"] - 470 / 500) < 1e-6
    assert e["max_loss_usd"] == 470.0
    assert e["max_profit_usd"] == 30.0


# ── Real-world EV (the VRP path) ─────────────────────────────────────────────
LIVE = dict(spot=765.6, short_strike=758.0, long_strike=753.0, dte=2)


def test_implied_ev_is_near_zero_by_construction():
    """Credit IS the market's expected value of the spread, so an implied-only
    gate can never find edge. This is why the gate uses realised vol."""
    e = expected_value(0.52, 5.0, 0.191, 0.095, realized_vol=0.129, **LIVE)
    assert e["ev_basis"] == "realized_vol_flat"
    # With realised == implied, EV should be small relative to the credit.
    assert abs(e["ev_gross_usd"]) < 0.5 * e["max_profit_usd"]


def test_vrp_makes_the_trade_positive():
    """Implied 12.9% vs realised 10.5% - the premium seller is overpaid."""
    calm = expected_value(0.52, 5.0, 0.191, 0.095, realized_vol=0.105, **LIVE)
    hot = expected_value(0.52, 5.0, 0.191, 0.095, realized_vol=0.160, **LIVE)
    assert calm["ev_gross_usd"] > 0 > hot["ev_gross_usd"]


def test_exact_integration_beats_the_half_width_approximation():
    """Losses between the strikes cluster near the short strike, so assuming an
    average loss of width/2 overstates expected loss."""
    e = expected_value(0.52, 5.0, 0.191, 0.095, realized_vol=0.129, **LIVE)
    approx_loss = ((0.191 - 0.095) * 5.0 / 2 + 0.095 * 5.0) * 100
    assert e["expected_loss_real_usd"] < approx_loss


def test_missing_vol_falls_back_and_says_so():
    e = expected_value(0.30, 5.0, 0.15, 0.08)
    assert e["ev_basis"] == "implied_only"


def test_daily_annual_target_blocks_new_entry_in_kelly_mode(monkeypatch):
    monkeypatch.setattr(config, "FULL_BUYING_POWER", False)
    ctx = {
        **CTX,
        "annual_target_reached": True,
        "daily_pnl_usd": 20.50,
        "daily_target_usd": 9.34,
    }
    verdict = evaluate(
        _spread(width=2.0, long_delta=0.05,
                long_symbol="SPY260828P00638000"),
        ctx,
    )
    assert "annual_target_budget" in {check.name for check in verdict.failures}


def test_full_buying_power_mode_keeps_annual_target_display_only():
    verdict = evaluate(
        _spread(width=2.0, long_delta=0.05,
                long_symbol="SPY260828P00638000"),
        {**CTX, "annual_target_reached": True, "daily_pnl_usd": 20.50},
    )
    assert "annual_target_budget" not in {check.name for check in verdict.failures}


def test_reentry_must_be_better_safer_liquid_and_not_same_pair():
    spread = _spread(
        width=2.0,
        long_delta=0.05,
        long_symbol="SPY260828P00638000",
        strategy="put_credit_spread",
        spot=765.0,
        short_strike=640.0,
        long_strike=638.0,
    )
    first = evaluate(spread, CTX)
    quality = first.economics["ev_net_usd"] / first.economics["max_loss_usd"]
    reentry = {
        "active": True,
        "allowed": True,
        "exit": {
            "short_symbol": "SPY260828P00641000",
            "long_symbol": "SPY260828P00639000",
            "safe_buffer_pct": 0.15,
            "worst_relative_quote_width": 0.05,
            "entry_baseline": {
                "quality": quality / 1.30,
                "short_delta": 0.15,
            },
        },
    }
    approved = evaluate(spread, {**CTX, "reentry": reentry})
    assert "reentry_quality" not in {check.name for check in approved.failures}

    same_pair = {
        **reentry,
        "exit": {
            **reentry["exit"],
            "short_symbol": spread["short_symbol"],
            "long_symbol": spread["long_symbol"],
        },
    }
    rejected = evaluate(spread, {**CTX, "reentry": same_pair})
    assert "reentry_quality" in {check.name for check in rejected.failures}


def test_alpaca_options_gate_requires_spread_level_and_unblocked_account():
    spread = _spread(width=2.0, long_delta=0.05,
                     long_symbol="SPY260828P00638000")
    too_low = evaluate(spread, {**CTX, "options_trading_level": 2})
    assert "alpaca_options_eligible" in {c.name for c in too_low.failures}

    blocked = evaluate(spread, {**CTX, "trading_blocked": True})
    assert "alpaca_options_eligible" in {c.name for c in blocked.failures}


def test_full_buying_power_gate_rejects_defined_loss_above_broker_budget(monkeypatch):
    monkeypatch.setattr(config, "SPREAD_EQUITY_PCT", 1.0)
    verdict = evaluate(
        _spread(qty=2, width=2.0, long_delta=0.05,
                long_symbol="SPY260828P00638000"),
        {**CTX, "options_buying_power": 300.0},
    )
    failure = next(c for c in verdict.failures if c.name == "total_risk_cap")
    assert "exceeds $300.00 Alpaca options-BP budget by $40.00" in failure.detail


def test_full_buying_power_gate_describes_a_passing_comparison(monkeypatch):
    monkeypatch.setattr(config, "SPREAD_EQUITY_PCT", 1.0)
    verdict = evaluate(
        _spread(qty=1, width=2.0, long_delta=0.05,
                long_symbol="SPY260828P00638000"),
        {**CTX, "options_buying_power": 300.0},
    )
    check = next(c for c in verdict.checks if c.name == "total_risk_cap")
    assert check.passed
    assert "<= $300.00 Alpaca options-BP budget" in check.detail


def test_spread_lane_is_capped_by_its_own_equity_budget(monkeypatch):
    """Each lane owns a share of equity, so the long-call lane's budget does not
    depend on which lane happened to fill first."""
    monkeypatch.setattr(config, "SPREAD_EQUITY_PCT", 0.20)
    spread = _spread(qty=1, width=2.0, long_delta=0.05,
                     long_symbol="SPY260828P00638000")

    # Equity is the binding side: 20% of $1,000 is less than the $900 of BP.
    tight = evaluate(spread, {**CTX, "equity": 1_000.0,
                              "options_buying_power": 900.0})
    assert tight.economics["risk_cap_usd"] == 200.0

    # Broker buying power is the binding side when it is the smaller number.
    broke = evaluate(spread, {**CTX, "equity": 100_000.0,
                              "options_buying_power": 150.0})
    assert broke.economics["risk_cap_usd"] == 150.0
    assert broke.economics["spread_equity_budget_pct"] == 0.20


def test_call_spread_after_sharp_selloff_fails_rebound_gate():
    spread = _spread(
        strategy="call_credit_spread",
        short_symbol="SPY260828C00780000",
        long_symbol="SPY260828C00785000",
        short_strike=780.0,
        long_strike=785.0,
        spot=765.0,
        dte=2,
        realized_vol=0.12,
        smile={765.0: 0.16, 780.0: 0.18, 785.0: 0.19},
        vol_profile={"latest_1d_return": -0.015},
    )
    verdict = evaluate(spread, CTX)
    assert "call_rebound_risk" in {c.name for c in verdict.failures}


def test_put_spread_is_not_subject_to_call_rebound_gate():
    verdict = evaluate(
        _spread(vol_profile={"latest_1d_return": -0.05}),
        CTX,
    )
    assert "call_rebound_risk" not in {c.name for c in verdict.failures}
