"""Deterministic contract builder - intent in, real contracts out.

The LLM never names a strike. It gives a delta target and a width; this module
resolves that against the live chain via the Alpaca MCP server, reads the real
bid/ask and the real Greeks, and hands the gates a fully-priced spread.
"""
from __future__ import annotations

import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone

from . import config, mcp_client, skew
from .gates import expected_value, friction_usd
from .sizing import buying_power_contracts, kelly_contracts, target_contract_cap
from .intent import Intent


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _quote_age(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return 1e9


def _account_context() -> dict:
    """Sizing fields from the API - never from local state."""
    try:
        a = mcp_client.run(mcp_client.call("get_account_info")) or {}
        return {
            "equity": _f(a.get("equity")),
            "options_buying_power": _f(a.get("options_buying_power")),
        }
    except Exception:
        return {"equity": 0.0, "options_buying_power": 0.0}


_VOL_CACHE: dict[str, dict] = {}

# RiskMetrics standard. Chosen because it is the industry default for
# short-horizon vol forecasting, NOT because of the answer it produces here.
EWMA_LAMBDA = float(os.getenv("PACAPOUNCE_EWMA_LAMBDA", "0.94"))


def _closes(symbol: str, days: int = 120) -> list[float]:
    try:
        start = (date.today() - timedelta(days=days)).isoformat()
        res = mcp_client.run(mcp_client.call(
            "get_stock_bars", symbols=symbol, timeframe="1Day",
            start=start, feed="sip", limit=90))
        bars = ((res or {}).get("bars") or {}).get(symbol) or []
        return [float(b["c"]) for b in bars if b.get("c")]
    except Exception:
        return []


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def vol_profile(symbol: str) -> dict:
    """Realised volatility by method, so the choice is visible rather than buried.

    The gate's verdict is SENSITIVE to this number: a 20-day window and a 10-day
    window disagreed by 5-9 vol points on live data, which was the difference
    between trading and sitting out. Every estimate is reported alongside the one
    actually used, so the sensitivity is auditable instead of hidden.

    EWMA is the decision input. It is the RiskMetrics default for short-horizon
    forecasting and weights recent observations without a hard cutoff, which is
    the right shape for pricing a 2-7 day option. A fixed 20-day window used to
    forecast 2 days is a horizon mismatch.
    """
    if symbol in _VOL_CACHE:
        return _VOL_CACHE[symbol]
    closes = _closes(symbol)
    if len(closes) < 12:
        return {"ewma": 0.0, "windows": {}, "used": "unavailable"}
    rets = _log_returns(closes)

    windows = {}
    for w in (5, 10, 20, 40, 60):
        if len(rets) >= w:
            windows[f"{w}d"] = round(statistics.stdev(rets[-w:]) * math.sqrt(252), 4)

    seed = rets[:10]
    var = statistics.pvariance(seed) if len(seed) > 1 else rets[0] ** 2
    for r in rets[10:]:
        var = EWMA_LAMBDA * var + (1 - EWMA_LAMBDA) * r * r
    ewma = math.sqrt(max(var, 0.0)) * math.sqrt(252)

    profile = {
        "ewma": round(ewma, 4),
        "windows": windows,
        "used": f"ewma(lambda={EWMA_LAMBDA})",
        "n_returns": len(rets),
        "latest_1d_return": round(math.exp(rets[-1]) - 1.0, 6),
    }
    _VOL_CACHE[symbol] = profile
    return profile


def realized_vol(symbol: str) -> float:
    """The single number the gate uses. See vol_profile for the alternatives."""
    return vol_profile(symbol).get("ewma", 0.0)


def _leg(sym: str, snap: dict) -> dict | None:
    """Flatten one chain entry into bid/ask/delta, or None if unusable."""
    q = snap.get("latestQuote") or snap.get("latest_quote") or {}
    g = snap.get("greeks") or {}
    bid, ask = _f(q.get("bp")), _f(q.get("ap"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return {
        "symbol": sym,
        "strike": _strike_from_occ(sym),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "rel_spread": (ask - bid) / mid if mid > 0 else 1.0,
        "delta": abs(_f(g.get("delta"))),
        "iv": _f(snap.get("impliedVolatility") or snap.get("implied_volatility")),
        "quote_age": _quote_age(q.get("t")),
        "has_greeks": bool(g.get("delta")),
    }


def _strike_from_occ(sym: str) -> float:
    """OCC: ROOT + YYMMDD + C/P + strike*1000, zero-padded to 8."""
    return int(sym[-8:]) / 1000.0


def expiries_in_range(dte_min: int, dte_max: int, today: date) -> list[str]:
    return [(today + timedelta(days=n)).isoformat() for n in range(dte_min, dte_max + 1)]


def build(intent: Intent, spot: float, today: date,
          sizing_context: dict | None = None) -> tuple[dict | None, str]:
    """Resolve intent -> priced spread. Returns (spread, error)."""
    rvol = realized_vol(intent.underlying)
    is_put = intent.strategy == "put_credit_spread"
    opt_type = "put" if is_put else "call"
    width = intent.spread_width

    # Search a strike band wide enough to contain the delta target and the long leg.
    if is_put:
        lo, hi = spot * 0.90 - width, spot * 1.005
    else:
        lo, hi = spot * 0.995, spot * 1.10 + width

    for expiry in expiries_in_range(intent.dte_min, intent.dte_max, today):
        chain = mcp_client.run(mcp_client.call(
            "get_option_chain",
            underlying_symbol=intent.underlying,
            expiration_date=expiry,
            type=opt_type,
            strike_price_gte=round(lo, 2),
            strike_price_lte=round(hi, 2),
            limit=500,
        ))
        snaps = (chain or {}).get("snapshots") or {}
        if not snaps:
            continue

        legs = [l for l in (_leg(s, d) for s, d in snaps.items()) if l]
        legs = [l for l in legs if l["has_greeks"] and 0 < l["delta"] < 0.95]
        if len(legs) < 2:
            continue

        # Never price off a chain that violates no-arbitrage.
        sane, why = skew.chain_is_sane(legs, is_put=is_put)
        if not sane:
            return None, f"chain rejected for {expiry}: {why}"

        dte_est = max((date.fromisoformat(expiry) - today).days, 0)
        smile = skew.build_smile(
            spot, max(dte_est, 1) / 252, legs, is_put=is_put,
        )

        # Short leg: closest to the requested delta.
        short = min(legs, key=lambda l: abs(l["delta"] - intent.short_delta_target))
        # Long leg: exactly `width` further out of the money.
        target = short["strike"] - width if is_put else short["strike"] + width
        long_ = min(legs, key=lambda l: abs(l["strike"] - target))
        if long_["symbol"] == short["symbol"]:
            continue

        actual_width = abs(short["strike"] - long_["strike"])
        if actual_width <= 0:
            continue

        # Alpaca receives a two-decimal limit price.  Normalize the credit
        # before sizing so sizing, gates, and execution all evaluate the exact
        # same executable price (rather than sizing from a half-cent midpoint).
        credit = round(short["mid"] - long_["mid"], 2)
        if credit <= 0:
            continue

        fric = friction_usd(short["bid"], short["ask"], long_["bid"], long_["ask"])
        # Size before gating so the gate validates the size we will actually send.
        account = sizing_context or _account_context()
        equity = _f(account.get("equity"))
        options_buying_power = _f(account.get("options_buying_power"))
        ksz = kelly_contracts(equity, spot, short["strike"], long_["strike"],
                              credit, rvol, max(dte_est, 1) / 252, friction=fric,
                              is_put=is_put,
                              drift=config.DRIFT_ANNUAL,
                              max_n=config.MAX_CONTRACTS)
        sizing_econ = expected_value(
            credit, actual_width, short["delta"], long_["delta"],
            spot=spot, short_strike=short["strike"], long_strike=long_["strike"],
            dte=dte_est, realized_vol=rvol, smile=smile,
            is_put=is_put,
        )
        expected_net = sizing_econ["ev_gross_usd"] - fric
        if config.FULL_BUYING_POWER:
            bp_utilization = float(
                account.get("options_bp_utilization", config.OPTIONS_BP_UTILIZATION)
            )
            ksz = buying_power_contracts(
                options_buying_power,
                sizing_econ["max_loss_usd"],
                bp_utilization,
                config.MAX_CONTRACTS,
            )
            qty = ksz["contracts"]
        else:
            target_cap = target_contract_cap(
                equity, expected_net, config.ANNUAL_RETURN_TARGET,
                config.TRADING_DAYS_PER_YEAR,
            )
            qty = max(min(
                ksz.get("contracts", 0),
                target_cap["contracts"],
                config.MAX_CONTRACTS,
            ), 0)
            ksz["mode"] = "kelly"
            ksz["annual_target_cap"] = target_cap
        if qty < 1:
            return None, (
                "Alpaca options buying power cannot collateralize one contract"
                if config.FULL_BUYING_POWER
                else f"Kelly sizes this at 0 contracts "
                     f"(full Kelly {ksz.get('full_kelly')}) - edge too thin to bear risk"
            )

        dte = max((date.fromisoformat(expiry) - today).days, 0)
        return {
            "dte": dte,
            "realized_vol": rvol,
            "smile": smile,
            "vol_profile": vol_profile(intent.underlying),
            "underlying": intent.underlying,
            "strategy": intent.strategy,
            "expiry": expiry,
            "qty": qty,
            "legs_short": 1,
            "legs_long": 1,
            "order_type": "limit",
            "short_symbol": short["symbol"],
            "long_symbol": long_["symbol"],
            "short_strike": short["strike"],
            "long_strike": long_["strike"],
            "width": actual_width,
            "credit": credit,
            "short_delta": short["delta"],
            "long_delta": long_["delta"],
            "short_iv": short["iv"],
            "short_rel_spread": short["rel_spread"],
            "long_rel_spread": long_["rel_spread"],
            "short_quote_age": short["quote_age"],
            "long_quote_age": long_["quote_age"],
            "friction_usd": fric,
            "kelly": ksz,
            "spot": spot,
        }, ""

    return None, f"no tradeable {opt_type} chain for {intent.underlying} in DTE {intent.dte_min}-{intent.dte_max}"
