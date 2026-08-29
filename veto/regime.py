"""Compact MCP-derived market regime features for the LLM intent brief.

The model receives only a small, timestamped summary. Contract selection,
pricing, sizing, and approval remain deterministic and independently refresh
the live option chain after the model returns an intent.
"""
from __future__ import annotations

import math
import statistics
import time
from datetime import date, timedelta

from . import config, mcp_client


_CACHE: dict = {}


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _completed_closes(payload, symbol: str, broker_date: date) -> list[float]:
    bars = ((payload or {}).get("bars") or {}).get(symbol) or []
    by_day: dict[str, float] = {}
    for bar in bars:
        day = str((bar or {}).get("t") or "")[:10]
        close = _f((bar or {}).get("c"))
        if day and day < broker_date.isoformat() and close > 0:
            by_day[day] = close
    return [by_day[day] for day in sorted(by_day)]


def _history_stats(closes: list[float], spot: float) -> dict:
    result: dict[str, float] = {}
    if spot > 0 and closes:
        result["return_1d"] = spot / closes[-1] - 1.0
    if spot > 0 and len(closes) >= 5:
        result["return_5d"] = spot / closes[-5] - 1.0
    if len(closes) >= 21:
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        result["rv20"] = statistics.stdev(returns[-20:]) * math.sqrt(252)
    return result


def _contract_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    rows = (payload or {}).get("option_contracts") if isinstance(payload, dict) else []
    return [row for row in (rows or []) if isinstance(row, dict)]


def _select_atm_contracts(
    rows: list[dict], spot: float, broker_date: date, target_dte: int = 3
) -> tuple[list[str], str | None, int | None]:
    eligible = []
    for row in rows:
        try:
            expiry = date.fromisoformat(str(row.get("expiration_date")))
        except ValueError:
            continue
        dte = (expiry - broker_date).days
        if (
            max(config.DTE_MIN, 1) <= dte <= config.DTE_MAX
            and row.get("tradable") is not False
            and _f(row.get("strike_price")) > 0
            and str(row.get("type") or "").lower() in {"call", "put"}
        ):
            eligible.append((row, expiry, dte))
    if not eligible:
        return [], None, None

    target = min(max(target_dte, max(config.DTE_MIN, 1)), config.DTE_MAX)
    expiries = {(expiry, dte) for _, expiry, dte in eligible}
    chosen_expiry, chosen_dte = min(
        expiries,
        key=lambda item: (abs(item[1] - target), -item[1]),
    )
    at_expiry = [row for row, expiry, _ in eligible if expiry == chosen_expiry]
    selected = []
    for option_type in ("call", "put"):
        side = [
            row for row in at_expiry
            if str(row.get("type") or "").lower() == option_type
        ]
        if side:
            selected.append(min(
                side,
                key=lambda row: abs(_f(row.get("strike_price")) - spot),
            )["symbol"])
    return selected, chosen_expiry.isoformat(), chosen_dte


def _snapshot_map(payload) -> dict[str, dict]:
    if not isinstance(payload, dict):
        return {}
    snapshots = payload.get("snapshots")
    if isinstance(snapshots, dict):
        return snapshots
    return payload


def _refresh(spots: dict[str, float], broker_date: date) -> dict[str, dict]:
    start = (broker_date - timedelta(days=70)).isoformat()
    end = (broker_date + timedelta(days=1)).isoformat()
    calls: list[tuple[str, dict]] = [
        ("get_stock_bars", {
            "symbols": ",".join(config.ALLOWLIST),
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "adjustment": "all",
            "feed": "sip",
            "limit": 10_000,
            "sort": "asc",
        }),
    ]
    for symbol in config.ALLOWLIST:
        spot = spots.get(symbol, 0.0)
        calls.append(("get_option_contracts", {
            "underlying_symbols": symbol,
            "status": "active",
            "expiration_date_gte": (
                broker_date + timedelta(days=max(config.DTE_MIN, 1))
            ).isoformat(),
            "expiration_date_lte": (
                broker_date + timedelta(days=config.DTE_MAX)
            ).isoformat(),
            "strike_price_gte": round(max(spot - 3.0, 0.01), 2),
            "strike_price_lte": round(spot + 3.0, 2),
            "limit": 500,
        }))

    payloads = mcp_client.run(mcp_client.call_many(calls))
    bars_payload = payloads[0] if payloads else {}
    contract_payloads = payloads[1:]
    raw: dict[str, dict] = {}
    selected_by_symbol: dict[str, list[str]] = {}
    selected_symbols: list[str] = []

    for symbol, contracts in zip(config.ALLOWLIST, contract_payloads):
        spot = spots.get(symbol, 0.0)
        chosen, expiry, dte = _select_atm_contracts(
            _contract_rows(contracts), spot, broker_date
        )
        selected_by_symbol[symbol] = chosen
        selected_symbols.extend(chosen)
        raw[symbol] = {
            "closes": _completed_closes(bars_payload, symbol, broker_date),
            "atm_expiry": expiry,
            "atm_dte": dte,
        }

    snapshots = {}
    if selected_symbols:
        payload = mcp_client.run(mcp_client.call(
            "get_option_snapshot",
            symbols=",".join(selected_symbols),
            feed=config.OPTIONS_FEED,
        ))
        snapshots = _snapshot_map(payload)

    for symbol, selected in selected_by_symbol.items():
        ivs = []
        timestamps = []
        for option_symbol in selected:
            snapshot = snapshots.get(option_symbol) or {}
            iv = _f(
                snapshot.get("impliedVolatility")
                or snapshot.get("implied_volatility")
            )
            if iv > 0:
                ivs.append(iv)
            quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
            if quote.get("t"):
                timestamps.append(str(quote["t"]))
        raw.setdefault(symbol, {})["atm_iv"] = (
            sum(ivs) / len(ivs) if ivs else None
        )
        raw[symbol]["iv_asof"] = min(timestamps) if timestamps else None
    return raw


def snapshot(spots: dict[str, float], broker_date: date) -> dict[str, dict]:
    """Return current-spot features with a bounded cache for expensive IV data."""
    global _CACHE
    now = time.monotonic()
    cache_key = (broker_date.isoformat(), tuple(config.ALLOWLIST))
    if _CACHE.get("key") != cache_key or now >= _CACHE.get("expires", 0.0):
        try:
            raw = _refresh(spots, broker_date)
        except Exception:
            raw = {}
        _CACHE = {
            "key": cache_key,
            "expires": now + max(config.REGIME_CACHE_SEC, 1),
            "raw": raw,
        }

    result: dict[str, dict] = {}
    for symbol in config.ALLOWLIST:
        spot = spots.get(symbol, 0.0)
        raw = (_CACHE.get("raw") or {}).get(symbol) or {}
        row = {"spot": spot, **_history_stats(raw.get("closes") or [], spot)}
        for key in ("atm_iv", "atm_expiry", "atm_dte", "iv_asof"):
            if raw.get(key) is not None:
                row[key] = raw[key]
        if row.get("atm_iv") and row.get("rv20"):
            row["iv_rv"] = row["atm_iv"] / row["rv20"]
        result[symbol] = row
    return result


def format_feature(symbol: str, row: dict) -> str:
    fields = [f"spot {_f(row.get('spot')):.2f}"]
    if row.get("return_1d") is not None:
        fields.append(f"1D {row['return_1d']:+.2%}")
    if row.get("return_5d") is not None:
        fields.append(f"5D {row['return_5d']:+.2%}")
    if row.get("rv20") is not None:
        fields.append(f"RV20 {row['rv20']:.2%}")
    if row.get("atm_iv") is not None:
        fields.append(f"ATM IV({row.get('atm_dte', '?')}D) {row['atm_iv']:.2%}")
    if row.get("iv_rv") is not None:
        fields.append(f"IV/RV {row['iv_rv']:.2f}")
    if row.get("iv_asof"):
        fields.append(f"IV quote {row['iv_asof']}")
    return f"{symbol}: " + " | ".join(fields)
