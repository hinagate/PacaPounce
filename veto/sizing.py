"""Position sizing for long-run Kelly and paper-tournament objectives.

Full-buying-power mode maximizes defined exposure against the broker's options
collateral budget. The long-run alternative asks a different question:
how large can this trade honestly be?

"As big as it can bear" has a precise answer: the size that maximises expected
log growth of capital (Kelly). Larger than that is not more aggressive, it is
arithmetically worse - expected growth turns DOWN past the Kelly point, and past
2x Kelly it goes negative even with a positive edge.

Kelly assumes you know the true distribution. We do not - real-world vol is
estimated, and the estimate moved 5 vol points depending on the lookback. Under
parameter uncertainty the standard response is FRACTIONAL Kelly: a quarter-Kelly
bet gives ~44% of the growth for ~25% of the drawdown, and it degrades
gracefully when the edge estimate is wrong.
"""
from __future__ import annotations

import math
import os

KELLY_FRACTION = float(os.getenv("PACAPOUNCE_KELLY_FRACTION", "0.25"))
MAX_EQUITY_AT_RISK = float(os.getenv("PACAPOUNCE_MAX_EQUITY_AT_RISK", "0.02"))


def spread_budget(options_buying_power: float, equity: float,
                  spread_equity_pct: float) -> float:
    """Defined loss the credit-spread lane may carry, in dollars.

    Full-buying-power sizing otherwise consumes the whole broker budget on the
    first approved spread, leaving the 15:45 long-call lane unable to place an
    order at all. The share is taken from equity rather than from remaining
    buying power so a lane's budget does not depend on which lane filled first.
    Live options buying power still bounds it: this is a ceiling, not a grant.
    """
    ceiling = max(equity, 0.0) * min(max(spread_equity_pct, 0.0), 1.0)
    return min(max(options_buying_power, 0.0), ceiling)


def option_mr_budget(options_buying_power: float, equity: float,
                     total_premium_pct: float,
                     deployed_premium: float = 0.0) -> float:
    """Premium the long-call lane may still deploy, in dollars."""
    ceiling = max(equity, 0.0) * min(max(total_premium_pct, 0.0), 1.0)
    remaining = ceiling - max(deployed_premium, 0.0)
    return max(min(max(options_buying_power, 0.0), remaining), 0.0)


def buying_power_contracts(options_buying_power: float,
                           max_loss_per_contract: float,
                           utilization: float = 1.0,
                           max_n: int = 1000) -> dict:
    """Use the largest integer size Alpaca's options BP can collateralize.

    This is intentionally an arithmetic-P&L tournament objective, not a Kelly
    objective.  It never creates buying power: the broker-reported remaining
    options buying power is the source of truth and the gate rechecks it.
    """
    usable = max(options_buying_power, 0.0) * min(max(utilization, 0.0), 1.0)
    if max_loss_per_contract <= 0 or max_n < 1:
        contracts = 0
    else:
        contracts = min(int(usable // max_loss_per_contract), max_n)
    total_risk = contracts * max(max_loss_per_contract, 0.0)
    used_pct = total_risk / options_buying_power if options_buying_power > 0 else 0.0
    return {
        "mode": "full_buying_power",
        "contracts": contracts,
        "options_buying_power": round(max(options_buying_power, 0.0), 2),
        "utilization": utilization,
        "usable_buying_power": round(usable, 2),
        "max_loss_per_contract": round(max_loss_per_contract, 2),
        "total_risk": round(total_risk, 2),
        "buying_power_used_pct": round(used_pct, 4),
    }


def daily_return_target(annual_target: float = 0.08,
                        trading_days: int = 252) -> float:
    """Geometric daily return that compounds to ``annual_target``."""
    if annual_target <= -1 or trading_days <= 0:
        raise ValueError("annual_target must exceed -100% and trading_days must be positive")
    return (1.0 + annual_target) ** (1.0 / trading_days) - 1.0


def target_contract_cap(equity: float, expected_pnl_per_contract: float,
                        annual_target: float = 0.08,
                        trading_days: int = 252) -> dict:
    """Contracts needed for one opportunity to meet the daily objective.

    This cap can only reduce fractional-Kelly size; it never manufactures an
    edge or bypasses the existing risk caps. A non-positive expected value gets
    a zero cap and will also fail the economic gate.
    """
    daily_rate = daily_return_target(annual_target, trading_days)
    daily_target_usd = max(equity, 0.0) * daily_rate
    contracts = (
        max(math.ceil(daily_target_usd / expected_pnl_per_contract), 1)
        if equity > 0 and expected_pnl_per_contract > 0
        else 0
    )
    return {
        "contracts": contracts,
        "annual_target": annual_target,
        "daily_rate": daily_rate,
        "daily_target_usd": round(daily_target_usd, 2),
        "expected_pnl_per_contract": round(expected_pnl_per_contract, 2),
    }


def _lognormal_grid(spot: float, sigma: float, T: float, steps: int = 2000,
                    drift: float = 0.0):
    """Discretised real-world density of the underlying at expiry."""
    if sigma <= 0 or T <= 0:
        return [(spot, 1.0)]
    lo = spot * math.exp(-6 * sigma * math.sqrt(T))
    hi = spot * math.exp(6 * sigma * math.sqrt(T))
    dx = (hi - lo) / steps
    var = sigma * sigma * T
    grid = []
    for i in range(steps):
        s = lo + (i + 0.5) * dx
        z = math.log(s / spot) - (drift - 0.5 * sigma * sigma) * T
        density = math.exp(-(z * z) / (2 * var)) / (s * math.sqrt(2 * math.pi * var))
        grid.append((s, density * dx))
    return grid


def _payoff(s: float, short_k: float, long_k: float, credit: float,
            is_put: bool = True) -> float:
    """Per-contract payoff in dollars at expiry."""
    width = abs(short_k - long_k)
    intrinsic = (max(short_k - s, 0.0) if is_put else max(s - short_k, 0.0))
    return (credit - min(intrinsic, width)) * 100


def kelly_contracts(equity: float, spot: float, short_k: float, long_k: float,
                    credit: float, sigma: float, T: float,
                    friction: float = 0.0, is_put: bool = True,
                    drift: float = 0.0,
                    max_n: int = 200) -> dict:
    """Contracts that maximise expected log growth, then scaled by KELLY_FRACTION.

    Searched directly over integer contract counts rather than solved in closed
    form: the payoff is a capped piecewise function, not a simple binary bet, so
    the textbook Kelly formula does not apply.
    """
    if equity <= 0 or sigma <= 0 or T <= 0:
        return {"contracts": 0, "reason": "missing inputs"}

    grid = _lognormal_grid(spot, sigma, T, drift=drift)
    payoffs = [(_payoff(s, short_k, long_k, credit, is_put) - friction, p)
               for s, p in grid]

    max_loss_per_contract = (abs(short_k - long_k) - credit) * 100 + friction

    best_n, best_growth = 0, 0.0
    for n in range(1, max_n + 1):
        if n * max_loss_per_contract >= equity:
            break                       # ruin is possible; stop
        growth = 0.0
        ok = True
        for pay, prob in payoffs:
            wealth = equity + n * pay
            if wealth <= 0:
                ok = False
                break
            growth += prob * math.log(wealth / equity)
        if not ok:
            break
        if growth > best_growth:
            best_n, best_growth = n, growth

    full_kelly = best_n
    sized = max(int(full_kelly * KELLY_FRACTION), 0)

    # Hard cap: never put more than MAX_EQUITY_AT_RISK of the account on one trade.
    cap_by_equity = int((equity * MAX_EQUITY_AT_RISK) / max(max_loss_per_contract, 1))
    final = max(min(sized, cap_by_equity), 0)

    return {
        "contracts": final,
        "full_kelly": full_kelly,
        "kelly_fraction": KELLY_FRACTION,
        "capped_by_equity": sized > cap_by_equity,
        "max_loss_per_contract": round(max_loss_per_contract, 2),
        "total_risk": round(final * max_loss_per_contract, 2),
        "risk_pct_of_equity": round(final * max_loss_per_contract / equity, 4),
        "expected_growth_at_full_kelly": round(best_growth, 6),
    }


def outcome_distribution(n_contracts: int, spot: float, short_k: float,
                         long_k: float, credit: float, sigma: float, T: float,
                         friction: float = 0.0, is_put: bool = True,
                         drift: float = 0.0) -> dict:
    """The full P&L distribution for a sized position, not just its mean.

    A point estimate of expected P&L is close to useless for a short window: the
    spread of outcomes dwarfs the mean. Reporting percentiles is the honest way
    to say how big this trade actually is.
    """
    grid = _lognormal_grid(spot, sigma, T, drift=drift)
    rows = sorted(((_payoff(s, short_k, long_k, credit, is_put) - friction) * n_contracts, p)
                  for s, p in grid)

    total = sum(p for _, p in rows) or 1.0
    mean = sum(v * p for v, p in rows) / total

    def pct(target: float) -> float:
        acc = 0.0
        for v, p in rows:
            acc += p / total
            if acc >= target:
                return v
        return rows[-1][0]

    p_profit = sum(p for v, p in rows if v > 0) / total
    return {
        "contracts": n_contracts,
        "mean": round(mean, 2),
        "p05": round(pct(0.05), 2),
        "p25": round(pct(0.25), 2),
        "median": round(pct(0.50), 2),
        "p75": round(pct(0.75), 2),
        "p95": round(pct(0.95), 2),
        "worst": round(rows[0][0], 2),
        "best": round(rows[-1][0], 2),
        "prob_profit": round(p_profit, 4),
    }
