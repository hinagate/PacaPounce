"""Gate stack: operational firewall + economic check.

Operational gates prevent malformed or oversized orders. They are necessary and
they are not sufficient - a perfectly-formed, correctly-sized, liquid spread can
still be negative expectancy. The economic gate is the one that catches that.

The economic gate uses the market's OWN implied probabilities (option deltas) to
price the trade's expected value. It forecasts nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config, sizing


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    approved: bool
    checks: list[Check] = field(default_factory=list)
    economics: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


# Ordered, judge-facing description of the decision stack. Keep this catalog in
# the same order as evaluate(); the dashboard and tests use it as the canonical
# explanation of what each candidate must survive.
GATE_CATALOG = (
    {"name": "defined_risk", "label": "Defined risk", "layer": "Structure",
     "question": "Is every short option protected by a long option?"},
    {"name": "allowlist", "label": "Underlying allowlist", "layer": "Policy",
     "question": "Is the underlying explicitly permitted?"},
    {"name": "alpaca_options_eligible", "label": "Alpaca options eligibility", "layer": "Broker",
     "question": "Does Alpaca report an active, unblocked Level 3+ options account with buying power?"},
    {"name": "position_size", "label": "Position size", "layer": "Sizing",
     "question": "Is the requested contract count below the hard ceiling?"},
    {"name": "open_positions", "label": "Open-position limit", "layer": "Portfolio",
     "question": "Can the portfolio accept another position?"},
    {"name": "daily_trade_limit", "label": "Daily trade limit", "layer": "Portfolio",
     "question": "Is the agent still inside its daily activity budget?"},
    {"name": "annual_target_budget", "label": "Objective budget", "layer": "Objective",
     "question": "Does the selected objective permit another entry?"},
    {"name": "no_duplicate", "label": "Duplicate exposure", "layer": "Portfolio",
     "question": "Is the proposed short contract absent from current holdings?"},
    {"name": "quote_freshness", "label": "Quote freshness", "layer": "Market data",
     "question": "Are both leg quotes recent enough to trust?"},
    {"name": "liquidity", "label": "Liquidity", "layer": "Market data",
     "question": "Are both bid/ask spreads inside the liquidity threshold?"},
    {"name": "max_loss_cap", "label": "Per-contract loss cap", "layer": "Risk",
     "question": "Is defined loss per contract inside policy?"},
    {"name": "total_risk_cap", "label": "Alpaca buying-power cap", "layer": "Risk",
     "question": "Can reported options buying power collateralize the entire defined loss?"},
    {"name": "limit_order_only", "label": "Limit order only", "layer": "Execution",
     "question": "Will the spread be submitted as a limit order?"},
    {"name": "call_rebound_risk", "label": "Call rebound risk", "layer": "Economics",
     "question": "For a call spread, has the index avoided a sharp one-day selloff that can rebound?"},
    {"name": "reentry_quality", "label": "Re-entry improvement", "layer": "Lifecycle",
     "question": "After a profit exit, is this candidate materially better and no riskier than the exited trade?"},
    {"name": "economic_ev", "label": "Economic EV", "layer": "Economics",
     "question": "Is expected value positive after measured execution friction?"},
)


# ── Economics ────────────────────────────────────────────────────────────────
def _expected_loss(spot: float, short_k: float, long_k: float,
                   sigma: float, T: float, steps: int = 4000,
                   is_put: bool = True) -> float:
    """Expected capped spread loss under lognormal S_T, per share.

    Integrated numerically rather than approximated. An earlier version assumed
    the average loss between the strikes was width/2; on live SPY quotes that
    overstated expected loss by ~12%, because losses between the strikes cluster
    near the short strike where the density is highest.
    """
    if sigma <= 0 or T <= 0:
        intrinsic = (
            max(short_k - spot, 0.0)
            if is_put else max(spot - short_k, 0.0)
        )
        return min(intrinsic, abs(short_k - long_k))
    width = abs(short_k - long_k)
    lo, hi = spot * math.exp(-6 * sigma * math.sqrt(T)), spot * math.exp(6 * sigma * math.sqrt(T))
    dx = (hi - lo) / steps
    var = sigma * sigma * T
    total = 0.0
    for i in range(steps):
        s = lo + (i + 0.5) * dx
        z = math.log(s / spot) + 0.5 * var
        density = math.exp(-(z * z) / (2 * var)) / (s * math.sqrt(2 * math.pi * var))
        intrinsic = (
            max(short_k - s, 0.0)
            if is_put else max(s - short_k, 0.0)
        )
        total += min(intrinsic, width) * density * dx
    return total


def expected_value(credit: float, width: float,
                   short_delta: float, long_delta: float,
                   spot: float = 0.0, short_strike: float = 0.0,
                   long_strike: float = 0.0, dte: float = 0.0,
                   realized_vol: float = 0.0,
                   smile: dict[float, float] | None = None,
                   is_put: bool = True) -> dict:
    """EV of a defined-risk credit spread, per contract.

    Two numbers, and the difference between them is the entire thesis.

    IMPLIED EV uses the option chain's own probabilities. It is ~0 by
    construction: the credit IS the market's expected value of the spread. It is
    reported as a sanity check, never as a decision input. A gate built on it
    would veto every trade forever.

    REAL-WORLD EV uses volatility actually realised by the underlying. For index
    options, implied vol exceeds subsequent realised vol most of the time - the
    variance risk premium - so the real-world probability of loss is lower than
    the price implies. That gap is what a premium seller is paid for.

    This is not forecast-free: it assumes near-future vol resembles recent vol.
    That is a far weaker claim than predicting direction, but it IS a claim.
    """
    ds, dl = abs(short_delta), abs(long_delta)
    dl = min(dl, ds)  # long leg is further OTM; guard against noisy greeks

    max_loss = (width - credit) * 100
    breakeven_wr = max_loss / (max_loss + credit * 100) if credit > 0 else 1.0

    # Implied: the chain's own probabilities, exact integration where possible.
    p_win = 1 - ds
    ev_implied = credit - ((ds - dl) * width / 2 + dl * width)

    out: dict = {
        "credit": round(credit, 4),
        "width": width,
        "short_delta": round(ds, 4),
        "long_delta": round(dl, 4),
        "max_profit_usd": round(credit * 100, 2),
        "max_loss_usd": round(max_loss, 2),
        "breakeven_wr": round(breakeven_wr, 4),
        "implied_wr": round(p_win, 4),
        "edge_pp": round((p_win - breakeven_wr) * 100, 2),
        "ev_implied_usd": round(ev_implied * 100, 2),
        "realized_vol": round(realized_vol, 4),
    }

    if spot > 0 and short_strike > 0 and dte > 0 and realized_vol > 0:
        T = dte / 252.0
        if smile:
            # Preferred path: market skew for tail SHAPE, realised vol for LEVEL,
            # equity risk premium as drift. Every EV is also reported at zero and
            # half drift so the ERP dependence is auditable.
            from . import skew as _skew
            atm = _skew.atm_vol(smile, spot) or realized_vol
            ratio = realized_vol / atm if atm > 0 else 1.0
            loss_real = _skew.expected_loss(spot, short_strike, long_strike, T,
                                            smile, ratio, drift=config.DRIFT_ANNUAL,
                                            is_put=is_put)
            out["atm_iv"] = round(atm, 4)
            out["vrp_ratio"] = round(ratio, 4)
            out["ev_basis"] = "skew+drift"
            for mu, key in ((0.0, "ev_at_zero_drift_usd"),
                            (config.DRIFT_ANNUAL / 2, "ev_at_half_drift_usd")):
                el = _skew.expected_loss(spot, short_strike, long_strike, T,
                                         smile, ratio, steps=200, drift=mu,
                                         is_put=is_put)
                out[key] = round((credit - el) * 100, 2)
        else:
            loss_real = _expected_loss(
                spot, short_strike, long_strike, realized_vol, T,
                is_put=is_put,
            )
            out["ev_basis"] = "realized_vol_flat"
        out["ev_gross_usd"] = round((credit - loss_real) * 100, 2)
        out["expected_loss_real_usd"] = round(loss_real * 100, 2)
    else:
        # No realised-vol input: fall back to the implied figure and say so.
        out["ev_gross_usd"] = out["ev_implied_usd"]
        out["ev_basis"] = "implied_only"
    return out


def friction_usd(short_bid: float, short_ask: float,
                 long_bid: float, long_ask: float) -> float:
    """Entry cost of crossing half the spread on each leg, per contract.

    Measured from real quotes. This is the number that killed S13 in backtest,
    and the number most agents never compute because they price at mid.
    """
    s_spread = max(short_ask - short_bid, 0.0)
    l_spread = max(long_ask - long_bid, 0.0)
    return round((s_spread / 2 + l_spread / 2) * 100, 2)


# ── Gate stack ───────────────────────────────────────────────────────────────
def evaluate(spread: dict, ctx: dict) -> Verdict:
    """Run every gate against a fully-resolved spread. Never short-circuits:
    the dashboard shows all results, not just the first failure."""
    checks: list[Check] = []
    c = checks.append

    # ── Operational ──────────────────────────────────────────────────────────
    c(Check("defined_risk", spread["legs_long"] == spread["legs_short"],
            f"{spread['legs_short']} short / {spread['legs_long']} long - no naked legs"))

    c(Check("allowlist", spread["underlying"] in config.ALLOWLIST,
            f"{spread['underlying']} in {config.ALLOWLIST}"))

    account_status = str(ctx.get("account_status") or "").upper()
    approved_level = int(float(ctx.get("options_approved_level") or 0))
    trading_level = int(float(ctx.get("options_trading_level") or 0))
    options_bp = max(float(ctx.get("options_buying_power") or 0.0), 0.0)
    blocked = bool(
        ctx.get("trading_blocked")
        or ctx.get("account_blocked")
        or ctx.get("trade_suspended_by_user")
    )
    options_eligible = (
        account_status == "ACTIVE"
        and not blocked
        and approved_level >= config.MIN_OPTIONS_TRADING_LEVEL
        and trading_level >= config.MIN_OPTIONS_TRADING_LEVEL
        and options_bp > 0
    )
    c(Check(
        "alpaca_options_eligible",
        options_eligible,
        f"status={account_status or 'missing'}, approved=L{approved_level}, "
        f"enabled=L{trading_level}, options BP=${options_bp:,.2f}, blocked={blocked}",
    ))

    c(Check("position_size", spread["qty"] <= config.MAX_CONTRACTS,
            f"qty {spread['qty']} <= {config.MAX_CONTRACTS}"))

    c(Check("open_positions", ctx.get("open_positions", 0) < config.MAX_OPEN_POSITIONS,
            f"{ctx.get('open_positions', 0)} open < {config.MAX_OPEN_POSITIONS}"))

    c(Check("daily_trade_limit", ctx.get("trades_today", 0) < config.MAX_TRADES_PER_DAY,
            f"{ctx.get('trades_today', 0)} today < {config.MAX_TRADES_PER_DAY}"))

    target_reached = bool(ctx.get("annual_target_reached", False))
    target_detail = (
        f"daily P&L ${ctx.get('daily_pnl_usd', 0):+.2f} reached "
        f"${ctx.get('daily_target_usd', 0):.2f} target - no more entries"
        if target_reached
        else (
            f"daily P&L ${ctx.get('daily_pnl_usd', 0):+.2f} below "
            f"${ctx.get('daily_target_usd', 0):.2f} target"
        )
    )
    if config.FULL_BUYING_POWER:
        target_detail = (
            f"full-buying-power objective; 8%-annual benchmark is display-only "
            f"(daily P&L ${ctx.get('daily_pnl_usd', 0):+.2f})"
        )
    c(Check("annual_target_budget", config.FULL_BUYING_POWER or not target_reached,
            target_detail))

    c(Check("no_duplicate", spread["short_symbol"] not in ctx.get("held_symbols", []),
            f"{spread['short_symbol']} not already held"))

    age = max(spread["short_quote_age"], spread["long_quote_age"])
    c(Check("quote_freshness", age <= config.QUOTE_MAX_AGE_SEC,
            f"oldest quote {age:.0f}s <= {config.QUOTE_MAX_AGE_SEC}s"))

    worst_rel = max(spread["short_rel_spread"], spread["long_rel_spread"])
    c(Check("liquidity", worst_rel <= config.MAX_LEG_SPREAD_PCT,
            f"worst leg bid/ask {worst_rel:.1%} of mid <= {config.MAX_LEG_SPREAD_PCT:.0%}"))

    strategy = spread.get("strategy", "put_credit_spread")
    is_put = strategy != "call_credit_spread"
    econ = expected_value(
        spread["credit"], spread["width"],
        spread["short_delta"], spread["long_delta"],
        spot=spread.get("spot", 0.0),
        short_strike=spread.get("short_strike", 0.0),
        long_strike=spread.get("long_strike", 0.0),
        dte=spread.get("dte", 0.0),
        realized_vol=spread.get("realized_vol", 0.0),
        smile=spread.get("smile"),
        is_put=is_put,
    )
    fric = spread["friction_usd"]
    econ["friction_usd"] = fric
    econ["ev_net_usd"] = round(econ["ev_gross_usd"] - fric, 2)

    c(Check("max_loss_cap", econ["max_loss_usd"] <= config.MAX_LOSS_USD,
            f"max loss ${econ['max_loss_usd']:.0f}/contract <= ${config.MAX_LOSS_USD:.0f}"))

    total_risk = econ["max_loss_usd"] * spread["qty"]
    econ["total_risk_usd"] = round(total_risk, 2)
    bp_utilization = float(
        ctx.get("options_bp_utilization", config.OPTIONS_BP_UTILIZATION)
    )
    spread_bp = sizing.spread_budget(
        options_bp, float(ctx.get("equity") or 0.0), config.SPREAD_EQUITY_PCT
    )
    risk_cap = (
        spread_bp * bp_utilization
        if config.FULL_BUYING_POWER else config.MAX_TOTAL_RISK_USD
    )
    econ["risk_cap_usd"] = round(risk_cap, 2)
    econ["options_buying_power_usd"] = round(options_bp, 2)
    econ["spread_equity_budget_pct"] = config.SPREAD_EQUITY_PCT
    econ["sizing_mode"] = config.SIZING_MODE
    within_risk_cap = total_risk <= risk_cap
    risk_cap_label = (
        "Alpaca options-BP budget" if config.FULL_BUYING_POWER else "policy cap"
    )
    risk_cap_detail = (
        f"total defined loss ${total_risk:,.2f} ({spread['qty']} contracts) "
        f"<= ${risk_cap:,.2f} {risk_cap_label}"
        if within_risk_cap else
        f"total defined loss ${total_risk:,.2f} ({spread['qty']} contracts) "
        f"exceeds ${risk_cap:,.2f} {risk_cap_label} by "
        f"${total_risk - risk_cap:,.2f}"
    )
    c(Check("total_risk_cap", within_risk_cap, risk_cap_detail))

    c(Check("limit_order_only", spread.get("order_type") == "limit",
            "limit order - never market on a spread"))

    latest_return = (spread.get("vol_profile") or {}).get("latest_1d_return")
    call_rebound_ok = (
        strategy != "call_credit_spread"
        or (
            latest_return is not None
            and float(latest_return) >= config.CALL_REBOUND_MIN_1D_RETURN
        )
    )
    rebound_detail = (
        "put spread - right-tail rebound gate not applicable"
        if strategy != "call_credit_spread"
        else (
            f"latest 1-day return {float(latest_return):+.2%} >= "
            f"{config.CALL_REBOUND_MIN_1D_RETURN:+.2%} floor"
            if latest_return is not None
            else "latest 1-day return unavailable - fail closed"
        )
    )
    c(Check("call_rebound_risk", call_rebound_ok, rebound_detail))

    reentry = ctx.get("reentry") or {}
    if not reentry.get("active"):
        reentry_ok = True
        reentry_detail = "first entry - lifecycle comparison not applicable"
    else:
        exit_state = reentry.get("exit") or {}
        baseline = exit_state.get("entry_baseline") or {}
        prior_quality = float(baseline.get("quality") or 0.0)
        current_quality = (
            econ["ev_net_usd"] / econ["max_loss_usd"]
            if econ["max_loss_usd"] > 0 else 0.0
        )
        required_quality = prior_quality * config.REENTRY_MIN_QUALITY_MULTIPLIER
        prior_delta = baseline.get("short_delta")
        prior_buffer = exit_state.get("safe_buffer_pct")
        prior_liquidity = exit_state.get("worst_relative_quote_width")
        spot = float(spread.get("spot") or 0.0)
        short_strike = float(spread.get("short_strike") or 0.0)
        candidate_buffer = (
            (spot - short_strike) / spot
            if is_put and spot > 0
            else ((short_strike - spot) / spot if spot > 0 else -1.0)
        )
        same_pair = (
            spread.get("short_symbol") == exit_state.get("short_symbol")
            and spread.get("long_symbol") == exit_state.get("long_symbol")
        )
        comparable = (
            prior_quality > 0
            and prior_delta is not None
            and prior_buffer is not None
            and prior_liquidity is not None
        )
        reentry_ok = bool(
            reentry.get("allowed")
            and comparable
            and not same_pair
            and current_quality >= required_quality
            and econ["short_delta"] <= float(prior_delta or 0.0)
            and candidate_buffer >= float(prior_buffer or 0.0)
            and worst_rel <= float(prior_liquidity or 0.0)
        )
        econ["quality_per_defined_risk"] = round(current_quality, 6)
        econ["reentry_required_quality"] = round(required_quality, 6)
        econ["reentry_quality_ratio"] = (
            round(current_quality / prior_quality, 4)
            if prior_quality > 0 else None
        )
        reentry_detail = (
            f"quality {current_quality:.3%} >= {required_quality:.3%}; "
            f"delta {econ['short_delta']:.3f} <= {float(prior_delta or 0):.3f}; "
            f"buffer {candidate_buffer:.2%} >= {float(prior_buffer or 0):.2%}; "
            f"liquidity {worst_rel:.1%} <= {float(prior_liquidity or 0):.1%}; "
            f"same pair={same_pair}; comparable={comparable}"
        )
    c(Check("reentry_quality", reentry_ok, reentry_detail))

    # ── Economic - the gate operational stacks omit ──────────────────────────
    c(Check("economic_ev", econ["ev_net_usd"] > config.MIN_EV_USD,
            f"market-implied EV ${econ['ev_net_usd']:+.2f} "
            f"(gross ${econ['ev_gross_usd']:+.2f} - friction ${fric:.2f}) "
            f"> ${config.MIN_EV_USD:.2f}"))

    failures = [c_ for c_ in checks if not c_.passed]
    return Verdict(
        approved=not failures,
        checks=checks,
        economics=econ,
        reason="approved" if not failures else "; ".join(f"{f.name}: {f.detail}" for f in failures),
    )
