"""Options-only NDX30 long-call mean-reversion strategy for Paper staging.

The independently tested underlying signal is observed once at 15:45 ET:
price above a rising SMA200 and Wilder RSI(2) below 10.  Competition execution
is a liquid 14-30 DTE long call near 0.70 delta, never stock.  Premium is sized
from a 2x ATR14 underlying stop and capped as a percentage of equity.  Exit
when the stop is breached, the 15:30 completed-bar price recovers above EMA5,
or the third normal session is reached.

Python calculates every number and resolves the exact OCC contract. Poe sees
only the top executable candidate and timestamped Alpaca MCP news; it may veto
event risk and write the thesis. It never sizes, prices, selects a contract, or
chooses an exit.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

from . import config, llm, mcp_client, ratchet, session, sizing as sizing_mod

ET = ZoneInfo("America/New_York")
ENTRY_PREFIX = "paca-callmr-open-"
EXIT_PREFIX = "paca-callmr-exit-"
OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
# 15-minute bars in one 09:30-16:00 regular session, used to size bar windows.
INTRADAY_BARS_PER_SESSION = 26
FINAL_ORDER_STATES = {
    "canceled", "done_for_day", "expired", "filled", "rejected", "replaced",
    "stopped", "suspended",
}
FAILED_ORDER_STATES = FINAL_ORDER_STATES - {"filled"}


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rows(payload, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [row for row in payload[key] if isinstance(row, dict)]
    return []


def _bar_map(payload, symbol: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    bars = payload.get("bars") or {}
    if isinstance(bars, dict):
        value = bars.get(symbol) or []
        return [row for row in value if isinstance(row, dict)]
    return []


def _timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except (TypeError, ValueError):
        return None


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        os.replace(raw_path, path)
    finally:
        try:
            os.unlink(raw_path)
        except FileNotFoundError:
            pass


def load_state() -> dict:
    try:
        state = json.loads(config.OPTION_MR_STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    _atomic_json(config.OPTION_MR_STATE_FILE, state)


def log(kind: str, **payload) -> dict:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": "NDX30_CALL_MR_01",
        "kind": kind,
        **payload,
    }
    config.OPTION_MR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with config.OPTION_MR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
    return row


def load_log() -> list[dict]:
    try:
        lines = config.OPTION_MR_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            continue
    return rows


def wilder_rsi(closes: list[float], period: int = 2) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = fmean(gains[:period])
    avg_loss = fmean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def ema(closes: list[float], period: int = 5) -> float | None:
    if len(closes) < period:
        return None
    value = fmean(closes[:period])
    alpha = 2.0 / (period + 1.0)
    for close in closes[period:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def atr14(daily: list[dict], current: dict | None = None) -> float | None:
    bars = list(daily)
    if current:
        bars.append(current)
    if len(bars) < 15:
        return None
    true_ranges = []
    for previous, bar in zip(bars, bars[1:]):
        high, low, previous_close = _f(bar.get("h")), _f(bar.get("l")), _f(previous.get("c"))
        if min(high, low, previous_close) <= 0:
            continue
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return fmean(true_ranges[-14:]) if len(true_ranges) >= 14 else None


def deployed_premium(state: dict) -> float:
    """Premium already committed to open or in-flight long calls."""
    total = 0.0
    for record in (state.get("positions") or {}).values():
        if not isinstance(record, dict):
            continue
        if record.get("status") in {"entry_pending", "open", "exit_pending"}:
            total += _f(record.get("max_premium"))
    return total


def option_position_size(equity: float, options_buying_power: float,
                         premium: float, delta: float, atr: float,
                         premium_budget: float | None = None) -> dict:
    """Size a long call against the active sizing objective.

    ``risk_budget`` sizes from the first-order option loss expected if the
    underlying reaches its deterministic 2x ATR stop. Because listed options
    cannot be fractional, one contract may use the separately bounded
    one-contract risk cap.

    ``tournament`` sizes from the premium budget instead. A four-session P&L
    leaderboard rewards the upper tail rather than expected value, and a long
    call is the only structure here with upside convexity. The objective change
    is explicit rather than smuggled in by inflating the risk budget: the
    modeled stop is still computed and recorded, it just is not the input.
    In both modes the full premium paid remains the legal maximum loss.
    """
    premium_per_contract = premium * 100
    modeled_stop_loss = min(
        premium_per_contract,
        config.OPTION_MR_STOP_ATR_MULTIPLE * atr * abs(delta) * 100,
    )
    position_cap = equity * config.OPTION_MR_MAX_PREMIUM_PCT
    budget = position_cap if premium_budget is None else min(position_cap, premium_budget)
    base = {
        "mode": config.OPTION_MR_SIZING_MODE,
        "premium_per_contract": round(premium_per_contract, 2),
        "modeled_stop_loss_per_contract": round(modeled_stop_loss, 2),
        "risk_budget": round(equity * config.OPTION_MR_EQUITY_RISK_PCT, 2),
        "one_contract_risk_cap": round(equity * config.OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT, 2),
        "premium_cap": round(budget, 2),
        "position_premium_cap": round(position_cap, 2),
        "discrete_one_contract": False,
    }
    if min(equity, options_buying_power, premium_per_contract, modeled_stop_loss) <= 0:
        return {**base, "contracts": 0}

    by_broker = math.floor(options_buying_power / premium_per_contract)
    by_premium = math.floor(budget / premium_per_contract)
    if config.OPTION_MR_TOURNAMENT:
        qty = max(0, min(by_premium, by_broker, config.MAX_CONTRACTS))
        return {**base, "contracts": qty}

    by_risk = math.floor(
        equity * config.OPTION_MR_EQUITY_RISK_PCT / modeled_stop_loss
    )
    qty = max(0, min(by_risk, by_premium, by_broker))
    discrete_one_contract = (
        qty == 0
        and modeled_stop_loss <= base["one_contract_risk_cap"]
        and by_premium >= 1
        and by_broker >= 1
    )
    if discrete_one_contract:
        qty = 1
    return {**base, "contracts": qty, "discrete_one_contract": discrete_one_contract}


def call_ratchet_policy() -> ratchet.Policy:
    """Long-call thresholds, measured against the premium paid.

    A long call's P&L swings with delta rather than grinding, so the dispersion
    of its levels is what identifies a fast tape. Its giveback is looser than a
    spread's for the same reason: the same underlying move moves it much further.
    """
    return ratchet.Policy(
        arm_pct=config.OPTION_MR_RATCHET_ARM_PCT,
        giveback_pct=config.OPTION_MR_RATCHET_GIVEBACK_PCT,
        high_vol_giveback_pct=config.OPTION_MR_RATCHET_HIGH_VOL_GIVEBACK_PCT,
        high_vol_pct=config.OPTION_MR_RATCHET_HIGH_VOL_PCT,
        confirmations=config.OPTION_MR_RATCHET_CONFIRMATIONS,
        history_limit=max(config.OPTION_MR_RATCHET_VOL_SAMPLES, 3),
        volatility_mode="levels",
    )


def ratchet_evidence(state: dict | None) -> dict:
    """The numbers that decided an exit, flattened for the audit log.

    A ``profit_ratchet`` reason on its own is unreviewable: it says a trail was
    breached without saying where the trail was, what it was trailing, or why it
    was that tight. Those are the only things that make the decision checkable
    after the fact, and the judge dashboard reads this log.
    """
    ratchet = state or {}
    return {
        "ratchet_armed": ratchet.get("armed"),
        "ratchet_high_water_pnl": ratchet.get("high_water_pnl"),
        "ratchet_arm_threshold_pnl": ratchet.get("arm_threshold_pnl"),
        "ratchet_floor_pnl": ratchet.get("floor_pnl"),
        "ratchet_giveback_pct": ratchet.get("giveback_pct"),
        "ratchet_breaches": ratchet.get("breaches"),
        "ratchet_capture_pct": ratchet.get("capture_pct"),
        "pnl_volatility_ratio": ratchet.get("pnl_volatility"),
        "pnl_volatility_high": ratchet.get("high_volatility"),
        "pnl_slope_nonpositive": ratchet.get("slope_nonpositive"),
    }


def ratchet_update(state: dict | None, executable_pnl: float | None,
                   premium_paid: float, quote_ready: bool = True) -> dict:
    """Advance the long call's profit ratchet by one executable mark."""
    previous = dict(state or {})
    if not config.OPTION_MR_RATCHET_ENABLED:
        return {**previous, "close": False, "reason": None,
                "armed": bool(previous.get("armed")),
                "samples": list(previous.get("samples") or [])}
    result = ratchet.update(
        pnl=executable_pnl,
        denominator=premium_paid,
        history=previous.get("samples") or [],
        breach_count=int(previous.get("breaches") or 0),
        high_water=_f(previous.get("high_water_pnl")),
        policy=call_ratchet_policy(),
        quote_ready=quote_ready,
    )
    return {
        "armed": result["armed"],
        "samples": result["history"],
        "high_water_pnl": result["high_water_pnl"],
        "arm_threshold_pnl": result["arm_threshold_pnl"],
        "floor_pnl": result["trailing_floor_pnl"] if result["armed"] else None,
        "breaches": result["breach_count"],
        "capture_pct": result.get("capture_pct"),
        "giveback_pct": result["giveback_pct"],
        "pnl_volatility": result["volatility_ratio"],
        "high_volatility": result["high_volatility"],
        "slope_nonpositive": result["slope_nonpositive"],
        "close": result["close"],
        "reason": result["reason"],
    }


def carry_to_break_even(ask: float, bid: float, spot: float, strike: float,
                        delta: float, dte: int,
                        hold_sessions: int | None = None) -> dict:
    """What the underlying must do just to pay for holding this call.

    Buying a call pays the variance risk premium that the credit-spread lane
    collects. Two costs are certain before the trade has an opinion: crossing
    the bid/ask once, and the extrinsic value that decays over the holding
    period. Expressed as the underlying move needed to offset them, they are
    directly comparable to the signal's measured edge.
    """
    hold = hold_sessions if hold_sessions is not None else config.OPTION_MR_MAX_HOLD_SESSIONS
    mid = (ask + bid) / 2
    if mid <= 0 or delta <= 0 or spot <= 0 or dte <= 0:
        return {"required_move_pct": float("inf")}
    crossing = max(ask - bid, 0.0)
    time_value = max(ask - max(spot - strike, 0.0), 0.0)
    # Extrinsic value decays with the square root of remaining time.
    remaining = max(dte - hold, 0)
    theta = time_value * (1.0 - math.sqrt(remaining / dte))
    carry = crossing + theta
    return {
        "crossing_usd": round(crossing * 100, 2),
        "theta_usd": round(theta * 100, 2),
        "carry_usd": round(carry * 100, 2),
        "carry_pct_of_premium": round(carry / ask, 4) if ask > 0 else None,
        "required_move_pct": carry / (delta * spot),
        "hold_sessions": hold,
    }


def parse_occ(symbol: str) -> dict | None:
    match = OCC_RE.fullmatch(str(symbol).upper())
    if not match:
        return None
    root, yymmdd, right, strike_raw = match.groups()
    try:
        expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None
    return {
        "symbol": str(symbol).upper(),
        "underlying": root,
        "expiry": expiry,
        "right": right,
        "strike": int(strike_raw) / 1000,
    }


def _quote_age(value, now_et: datetime) -> float:
    observed = _timestamp(value)
    if observed is None:
        return float("inf")
    return max(0.0, (now_et - observed).total_seconds())


def select_long_call_contract(candidate: dict, now_et: datetime) -> tuple[dict | None, str]:
    """Resolve one liquid long call from the live Alpaca option chain."""
    symbol = candidate["symbol"]
    spot = float(candidate["price"])
    start = (now_et.date() + timedelta(days=config.OPTION_MR_DTE_MIN)).isoformat()
    end = (now_et.date() + timedelta(days=config.OPTION_MR_DTE_MAX)).isoformat()
    payload = mcp_client.run(mcp_client.call_all_pages(
        "get_option_chain",
        underlying_symbol=symbol,
        feed=config.OPTIONS_FEED,
        type="call",
        expiration_date_gte=start,
        expiration_date_lte=end,
        strike_price_gte=round(spot * 0.70, 2),
        strike_price_lte=round(spot * 1.03, 2),
        limit=1000,
    ))
    if isinstance(payload, dict) and payload.get("error"):
        return None, str(payload["error"])
    snapshots = (payload or {}).get("snapshots") if isinstance(payload, dict) else None
    if not isinstance(snapshots, dict):
        return None, "Alpaca returned no option snapshots"

    eligible = []
    for contract_symbol, snapshot in snapshots.items():
        occ = parse_occ(contract_symbol)
        if occ is None or occ["right"] != "C" or occ["underlying"] != symbol:
            continue
        dte = (occ["expiry"] - now_et.date()).days
        quote = (snapshot or {}).get("latestQuote") or (snapshot or {}).get("latest_quote") or {}
        greeks = (snapshot or {}).get("greeks") or {}
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        delta = abs(_f(greeks.get("delta")))
        if bid <= 0 or ask <= 0 or ask < bid or delta <= 0:
            continue
        mid = (bid + ask) / 2
        rel_spread = (ask - bid) / mid if mid > 0 else 1.0
        quote_age = _quote_age(quote.get("t"), now_et)
        if not (
            config.OPTION_MR_DTE_MIN <= dte <= config.OPTION_MR_DTE_MAX
            and config.OPTION_MR_DELTA_MIN <= delta <= config.OPTION_MR_DELTA_MAX
            and rel_spread <= config.OPTION_MR_MAX_SPREAD_PCT
            and quote_age <= config.QUOTE_MAX_AGE_SEC
        ):
            continue
        # The economic filter the operational ones omit: a contract whose
        # crossing cost plus holding-period theta needs a bigger move than the
        # signal has ever averaged cannot be paid for by this signal. Measured
        # on the live chain, 48% of contracts inside the old 15% spread gate
        # failed this test, so they were rejected here rather than in the P&L.
        carry = carry_to_break_even(ask, bid, spot, occ["strike"], delta, dte)
        if carry["required_move_pct"] > config.OPTION_MR_CARRY_CEILING_PCT:
            continue
        eligible.append({
            "carry": carry,
            "required_move_pct": round(carry["required_move_pct"], 6),
            "edge_margin_pct": round(
                config.OPTION_MR_CARRY_CEILING_PCT - carry["required_move_pct"], 6
            ),
            **occ,
            "expiry": occ["expiry"].isoformat(),
            "dte": dte,
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "mid": round(mid, 4),
            "entry_limit": round(ask, 2),
            "delta": round(delta, 4),
            "iv": round(_f((snapshot or {}).get("impliedVolatility") or
                           (snapshot or {}).get("implied_volatility")), 4),
            "rel_spread": round(rel_spread, 6),
            "quote_age": round(quote_age, 3),
        })
    if not eligible:
        return None, (
            f"no {config.OPTION_MR_DTE_MIN}-{config.OPTION_MR_DTE_MAX} DTE call with "
            f"delta {config.OPTION_MR_DELTA_MIN:.2f}-{config.OPTION_MR_DELTA_MAX:.2f}, "
            f"spread <= {config.OPTION_MR_MAX_SPREAD_PCT:.0%}, and carry inside the "
            f"{config.OPTION_MR_CARRY_CEILING_PCT:.3%} ceiling "
            f"({config.OPTION_MR_CARRY_EDGE_MULTIPLE:g}x the measured signal edge)"
        )
    target_dte = (config.OPTION_MR_DTE_MIN + config.OPTION_MR_DTE_MAX) / 2
    chosen = min(eligible, key=lambda row: (
        # Cheapest carry relative to the measured edge comes first: that is the
        # margin the trade actually has. Delta proximity breaks ties, so
        # directional exposure is still resolved near the 0.70 target.
        -row["edge_margin_pct"],
        abs(row["delta"] - config.OPTION_MR_DELTA_TARGET) + row["rel_spread"],
        abs(row["dte"] - target_dte),
        row["symbol"],
    ))
    return chosen, ""


def _completed_daily(rows: list[dict], today: date) -> list[dict]:
    by_day: dict[str, dict] = {}
    for row in rows:
        day = str(row.get("t") or "")[:10]
        if day and day < today.isoformat() and _f(row.get("c")) > 0:
            by_day[day] = row
    return [by_day[day] for day in sorted(by_day)]


def _current_intraday(rows: list[dict], now_et: datetime) -> dict | None:
    completed = []
    for row in rows:
        started = _timestamp(row.get("t"))
        if (
            started is not None
            and started.date() == now_et.date()
            and wall_time(9, 30) <= started.time().replace(tzinfo=None) < wall_time(16)
            and started + timedelta(minutes=15) <= now_et
            and _f(row.get("c")) > 0
        ):
            completed.append((started, row))
    if not completed:
        return None
    completed.sort(key=lambda item: item[0])
    latest = completed[-1][1]
    return {
        "t": completed[-1][0].isoformat(),
        "o": _f(completed[0][1].get("o")),
        "h": max(_f(row.get("h")) for _, row in completed),
        "l": min(_f(row.get("l")) for _, row in completed),
        "c": _f(latest.get("c")),
        "v": sum(_f(row.get("v")) for _, row in completed),
    }


def signal_from_bars(symbol: str, daily_rows: list[dict], intraday_rows: list[dict],
                     now_et: datetime) -> dict | None:
    daily = _completed_daily(daily_rows, now_et.date())
    current = _current_intraday(intraday_rows, now_et)
    if len(daily) < 200 or current is None:
        return None
    historical_closes = [_f(row.get("c")) for row in daily]
    price = _f(current.get("c"))
    series = [*historical_closes, price]
    current_sma = fmean(series[-200:])
    previous_sma = fmean(historical_closes[-200:])
    rsi = wilder_rsi(series, 2)
    # The frozen validation sizes the position from completed prior sessions.
    # Do not let the still-forming entry session change ATR or the stop distance.
    atr = atr14(daily)
    mean = ema(series, 5)
    if rsi is None or atr is None or mean is None:
        return None
    return {
        "symbol": symbol,
        "signal_time": current["t"],
        "price": round(price, 4),
        "sma200": round(current_sma, 4),
        "previous_sma200": round(previous_sma, 4),
        "rsi2": round(rsi, 4),
        "atr14": round(atr, 4),
        "ema5": round(mean, 4),
        "passes": (
            price > current_sma
            and current_sma > previous_sma
            and rsi < config.OPTION_MR_RSI_MAX
        ),
    }


def fetch_signals(symbols: list[str], now_et: datetime) -> tuple[list[dict], dict]:
    start = (now_et.date() - timedelta(days=420)).isoformat()
    end = now_et.isoformat()
    symbol_csv = ",".join(symbols)
    session_open = datetime.combine(now_et.date(), wall_time(9, 30), tzinfo=ET)
    # get_stock_bars returns next_page_token but MCP 2.3.0 will not accept one
    # back, so completeness comes from non-overlapping time windows: every
    # window must reach EOF on its own, and a window that does not is bisected.
    # Daily and intraday need different widths to stay inside one Alpaca page.
    bar_calls = [mcp_client.bars_call("get_stock_bars", {
        "symbols": symbol_csv, "timeframe": "1Day", "start": start,
        "end": end, "limit": 10_000, "adjustment": "all", "feed": "sip",
        "sort": "asc",
    }, symbols=symbol_csv)]
    # Before the opening bell there is no interval to slice. Ask for nothing
    # rather than inverting one: every symbol then lacks a current bar, and the
    # complete-data gate refuses the scan instead of trading on a stale close.
    if now_et > session_open:
        bar_calls.append(mcp_client.bars_call("get_stock_bars", {
            "symbols": symbol_csv, "timeframe": "15Min",
            "start": session_open.isoformat(),
            "end": end, "limit": 2_000, "adjustment": "all", "feed": "sip",
            "sort": "asc",
        }, symbols=symbol_csv, bars_per_session=INTRADAY_BARS_PER_SESSION))
    payloads = mcp_client.run(mcp_client.call_many_time_windows(bar_calls))
    daily_payload = payloads[0]
    intraday_payload = payloads[1] if len(payloads) > 1 else {}
    errors = {}
    for name, payload in (("daily", daily_payload), ("intraday", intraday_payload)):
        if isinstance(payload, dict) and payload.get("error"):
            errors[name] = str(payload["error"])
    if errors:
        return [], errors

    daily_by_symbol = {symbol: _bar_map(daily_payload, symbol) for symbol in symbols}
    intraday_by_symbol = {symbol: _bar_map(intraday_payload, symbol) for symbol in symbols}
    # Alpaca's multi-symbol bars response can omit symbols even when the total
    # requested rows are below ``limit``.  Keep the fast bulk path, then repair
    # only missing series through the same MCP tool.  The complete-data gate is
    # unchanged: a symbol is accepted only after a full signal can be computed.
    missing = [
        symbol for symbol in symbols
        if len(daily_by_symbol[symbol]) < 200 or not intraday_by_symbol[symbol]
    ]
    if missing:
        repair_calls: list[tuple[str, dict]] = []
        repair_index: list[tuple[str, str]] = []
        for symbol in missing:
            if len(daily_by_symbol[symbol]) < 200:
                repair_calls.append(mcp_client.bars_call("get_stock_bars", {
                    "symbols": symbol, "timeframe": "1Day", "start": start,
                    "end": end, "limit": 10_000, "adjustment": "all", "feed": "sip",
                    "sort": "asc",
                }, symbols=symbol))
                repair_index.append((symbol, "daily"))
            if not intraday_by_symbol[symbol] and now_et > session_open:
                repair_calls.append(mcp_client.bars_call("get_stock_bars", {
                    "symbols": symbol, "timeframe": "15Min",
                    "start": session_open.isoformat(),
                    "end": end, "limit": 2_000, "adjustment": "all", "feed": "sip",
                    "sort": "asc",
                }, symbols=symbol, bars_per_session=INTRADAY_BARS_PER_SESSION))
                repair_index.append((symbol, "intraday"))
        repaired = mcp_client.run(
            mcp_client.call_many_time_windows(repair_calls)
        ) if repair_calls else []
        for (symbol, series), payload in zip(repair_index, repaired):
            if isinstance(payload, dict) and payload.get("error"):
                errors[f"{symbol}_{series}"] = str(payload["error"])
                continue
            if series == "daily":
                daily_by_symbol[symbol] = _bar_map(payload, symbol)
            else:
                intraday_by_symbol[symbol] = _bar_map(payload, symbol)

    signals = []
    for symbol in symbols:
        signal = signal_from_bars(
            symbol, daily_by_symbol[symbol], intraday_by_symbol[symbol], now_et
        )
        if signal:
            signals.append(signal)
    return signals, errors


def _issuer(symbol: str) -> str:
    return "ALPHABET" if symbol in {"GOOG", "GOOGL"} else symbol


def rank_candidates(signals: list[dict], held_symbols: set[str]) -> list[dict]:
    held_issuers = {_issuer(symbol) for symbol in held_symbols}
    eligible = [
        signal for signal in signals
        if signal.get("passes")
        and signal["symbol"] not in held_symbols
        and _issuer(signal["symbol"]) not in held_issuers
    ]
    best_by_issuer: dict[str, dict] = {}
    for signal in eligible:
        key = _issuer(signal["symbol"])
        incumbent = best_by_issuer.get(key)
        if incumbent is None or (signal["rsi2"], signal["symbol"]) < (
            incumbent["rsi2"], incumbent["symbol"]
        ):
            best_by_issuer[key] = signal
    return sorted(
        best_by_issuer.values(), key=lambda row: (row["rsi2"], row["symbol"])
    )


def select_candidate(signals: list[dict], held_symbols: set[str]) -> dict | None:
    ranked = rank_candidates(signals, held_symbols)
    return ranked[0] if ranked else None


def _news_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    for key in ("news", "articles"):
        rows = _rows(payload, key)
        if rows:
            return rows
    return []


def news_brief(symbol: str, now_et: datetime) -> tuple[str, int]:
    payload = mcp_client.run(mcp_client.call(
        "get_news",
        symbols=symbol,
        start=(now_et - timedelta(days=4)).isoformat(),
        end=now_et.isoformat(),
        sort="desc",
        limit=8,
        include_content=False,
    ))
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    articles = _news_rows(payload)[:8]
    if not articles:
        return "No articles returned by Alpaca MCP get_news for this interval.", 0
    lines = []
    for article in articles:
        headline = str(article.get("headline") or article.get("title") or "untitled")[:240]
        summary = str(article.get("summary") or "")[:360]
        timestamp = article.get("created_at") or article.get("updated_at") or "time unavailable"
        lines.append(f"- {timestamp} | {headline}" + (f" | {summary}" if summary else ""))
    return "\n".join(lines), len(articles)


def candidate_brief(candidate: dict, contract: dict, sizing: dict,
                    stop_price: float, news_text: str, now_et: datetime) -> str:
    qty = sizing["contracts"]
    return f"""Paper options candidate selected by NDX30_CALL_MR_01 policy.
Observed at: {now_et.isoformat()}
Underlying: {candidate['symbol']}
15:30 completed-bar price: {candidate['price']:.2f}
Wilder RSI(2): {candidate['rsi2']:.2f} (hard rule < {config.OPTION_MR_RSI_MAX:.2f})
SMA200: {candidate['sma200']:.2f}; previous SMA200: {candidate['previous_sma200']:.2f}
EMA5: {candidate['ema5']:.2f}; ATR14: {candidate['atr14']:.2f}
Deterministic OCC contract: {contract['symbol']}; long call; expiry {contract['expiry']};
delta {contract['delta']:.3f}; bid/ask {contract['bid']:.2f}/{contract['ask']:.2f}.
Deterministic order: buy to open {qty} contract(s) at limit {contract['entry_limit']:.2f};
maximum premium at risk ${qty * contract['entry_limit'] * 100:,.2f}.
Deterministic exit: underlying stop {stop_price:.2f}, 15:45 ET recovery above EMA5,
or {config.OPTION_MR_MAX_HOLD_SESSIONS} normal sessions.
Invalidation: underlying reaches {stop_price:.2f}. The full paid premium is the
contract's legal maximum loss; no stock order is permitted.

Alpaca MCP get_news observations (not an earnings calendar):
{news_text}
"""


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _flatten_orders(orders: list[dict]) -> list[dict]:
    flattened = []
    for order in orders:
        flattened.append(order)
        flattened.extend(_flatten_orders([
            leg for leg in (order.get("legs") or []) if isinstance(leg, dict)
        ]))
    return flattened


def verify_order(
    client_order_id: str, attempts: int = 10, after: str | None = None
) -> dict | None:
    after = after or datetime.now(ET).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    for attempt in range(attempts):
        if attempt:
            time.sleep(0.35)
        payload = mcp_client.run(mcp_client.call(
            "get_orders", status="all", limit=500, after=after, nested=True
        ))
        if isinstance(payload, dict) and payload.get("error"):
            continue
        for order in _flatten_orders(_rows(payload, "orders")):
            if (
                str(order.get("client_order_id") or "").lower() == client_order_id.lower()
                and order.get("id")
            ):
                return order
    return None


def broker_submission_confirmed(order: dict | None) -> bool:
    """An MCP accepted envelope is not success; Alpaca order state is."""
    status = str((order or {}).get("status") or "").lower().split(".")[-1]
    return bool(order and order.get("id") and status not in FAILED_ORDER_STATES)


def _normal_entry_window(snapshot) -> tuple[str | None, str]:
    """Return the key of the decision window now open, if any."""
    if not snapshot.market_open or snapshot.regular_close is None:
        return None, "regular market is not open"
    if snapshot.regular_close.time().replace(tzinfo=None) != wall_time(16):
        return None, f"early-close session ends {snapshot.regular_close.time()}"
    current = snapshot.now_et.time().replace(tzinfo=None)
    for hour, minute in config.OPTION_MR_DECISION_WINDOWS:
        start = wall_time(hour, minute)
        end_minute = minute + 10
        end = wall_time(hour + end_minute // 60, end_minute % 60)
        if start <= current <= end:
            return (
                f"{hour:02d}:{minute:02d}",
                f"decision window {start.strftime('%H:%M')}-{end.strftime('%H:%M')} ET",
            )
    listed = ", ".join(f"{h:02d}:{m:02d}" for h, m in config.OPTION_MR_DECISION_WINDOWS)
    return None, f"outside the {listed} decision window(s)"


def _window_start(snapshot, window_key: str) -> datetime:
    hour, minute = (int(part) for part in window_key.split(":", 1))
    return snapshot.now_et.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _transient_retry_allowed(snapshot, window_key: str) -> bool:
    """Allow a few supervisor cycles to reconcile inside the current window."""
    return snapshot.now_et < _window_start(snapshot, window_key) + timedelta(minutes=8)


def _window_done(state: dict, session_date: str, window_key: str) -> bool:
    scanned = state.get("scanned_windows") or {}
    if window_key in (scanned.get(session_date) or []):
        return True
    # State written before multiple windows existed recorded only the date.
    # Treat that as "this session is finished" so an upgrade mid-session cannot
    # hand the lane a second set of entries it has already used.
    return (
        state.get("last_scan_date") == session_date
        and session_date not in scanned
    )


def _mark_window_done(state: dict, session_date: str, window_key: str) -> None:
    scanned = state.setdefault("scanned_windows", {})
    done = scanned.setdefault(session_date, [])
    if window_key not in done:
        done.append(window_key)
    # Keep only recent sessions so the state file cannot grow without bound.
    for stale in sorted(scanned)[:-10]:
        scanned.pop(stale, None)
    state["last_scan_date"] = session_date


def _write_decision(state: dict, snapshot, status: str, checks: list[dict],
                    window: str | None = None, **payload) -> dict:
    if window:
        _mark_window_done(state, snapshot.session_date, window)
    state["last_decision"] = {
        "status": status, "at": snapshot.now_et.isoformat(),
        "window": window, **payload,
    }
    save_state(state)
    return log("decision", status=status, checks=checks, window=window,
               session_date=snapshot.session_date, **payload)


def maybe_enter(snapshot) -> dict | None:
    """Run at most one broker-reconciled long-call entry decision per day."""
    if not config.OPTION_MR_ENABLED:
        return None
    window_key, window_detail = _normal_entry_window(snapshot)
    if window_key is None:
        return None
    state = load_state()
    if _window_done(state, snapshot.session_date, window_key):
        return None

    active_records = {
        contract_symbol: record
        for contract_symbol, record in (state.get("positions") or {}).items()
        if isinstance(record, dict)
        and record.get("status") in {"entry_pending", "open", "exit_pending"}
    }
    broker_option_symbols = {
        str(position.get("symbol") or "").upper()
        for position in snapshot.option_positions
    }
    managed_symbols = {symbol.upper() for symbol in active_records}
    managed_open = broker_option_symbols & managed_symbols
    foreign_options = broker_option_symbols - managed_symbols

    option_eligible = (
        snapshot.options_approved_level >= config.MIN_OPTIONS_TRADING_LEVEL
        and snapshot.options_trading_level >= config.MIN_OPTIONS_TRADING_LEVEL
        and snapshot.options_buying_power > 0
    )
    clock_ok, clock_detail = session.verify_broker_clock(snapshot)
    checks = [
        # Everything below trusts snapshot.now_et - which window is open, the
        # session date, the client order id. Confirm it against Alpaca before
        # any of that matters, so a snapshot the broker does not recognise can
        # never reach an order.
        _check("broker_clock_agrees", clock_ok, clock_detail),
        _check("paper_account_identity", bool(config.ALPACA_ACCOUNT_ID) and
               snapshot.account_number == config.ALPACA_ACCOUNT_ID,
               f"MCP={snapshot.account_number or 'missing'} configured={config.ALPACA_ACCOUNT_ID or 'missing'}"),
        _check("regular_decision_window", True, window_detail),
        _check("account_active", snapshot.account_status == "ACTIVE" and not (
            snapshot.trading_blocked or snapshot.account_blocked or snapshot.trade_suspended_by_user
        ), f"status={snapshot.account_status}; blocked={snapshot.trading_blocked or snapshot.account_blocked or snapshot.trade_suspended_by_user}"),
        _check("alpaca_options_eligible", option_eligible,
               f"approved=L{snapshot.options_approved_level}; enabled=L{snapshot.options_trading_level}; options BP=${snapshot.options_buying_power:,.2f}"),
        _check("options_only_account", not snapshot.non_option_positions,
               f"{len(snapshot.non_option_positions)} non-option position(s)"),
        # Each lane owns a share of equity, so this one can still trade while a
        # spread is open. Requiring a flat account instead made the two
        # strategies mutually exclusive, and this one never traded.
        _check("option_mr_capital_budget", snapshot.options_buying_power > 0,
               f"${snapshot.options_buying_power:,.2f} options BP available; this "
               f"lane may hold {config.OPTION_MR_TOTAL_PREMIUM_PCT:.0%} of equity in "
               f"premium while the spread lane holds {config.SPREAD_EQUITY_PCT:.0%}; "
               f"{len(foreign_options)} contract(s) held by the primary lane"),
        _check("option_position_limit", len(managed_open) < config.OPTION_MR_MAX_POSITIONS,
               f"{len(managed_open)} managed calls < {config.OPTION_MR_MAX_POSITIONS}"),
        _check("daily_entry_limit", snapshot.option_mr_entries_today < config.OPTION_MR_MAX_ENTRIES_PER_DAY,
               f"broker reports {snapshot.option_mr_entries_today} option-MR entries today"),
        _check("pending_option_order", not snapshot.pending_opening_orders and not snapshot.pending_closing_orders,
               f"opening={len(snapshot.pending_opening_orders)}; closing={len(snapshot.pending_closing_orders)}"),
    ]
    if not clock_ok:
        return _write_decision(state, snapshot, "VETOED", checks, window=window_key,
                               reason="broker_clock_disagrees")
    if not all(check["passed"] for check in checks):
        # Give an option fill/close a few supervisor cycles to reconcile, but
        # make a final auditable decision near the end of the ten-minute window.
        if _transient_retry_allowed(snapshot, window_key):
            return {"strategy": "NDX30_CALL_MR_01", "status": "waiting",
                    "window": window_key, "checks": checks}
        return _write_decision(state, snapshot, "VETOED", checks, window=window_key,
                               reason="portfolio_or_broker_preflight")

    signals, errors = fetch_signals(config.OPTION_MR_UNIVERSE, snapshot.now_et)
    data_ok = not errors and len(signals) == len(config.OPTION_MR_UNIVERSE)
    checks.append(_check(
        "complete_sip_data", data_ok,
        f"{len(signals)}/{len(config.OPTION_MR_UNIVERSE)} symbols complete; errors={errors or 'none'}",
    ))
    if not data_ok:
        if _transient_retry_allowed(snapshot, window_key):
            return {
                "strategy": "NDX30_CALL_MR_01", "status": "waiting",
                "reason": "sip_data_pending", "window": window_key, "checks": checks,
            }
        return _write_decision(state, snapshot, "VETOED", checks, window=window_key,
                               reason="incomplete_sip_data")

    held = {
        str(record.get("underlying") or "").upper()
        for record in active_records.values()
    }
    ranked = rank_candidates(signals, held)
    signal_count = sum(bool(signal.get("passes")) for signal in signals)
    checks.append(_check(
        "mean_reversion_signal", bool(ranked),
        f"{signal_count} symbols pass price>SMA200, rising SMA200, RSI(2)<{config.OPTION_MR_RSI_MAX:g}",
    ))
    if not ranked:
        return _write_decision(
            state, snapshot, "NO_SIGNAL", checks, window=window_key,
            reason="no_qualified_mean_reversion",
            scan={"symbols": len(signals), "raw_signals": signal_count},
        )

    entries_allowed = min(
        config.OPTION_MR_MAX_ENTRIES_PER_DAY - snapshot.option_mr_entries_today,
        config.OPTION_MR_MAX_POSITIONS - len(managed_open),
    )
    budget = sizing_mod.option_mr_budget(
        snapshot.options_buying_power, snapshot.equity,
        config.OPTION_MR_TOTAL_PREMIUM_PCT, deployed_premium(state),
    )
    scan = {"symbols": len(signals), "raw_signals": signal_count}
    option_errors: dict[str, str] = {}
    entries: list[dict] = []
    submitted_any = False
    taken_issuers = {_issuer(symbol) for symbol in held}
    last_result = None

    # One decision window may open several positions. Each is resolved, gated,
    # and reviewed by the LLM independently, so a portfolio of entries is a
    # portfolio of separate AI decisions rather than one bulk allocation.
    options_bp = snapshot.options_buying_power
    for proposal in ranked:
        if len(entries) >= entries_allowed or budget <= 0:
            break
        if _issuer(proposal["symbol"]) in taken_issuers:
            continue
        if entries:
            # A previous entry in this window is already working at the broker.
            # Its collateral is reserved the moment Alpaca accepts it, well
            # before it fills, so sizing the next one against the opening
            # snapshot would double-spend the same buying power.
            options_bp = _refresh_options_buying_power(options_bp)
            if options_bp <= 0:
                option_errors[proposal["symbol"]] = "no options buying power left"
                break
        built, error = select_long_call_contract(proposal, snapshot.now_et)
        if built is None:
            option_errors[proposal["symbol"]] = error
            continue
        sized = option_position_size(
            snapshot.equity, options_bp,
            built["entry_limit"], built["delta"], proposal["atr14"],
            premium_budget=budget,
        )
        if sized.get("contracts", 0) < 1:
            option_errors[proposal["symbol"]] = "premium budget cannot buy one contract"
            continue

        result = _open_one_call(
            state, snapshot, proposal, built, sized, list(checks), scan,
            options_bp=options_bp, window=window_key,
        )
        last_result = result
        entries.append({
            "underlying": proposal["symbol"],
            "contract": built["symbol"],
            "status": result.get("status"),
            "reason": result.get("reason"),
            "qty": result.get("qty"),
            "premium": result.get("premium_total"),
        })
        taken_issuers.add(_issuer(proposal["symbol"]))
        if result.get("status") == "SUBMITTED":
            submitted_any = True
            budget -= _f(result.get("premium_total"))

    if not entries:
        checks.append(_check("executable_long_call", False, f"none; {option_errors}"))
        if _transient_retry_allowed(snapshot, window_key):
            return {
                "strategy": "NDX30_CALL_MR_01", "status": "waiting",
                "reason": "option_chain_pending", "window": window_key,
                "checks": checks, "option_errors": option_errors,
            }
        return _write_decision(
            state, snapshot, "VETOED", checks, window=window_key,
            reason="no_executable_long_call", scan=scan, option_errors=option_errors,
        )

    _mark_window_done(state, snapshot.session_date, window_key)
    save_state(state)
    log("window_summary", session_date=snapshot.session_date, window=window_key,
        entries=entries, submitted=submitted_any,
        premium_budget_remaining=round(max(budget, 0.0), 2))
    return {
        **(last_result or {}),
        "window": window_key,
        "status": "SUBMITTED" if submitted_any else (last_result or {}).get("status"),
        "entries": entries,
        "entries_allowed": entries_allowed,
        "premium_budget_remaining": round(max(budget, 0.0), 2),
        "option_errors": option_errors,
    }


def _refresh_options_buying_power(fallback: float) -> float:
    """Re-read live options buying power between entries in one window."""
    account = mcp_client.run(mcp_client.call("get_account_info"))
    if not isinstance(account, dict) or account.get("error"):
        return 0.0  # fail closed rather than reuse a stale number
    return _f(account.get("options_buying_power"), fallback)


def _open_one_call(state: dict, snapshot, candidate: dict, contract: dict,
                   sizing: dict, checks: list[dict], scan: dict,
                   options_bp: float | None = None,
                   window: str | None = None) -> dict:
    """Gate, AI-review, and submit exactly one long call."""
    if options_bp is None:
        options_bp = snapshot.options_buying_power
    qty = int(sizing["contracts"])
    stop_price = round(
        candidate["price"] - config.OPTION_MR_STOP_ATR_MULTIPLE * candidate["atr14"], 2
    )
    premium_total = qty * contract["entry_limit"] * 100
    checks.extend([
        _check("executable_long_call", True, f"resolved {contract['symbol']}"),
        _check("options_only_instrument", parse_occ(contract["symbol"]) is not None,
               f"single long OCC call {contract['symbol']}; no stock order"),
        _check("contract_dte", config.OPTION_MR_DTE_MIN <= contract["dte"] <= config.OPTION_MR_DTE_MAX,
               f"{contract['dte']} DTE in {config.OPTION_MR_DTE_MIN}-{config.OPTION_MR_DTE_MAX}"),
        _check("contract_delta", config.OPTION_MR_DELTA_MIN <= contract["delta"] <= config.OPTION_MR_DELTA_MAX,
               f"delta {contract['delta']:.3f}; target {config.OPTION_MR_DELTA_TARGET:.2f}"),
        _check("quote_freshness", contract["quote_age"] <= config.QUOTE_MAX_AGE_SEC,
               f"quote age {contract['quote_age']:.1f}s <= {config.QUOTE_MAX_AGE_SEC}s"),
        _check("option_liquidity", contract["rel_spread"] <= config.OPTION_MR_MAX_SPREAD_PCT,
               f"bid/ask {contract['rel_spread']:.1%} <= {config.OPTION_MR_MAX_SPREAD_PCT:.1%}"),
        _check("carry_within_signal_edge",
               contract["required_move_pct"] <= config.OPTION_MR_CARRY_CEILING_PCT,
               f"needs {contract['required_move_pct']:.3%} underlying move over "
               f"{contract['carry']['hold_sessions']} sessions vs a "
               f"{config.OPTION_MR_CARRY_CEILING_PCT:.3%} ceiling "
               f"({config.OPTION_MR_CARRY_EDGE_MULTIPLE:g}x the "
               f"{config.OPTION_MR_SIGNAL_EDGE_PCT:.3%} measured signal edge); carry "
               f"${contract['carry']['carry_usd']:,.2f}/contract = crossing "
               f"${contract['carry']['crossing_usd']:,.2f} + theta "
               f"${contract['carry']['theta_usd']:,.2f}"),
        _check("deterministic_size", qty >= 1,
               f"mode={sizing['mode']}; qty={qty}; premium ${premium_total:,.2f} <= "
               f"budget ${sizing['premium_cap']:,.2f}; modeled 2xATR loss "
               f"${sizing['modeled_stop_loss_per_contract'] * qty:,.2f}"),
        _check("premium_cap", premium_total <= sizing["premium_cap"],
               f"premium ${premium_total:,.2f} <= budget ${sizing['premium_cap']:,.2f}"),
        _check("broker_buying_power", premium_total <= options_bp,
               f"premium ${premium_total:,.2f} <= live options BP ${options_bp:,.2f}"),
        _check("underlying_hard_stop", 0 < stop_price < candidate["price"],
               f"software-monitored ${stop_price:.2f} = signal - {config.OPTION_MR_STOP_ATR_MULTIPLE:g}x ATR14"),
        _check("limit_order_only", contract["entry_limit"] > 0,
               f"buy-to-open limit ${contract['entry_limit']:.2f}; never market"),
    ])
    if not all(check["passed"] for check in checks):
        return {**_write_decision(state, snapshot, "VETOED", checks,
                                  reason="deterministic_risk_gate", candidate=candidate,
                                  window=None, entry_window=window),
                "premium_total": 0.0, "qty": 0}

    try:
        news_text, news_count = news_brief(candidate["symbol"], snapshot.now_et)
        news_ok = True
    except Exception as exc:
        news_text, news_count, news_ok = f"{type(exc).__name__}: {exc}", 0, False
    checks.append(_check(
        "alpaca_news_available", news_ok,
        f"{news_count} timestamped Alpaca MCP articles; earnings calendar independently verified=false",
    ))
    if not news_ok:
        return {**_write_decision(state, snapshot, "VETOED", checks,
                                  reason="news_context_unavailable", candidate=candidate,
                                  window=None, entry_window=window),
                "premium_total": 0.0, "qty": 0}

    review, raw_reply = llm.review_option_mr_candidate(
        candidate_brief(candidate, contract, sizing, stop_price, news_text, snapshot.now_et)
    )
    llm_ok = review is not None
    approved = llm_ok and review.get("decision") == "approve"
    checks.extend([
        _check("ai_review_schema", llm_ok,
               "one structured approve/veto review" if llm_ok else "unparseable AI review"),
        _check("ai_event_risk", approved,
               str((review or {}).get("event_risk") or "review unavailable")[:400]),
    ])
    if not approved:
        return {**_write_decision(
            state, snapshot, "VETOED", checks, reason="ai_event_risk",
            candidate=candidate, contract=contract, qty=qty, stop_price=stop_price,
            sizing=sizing, ai_review=review, scan=scan, entry_window=window,
            raw_reply_chars=len(raw_reply), news_count=news_count,
            earnings_calendar_verified=False,
        ), "premium_total": 0.0, "qty": 0}

    client_order_id = (
        f"{ENTRY_PREFIX}{snapshot.now_et.strftime('%Y%m%d')}-"
        f"{candidate['symbol'].lower()}-{uuid.uuid4().hex[:6]}"
    )
    response = mcp_client.run(mcp_client.call(
        "place_option_order",
        symbol=contract["symbol"], side="buy", position_intent="buy_to_open",
        qty=str(qty), type="limit", time_in_force="day",
        limit_price=f"{contract['entry_limit']:.2f}", client_order_id=client_order_id,
    ))
    broker_order = verify_order(client_order_id)
    broker_status = str((broker_order or {}).get("status") or "").lower().split(".")[-1]
    submitted = broker_submission_confirmed(broker_order)
    checks.append(_check(
        "broker_reconciliation", submitted,
        f"get_orders confirmed broker id {(broker_order or {}).get('id')}"
        if submitted else "MCP response was not found by client ID in Alpaca get_orders",
    ))
    if submitted:
        positions = state.setdefault("positions", {})
        positions[contract["symbol"]] = {
            "status": "entry_pending",
            "contract_symbol": contract["symbol"],
            "underlying": candidate["symbol"],
            "expiry": contract["expiry"],
            "strike": contract["strike"],
            "delta": contract["delta"],
            "signal_date": snapshot.session_date,
            "signal_time": candidate["signal_time"],
            "signal_price": candidate["price"],
            "qty": qty,
            "atr14": candidate["atr14"],
            "underlying_stop": stop_price,
            "entry_limit": contract["entry_limit"],
            "max_premium": round(premium_total, 2),
            "sizing": sizing,
            "entry_client_order_id": client_order_id,
            "entry_broker_order_id": broker_order.get("id"),
            "thesis": review.get("thesis"),
            "event_risk": review.get("event_risk"),
            "earnings_calendar_verified": False,
        }
    return {**_write_decision(
        state, snapshot, "SUBMITTED" if submitted else "VETOED", checks,
        reason="approved" if submitted else "broker_reconciliation_failed",
        candidate=candidate, contract=contract, qty=qty, stop_price=stop_price,
        sizing=sizing, ai_review=review, scan=scan, entry_window=window,
        news_count=news_count, raw_reply_chars=len(raw_reply),
        earnings_calendar_verified=False,
        execution={
            "submitted": submitted, "client_order_id": client_order_id,
            "broker_order_id": (broker_order or {}).get("id"),
            "response_status": (response or {}).get("status") if isinstance(response, dict) else None,
            "broker_status": broker_status or None,
        },
    ), "premium_total": premium_total if submitted else 0.0, "qty": qty}


def _holding_sessions(entry_date: str, today: date) -> int:
    payload = mcp_client.run(mcp_client.call(
        "get_calendar", start=entry_date, end=today.isoformat()
    ))
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    sessions = 0
    for row in _rows(payload, "calendar"):
        day = str(row.get("date") or "")
        close = str(row.get("close") or "").replace(":", "")
        # The frozen validation counts the entry day as holding session one.
        # An entry on Monday therefore reaches a three-session timeout on
        # Wednesday, provided all three are normal 16:00 closes.
        if entry_date <= day <= today.isoformat() and close == "1600":
            sessions += 1
    return sessions


def _option_positions(payload) -> dict[str, dict]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in _rows(payload, "positions")
        if parse_occ(str(row.get("symbol") or "")) is not None
    }


def _option_quotes(payload) -> dict[str, dict]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("quotes") if isinstance(payload.get("quotes"), dict) else payload
    out = {}
    for symbol, quote in rows.items():
        if not isinstance(quote, dict):
            continue
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        if bid > 0 and ask > 0 and ask >= bid:
            out[str(symbol).upper()] = {
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            }
    return out


def _stock_spots(payload) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("quotes") if isinstance(payload.get("quotes"), dict) else payload
    out = {}
    for symbol, quote in rows.items():
        if not isinstance(quote, dict):
            continue
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        value = (bid + ask) / 2 if bid and ask else (bid or ask)
        if value > 0:
            out[str(symbol).upper()] = value
    return out


def _order_intent(order: dict) -> str:
    return str(order.get("position_intent") or "").lower()


def _pending_close(contract_symbol: str, open_orders: list[dict]) -> bool:
    return any(
        str(order.get("symbol") or "").upper() == contract_symbol
        and str(order.get("side") or "").lower() == "sell"
        and _order_intent(order) == "sell_to_close"
        for order in open_orders
    )


def monitor_cycle(clock: dict, open_orders_payload, all_positions_payload,
                  account: dict, execute: bool) -> dict:
    """Reconcile managed long calls and submit deterministic option exits."""
    if not config.OPTION_MR_ENABLED:
        return {"enabled": False}
    now_et = _timestamp(clock.get("timestamp")) or datetime.now(ET)
    positions = _option_positions(all_positions_payload)
    open_orders = _rows(open_orders_payload, "orders")
    state = load_state()
    managed = state.setdefault("positions", {})
    events = []

    for contract_symbol, record in list(managed.items()):
        status = str(record.get("status") or "")
        broker_position = positions.get(contract_symbol)
        if broker_position and status == "entry_pending":
            record.update({
                "status": "open",
                "opened_at": now_et.isoformat(),
                "filled_qty": _f(broker_position.get("qty")),
                "avg_entry_price": _f(broker_position.get("avg_entry_price")),
            })
            events.append(log(
                "entry_filled", contract_symbol=contract_symbol,
                underlying=record.get("underlying"), broker_position=broker_position,
            ))
        elif not broker_position and status == "entry_pending":
            signal_date = str(record.get("signal_date") or now_et.date().isoformat())
            try:
                signal_day = date.fromisoformat(signal_date)
            except ValueError:
                signal_day = now_et.date()
            broker_order = verify_order(
                str(record.get("entry_client_order_id") or ""), attempts=1,
                after=datetime.combine(signal_day, wall_time.min, tzinfo=ET).isoformat(),
            )
            broker_status = str((broker_order or {}).get("status") or "").lower().split(".")[-1]
            if broker_status in FAILED_ORDER_STATES:
                record.update({
                    "status": "closed", "closed_at": now_et.isoformat(),
                    "close_reason": f"entry_{broker_status}",
                })
                events.append(log(
                    "entry_not_filled", contract_symbol=contract_symbol,
                    underlying=record.get("underlying"), broker_status=broker_status,
                    client_order_id=record.get("entry_client_order_id"),
                ))
        elif not broker_position and status in {"open", "exit_pending"}:
            record.update({"status": "closed", "closed_at": now_et.isoformat()})
            # The terminal record for this position. It has to stand alone:
            # everything needed to review the exit, including the entry it is
            # being measured against and the trail that triggered it.
            entry_premium = _f(record.get("entry_limit")) * _f(record.get("qty")) * 100
            event = log(
                "position_closed", contract_symbol=contract_symbol,
                underlying=record.get("underlying"),
                reason=record.get("exit_reason") or "broker_or_manual_close",
                entry_client_order_id=record.get("entry_client_order_id"),
                exit_client_order_id=record.get("exit_client_order_id"),
                qty=record.get("qty"),
                entry_limit=record.get("entry_limit"),
                entry_premium=round(entry_premium, 2),
                exit_limit=record.get("exit_limit"),
                exit_trigger_pnl=record.get("exit_trigger_pnl"),
                underlying_stop=record.get("underlying_stop"),
                signal_date=record.get("signal_date"),
                adopted=record.get("adopted"),
                **ratchet_evidence(record.get("ratchet")),
            )
            state["last_exit"] = event
            events.append(event)
        elif broker_position and status == "exit_pending":
            if _pending_close(contract_symbol, open_orders):
                continue
            signal_date = str(record.get("signal_date") or now_et.date().isoformat())
            try:
                signal_day = date.fromisoformat(signal_date)
            except ValueError:
                signal_day = now_et.date()
            broker_order = verify_order(
                str(record.get("exit_client_order_id") or ""), attempts=1,
                after=datetime.combine(signal_day, wall_time.min, tzinfo=ET).isoformat(),
            )
            broker_status = str((broker_order or {}).get("status") or "").lower().split(".")[-1]
            if broker_status in FAILED_ORDER_STATES:
                record.update({"status": "open", "exit_client_order_id": None})
                events.append(log(
                    "exit_not_filled", contract_symbol=contract_symbol,
                    underlying=record.get("underlying"), broker_status=broker_status,
                ))

    exit_hour, exit_minute = config.OPTION_MR_EXIT_CHECK_WINDOW
    normal_window = (
        bool(clock.get("is_open"))
        and now_et.hour == exit_hour
        and exit_minute <= now_et.minute <= exit_minute + 10
    )

    open_records = {
        symbol: record for symbol, record in managed.items()
        if record.get("status") == "open" and symbol in positions
    }
    option_quotes = {}
    spots = {}
    if bool(clock.get("is_open")) and open_records:
        underlyings = sorted({str(record.get("underlying") or "") for record in open_records.values()})
        quote_payload, spot_payload = mcp_client.run(mcp_client.call_many([
            ("get_option_latest_quote", {
                "symbols": ",".join(sorted(open_records)), "feed": config.OPTIONS_FEED,
            }),
            ("get_stock_latest_quote", {
                "symbols": ",".join(underlyings), "feed": "sip",
            }),
        ]))
        if not (isinstance(quote_payload, dict) and quote_payload.get("error")):
            option_quotes = _option_quotes(quote_payload)
        if not (isinstance(spot_payload, dict) and spot_payload.get("error")):
            spots = _stock_spots(spot_payload)

    for contract_symbol, record in open_records.items():
        underlying = str(record.get("underlying") or "").upper()
        spot = spots.get(underlying, 0.0)
        option_quote = option_quotes.get(contract_symbol)
        reason = None
        signal = None
        held_sessions = None

        broker_position = positions[contract_symbol]
        avg_entry = _f(broker_position.get("avg_entry_price"))
        qty = abs(_f(broker_position.get("qty")))
        executable_pnl = None
        if option_quote and avg_entry > 0 and qty > 0:
            executable_pnl = round((option_quote["bid"] - avg_entry) * qty * 100, 2)
        ratchet_state = ratchet_update(
            record.get("ratchet"), executable_pnl, avg_entry * qty * 100,
            quote_ready=bool(option_quote and option_quote.get("bid", 0) > 0),
        )
        record["ratchet"] = ratchet_state

        if spot > 0 and spot <= _f(record.get("underlying_stop")):
            reason = "underlying_stop"
        elif ratchet_state.get("close"):
            reason = ratchet_state["reason"]
        elif normal_window and record.get("last_exit_check_date") != now_et.date().isoformat():
            signals, errors = fetch_signals([underlying], now_et)
            signal = signals[0] if signals else None
            if errors or signal is None:
                events.append(log(
                    "exit_check_failed", contract_symbol=contract_symbol,
                    underlying=underlying, errors=errors or "incomplete bars",
                ))
                continue
            held_sessions = _holding_sessions(str(record.get("signal_date")), now_et.date())
            if config.OPTION_MR_PROFIT_EXIT_ENABLED and signal["price"] > signal["ema5"]:
                reason = "ema5_recovery"
            elif held_sessions >= config.OPTION_MR_MAX_HOLD_SESSIONS:
                reason = "max_hold_sessions"
            record["last_exit_check"] = {
                "price": signal["price"], "ema5": signal["ema5"],
                "held_sessions": held_sessions, "reason": reason,
            }

        record["last_mark"] = {
            "at": now_et.isoformat(), "underlying_price": round(spot, 4),
            "option_bid": (round(option_quote["bid"], 4) if option_quote else None),
            "executable_pnl": executable_pnl,
            "capture_pct": ratchet_state.get("capture_pct"),
            "ratchet_armed": ratchet_state.get("armed"),
            "ratchet_high_water": ratchet_state.get("high_water_pnl"),
            "ratchet_floor": ratchet_state.get("floor_pnl"),
        }

        if not reason:
            if normal_window and signal is not None:
                record["last_exit_check_date"] = now_et.date().isoformat()
                events.append(log(
                    "exit_hold", contract_symbol=contract_symbol, underlying=underlying,
                    price=signal["price"], ema5=signal["ema5"],
                    held_sessions=held_sessions, executable_pnl=executable_pnl,
                    **ratchet_evidence(record.get("ratchet")),
                ))
            continue
        if _pending_close(contract_symbol, open_orders):
            events.append(log(
                "exit_wait", contract_symbol=contract_symbol, underlying=underlying,
                reason="close_order_pending",
            ))
            continue
        if not option_quote or option_quote["bid"] <= 0 or qty <= 0:
            events.append(log(
                "exit_blocked", contract_symbol=contract_symbol, underlying=underlying,
                reason="executable_option_bid_unavailable", trigger=reason,
            ))
            continue
        if not execute:
            events.append(log(
                "exit_would_submit", contract_symbol=contract_symbol,
                underlying=underlying, reason=reason, executable_pnl=executable_pnl,
                underlying_price=round(spot, 4),
                **ratchet_evidence(record.get("ratchet")),
            ))
            continue

        exit_limit = round(option_quote["bid"], 2)
        client_order_id = (
            f"{EXIT_PREFIX}{now_et.strftime('%Y%m%d')}-"
            f"{underlying.lower()}-{uuid.uuid4().hex[:6]}"
        )
        response = mcp_client.run(mcp_client.call(
            "place_option_order", symbol=contract_symbol, side="sell",
            position_intent="sell_to_close", qty=f"{qty:g}", type="limit",
            time_in_force="day", limit_price=f"{exit_limit:.2f}",
            client_order_id=client_order_id,
        ))
        broker_order = verify_order(client_order_id)
        if not broker_submission_confirmed(broker_order):
            events.append(log(
                "exit_submit_unverified", contract_symbol=contract_symbol,
                underlying=underlying, reason=reason, client_order_id=client_order_id,
                response_status=(response or {}).get("status") if isinstance(response, dict) else None,
            ))
            continue
        record.update({
            "status": "exit_pending", "exit_reason": reason,
            "last_exit_check_date": now_et.date().isoformat(),
            "exit_client_order_id": client_order_id,
            "exit_broker_order_id": broker_order.get("id"),
            "exit_submitted_at": now_et.isoformat(),
            "exit_limit": exit_limit,
            "exit_trigger_pnl": executable_pnl,
        })
        events.append(log(
            "exit_submitted", contract_symbol=contract_symbol,
            underlying=underlying, reason=reason, qty=qty,
            limit_price=exit_limit, executable_pnl=executable_pnl,
            client_order_id=client_order_id, broker_order_id=broker_order.get("id"),
            underlying_price=round(spot, 4),
            underlying_stop=_f(record.get("underlying_stop")),
            **ratchet_evidence(record.get("ratchet")),
        ))

    save_state(state)
    active_contracts = [
        symbol for symbol, record in managed.items()
        if record.get("status") in {"entry_pending", "open", "exit_pending"}
    ]
    return {
        "enabled": True,
        "managed": len(active_contracts),
        "managed_contracts": active_contracts,
        "broker_option_positions": sum(symbol in positions for symbol in active_contracts),
        "events": events,
    }
