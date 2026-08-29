#!/usr/bin/env python
"""Gate validation harness - does the gate actually separate good from bad?

A gate that has never been tested against outcomes is an assertion, not a
control. This harness scores BOTH the trades the gate approved and the trades it
vetoed, then asks whether the split beats chance.

Scoring the vetoed set is the whole point. It is the counterfactual, and it is
the step every naive version of this skips: if you only measure what you traded,
you cannot tell a good filter from a lucky one.

NON-CIRCULARITY
    The gate decides using IMPLIED information (option delta, derived from
    implied vol). Outcomes are generated from a separate REALISED vol, related to
    implied only through a noisy variance risk premium. The gate never sees the
    realised path. If it still separates, the separation is real.

Usage:
    python scripts/validate_gate.py                    # 400 trials, 1000 perms
    python scripts/validate_gate.py --trials 2000
    python scripts/validate_gate.py --seed 7 --perms 5000
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from veto import config, gates  # noqa: E402
from veto.pnl import spread_pnl_at_expiry  # noqa: E402

TRADING_DAYS = 252


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def put_delta(spot: float, strike: float, sigma: float, T: float) -> float:
    """|delta| of a European put - the market's implied P(finishing ITM)."""
    if sigma <= 0 or T <= 0:
        return 1.0 if strike > spot else 0.0
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return abs(_norm_cdf(d1) - 1)


def fair_credit(width: float, ds: float, dl: float) -> float:
    """Credit at which this EV model is exactly zero (see gates.expected_value)."""
    return width * (ds + dl) / 2


def trial(rng: random.Random) -> dict:
    """One independent trade opportunity, priced and then resolved."""
    spot = 640.0
    dte = rng.randint(1, 7)
    T = dte / TRADING_DAYS

    # Realised vol is what the world does. Implied is what the market charges.
    # The premium between them is real on index options but noisy, and it goes
    # negative often enough to matter.
    sigma_real = rng.uniform(0.08, 0.32)
    vrp = rng.gauss(0.06, 0.12)
    sigma_imp = max(sigma_real * (1 + vrp), 0.02)

    width = rng.choice([1.0, 2.0, 3.0, 5.0, 10.0])
    target_delta = rng.uniform(0.08, 0.35)

    # Strike implied by the delta target under IMPLIED vol (what a desk quotes).
    z = _norm_cdf_inv(1 - target_delta)
    short_k = round(spot * math.exp(-z * sigma_imp * math.sqrt(T)
                                    + 0.5 * sigma_imp ** 2 * T), 0)
    long_k = short_k - width
    if long_k <= 0:
        long_k = max(short_k - 1.0, 1.0)
        width = short_k - long_k

    ds = put_delta(spot, short_k, sigma_imp, T)
    dl = put_delta(spot, long_k, sigma_imp, T)

    # Market prices around fair, sometimes rich, sometimes cheap.
    credit = round(max(fair_credit(width, ds, dl) * rng.uniform(0.70, 1.35), 0.01), 2)

    # Friction scales with width and thins out on tight, liquid strikes.
    leg_spread = max(0.01, min(0.10, 0.02 * width * rng.uniform(0.5, 1.5)))
    friction = gates.friction_usd(credit, credit + leg_spread,
                                  credit / 2, credit / 2 + leg_spread)

    spread = {
        "underlying": "SPY", "strategy": "put_credit_spread", "qty": 1,
        "legs_short": 1, "legs_long": 1, "order_type": "limit",
        "short_symbol": f"SYN{short_k:.0f}", "long_symbol": f"SYN{long_k:.0f}",
        "short_strike": short_k, "long_strike": long_k, "width": width,
        "credit": credit, "short_delta": ds, "long_delta": dl,
        "short_rel_spread": leg_spread / max(credit, 0.01),
        "long_rel_spread": leg_spread / max(credit / 2, 0.01),
        "short_quote_age": 5, "long_quote_age": 5,
        "friction_usd": friction, "spot": spot,
    }
    verdict = gates.evaluate(spread, {
        "open_positions": 0,
        "trades_today": 0,
        "held_symbols": [],
        "account_status": "ACTIVE",
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
        "options_approved_level": 3,
        "options_trading_level": 3,
        "options_buying_power": 1_000_000.0,
    })

    # OUTCOME - drawn from REALISED vol. The gate never saw this.
    drift = -0.5 * sigma_real ** 2 * T
    shock = rng.gauss(0, sigma_real * math.sqrt(T))
    s_expiry = spot * math.exp(drift + shock)

    pnl = spread_pnl_at_expiry(short_k, long_k, credit, s_expiry, is_put=True) - friction

    return {
        "approved": verdict.approved,
        "pnl": round(pnl, 2),
        "ev_net": verdict.economics.get("ev_net_usd"),
        "width": width,
        "credit": credit,
        "operational_only": all(c.passed for c in verdict.checks
                                if c.name != "economic_ev"),
    }


def _norm_cdf_inv(p: float) -> float:
    """Acklam's inverse normal CDF - adequate for strike placement."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def permutation_test(pnls: list[float], labels: list[bool],
                     perms: int, rng: random.Random) -> dict:
    """How often does a RANDOM split of these same trades separate this well?

    This is the test that matters. With few approvals the approved set can beat
    the vetoed set by luck alone; shuffling the labels measures exactly that.
    """
    approved = [p for p, a in zip(pnls, labels) if a]
    vetoed = [p for p, a in zip(pnls, labels) if not a]
    if not approved or not vetoed:
        return {"error": "one side is empty - cannot test separation"}

    observed = statistics.mean(approved) - statistics.mean(vetoed)
    n_app = len(approved)
    pool = list(pnls)
    hits = 0
    for _ in range(perms):
        rng.shuffle(pool)
        diff = statistics.mean(pool[:n_app]) - statistics.mean(pool[n_app:])
        if diff >= observed:
            hits += 1
    return {
        "mean_approved": round(statistics.mean(approved), 2),
        "mean_vetoed": round(statistics.mean(vetoed), 2),
        "observed_separation": round(observed, 2),
        "permutations": perms,
        "p_value": round((hits + 1) / (perms + 1), 5),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the gate against outcomes")
    ap.add_argument("--trials", type=int, default=400)
    ap.add_argument("--perms", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    results = [trial(rng) for _ in range(a.trials)]

    pnls = [r["pnl"] for r in results]
    labels = [r["approved"] for r in results]
    perm = permutation_test(pnls, labels, a.perms, random.Random(a.seed + 1))

    approved = [r for r in results if r["approved"]]
    vetoed = [r for r in results if not r["approved"]]

    # What would an operational-only gate stack have done with the same trades?
    op_only = [r for r in results if r["operational_only"]]

    report = {
        "trials": a.trials,
        "seed": a.seed,
        "approved": len(approved),
        "vetoed": len(vetoed),
        "pass_rate": round(len(approved) / a.trials, 4),
        "total_pnl_if_traded_everything": round(sum(pnls), 2),
        "total_pnl_gate_approved_only": round(sum(r["pnl"] for r in approved), 2),
        "total_pnl_operational_gate_only": round(sum(r["pnl"] for r in op_only), 2),
        "permutation_test": perm,
        "gate_version": config.GATE_VERSION,
    }

    # Persist before branching - --json used to return early and leave a stale file.
    out = ROOT / "data" / "gate_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if a.json:
        print(json.dumps(report, indent=2))
        return 0

    print("=" * 66)
    print(f"GATE VALIDATION  -  {a.trials} independent trade opportunities")
    print("=" * 66)
    print(f"  approved {len(approved):>5}   vetoed {len(vetoed):>5}   "
          f"pass rate {report['pass_rate']:.1%}")
    print()
    print("  Counterfactual - what happened to the trades it REJECTED:")
    if "error" in perm:
        print(f"    {perm['error']}")
    else:
        print(f"    mean P&L, approved trades   ${perm['mean_approved']:+8.2f}")
        print(f"    mean P&L, vetoed trades     ${perm['mean_vetoed']:+8.2f}")
        print(f"    separation                  ${perm['observed_separation']:+8.2f}")
        print(f"    permutation p-value          {perm['p_value']:.5f}  "
              f"({perm['permutations']} shuffles)")
    print()
    print("  Total P&L under three policies:")
    print(f"    trade everything             ${report['total_pnl_if_traded_everything']:+10,.2f}")
    print(f"    operational gates only       ${report['total_pnl_operational_gate_only']:+10,.2f}")
    print(f"    full gate (with economic)    ${report['total_pnl_gate_approved_only']:+10,.2f}")
    print("=" * 66)

    print(f"  written to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
