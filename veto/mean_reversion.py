"""MCP-only NDX30 mean-reversion strategy for Paper staging.

The policy is intentionally frozen to the independently tested specification:
one 15:45 ET scan on normal sessions, price above a rising SMA200, Wilder
RSI(2) below 10, 0.5% equity risk at a 2x ATR14 stop, at most 20% notional,
one new position per day, and at most three concurrent stock positions.  Exit
when the 15:30 completed-bar price recovers above EMA5 or after three sessions.

Python calculates every number. Poe receives only the top numerical candidate
and timestamped Alpaca MCP news, and may approve/veto event risk plus write the
thesis. It never sizes, prices, selects a symbol, or chooses an exit.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
import uuid
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

from . import config, llm, mcp_client

ET = ZoneInfo("America/New_York")
ENTRY_PREFIX = "paca-mr-open-"
EXIT_PREFIX = "paca-mr-exit-"
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
        state = json.loads(config.STOCK_MR_STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    _atomic_json(config.STOCK_MR_STATE_FILE, state)


def log(kind: str, **payload) -> dict:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": "NDX30_MR_01",
        "kind": kind,
        **payload,
    }
    config.STOCK_MR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with config.STOCK_MR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
    return row


def load_log() -> list[dict]:
    try:
        lines = config.STOCK_MR_LOG.read_text(encoding="utf-8").splitlines()
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


def position_size(equity: float, buying_power: float, price: float, atr: float) -> int:
    stop_distance = config.STOCK_MR_STOP_ATR_MULTIPLE * atr
    if min(equity, buying_power, price, stop_distance) <= 0:
        return 0
    by_risk = math.floor(equity * config.STOCK_MR_EQUITY_RISK_PCT / stop_distance)
    by_notional = math.floor(equity * config.STOCK_MR_MAX_NOTIONAL_PCT / price)
    by_broker = math.floor(buying_power / price)
    return max(0, min(by_risk, by_notional, by_broker))


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
            and rsi < config.STOCK_MR_RSI_MAX
        ),
    }


def fetch_signals(symbols: list[str], now_et: datetime) -> tuple[list[dict], dict]:
    start = (now_et.date() - timedelta(days=420)).isoformat()
    end = now_et.isoformat()
    symbol_csv = ",".join(symbols)
    daily_payload, intraday_payload = mcp_client.run(mcp_client.call_many([
        ("get_stock_bars", {
            "symbols": symbol_csv, "timeframe": "1Day", "start": start,
            "end": end, "limit": 10_000, "adjustment": "all", "feed": "sip",
            "sort": "asc",
        }),
        ("get_stock_bars", {
            "symbols": symbol_csv, "timeframe": "15Min",
            "start": datetime.combine(now_et.date(), wall_time(9, 30), tzinfo=ET).isoformat(),
            "end": end, "limit": 2_000, "adjustment": "all", "feed": "sip",
            "sort": "asc",
        }),
    ]))
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
                repair_calls.append(("get_stock_bars", {
                    "symbols": symbol, "timeframe": "1Day", "start": start,
                    "end": end, "limit": 500, "adjustment": "all", "feed": "sip",
                    "sort": "asc",
                }))
                repair_index.append((symbol, "daily"))
            if not intraday_by_symbol[symbol]:
                repair_calls.append(("get_stock_bars", {
                    "symbols": symbol, "timeframe": "15Min",
                    "start": datetime.combine(
                        now_et.date(), wall_time(9, 30), tzinfo=ET
                    ).isoformat(),
                    "end": end, "limit": 100, "adjustment": "all", "feed": "sip",
                    "sort": "asc",
                }))
                repair_index.append((symbol, "intraday"))
        repaired = mcp_client.run(mcp_client.call_many(repair_calls))
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


def select_candidate(signals: list[dict], held_symbols: set[str]) -> dict | None:
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
    return min(
        best_by_issuer.values(), key=lambda row: (row["rsi2"], row["symbol"]),
        default=None,
    )


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


def candidate_brief(candidate: dict, qty: int, stop_price: float,
                    news_text: str, now_et: datetime) -> str:
    return f"""Paper candidate selected by frozen NDX30_MR_01 policy.
Observed at: {now_et.isoformat()}
Symbol: {candidate['symbol']}
15:30 completed-bar price: {candidate['price']:.2f}
Wilder RSI(2): {candidate['rsi2']:.2f} (hard rule < {config.STOCK_MR_RSI_MAX:.2f})
SMA200: {candidate['sma200']:.2f}; previous SMA200: {candidate['previous_sma200']:.2f}
EMA5: {candidate['ema5']:.2f}; ATR14: {candidate['atr14']:.2f}
Deterministic order: buy {qty} shares; broker stop {stop_price:.2f}
Deterministic exit: 15:45 ET recovery above EMA5, or {config.STOCK_MR_MAX_HOLD_SESSIONS} sessions.
Invalidation: price reaches the broker stop at {stop_price:.2f}.

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


def _normal_entry_window(snapshot) -> tuple[bool, str]:
    if not snapshot.market_open or snapshot.regular_close is None:
        return False, "regular market is not open"
    if snapshot.regular_close.time().replace(tzinfo=None) != wall_time(16):
        return False, f"early-close session ends {snapshot.regular_close.time()}"
    start = wall_time(15, config.STOCK_MR_DECISION_MINUTE)
    end = wall_time(15, min(config.STOCK_MR_DECISION_MINUTE + 10, 59))
    current = snapshot.now_et.time().replace(tzinfo=None)
    return start <= current <= end, f"decision window {start.strftime('%H:%M')}-{end.strftime('%H:%M')} ET"


def _write_decision(state: dict, snapshot, status: str, checks: list[dict], **payload) -> dict:
    state["last_scan_date"] = snapshot.session_date
    state["last_decision"] = {"status": status, "at": snapshot.now_et.isoformat(), **payload}
    save_state(state)
    return log("decision", status=status, checks=checks, session_date=snapshot.session_date, **payload)


def maybe_enter(snapshot) -> dict | None:
    """Run at most one broker-reconciled Paper entry decision per regular day."""
    if not config.STOCK_MR_ENABLED:
        return None
    in_window, window_detail = _normal_entry_window(snapshot)
    if not in_window:
        return None
    state = load_state()
    if state.get("last_scan_date") == snapshot.session_date:
        return None

    checks = [
        _check("paper_account_identity", bool(config.ALPACA_ACCOUNT_ID) and
               snapshot.account_number == config.ALPACA_ACCOUNT_ID,
               f"MCP={snapshot.account_number or 'missing'} configured={config.ALPACA_ACCOUNT_ID or 'missing'}"),
        _check("regular_1545_window", True, window_detail),
        _check("account_active", snapshot.account_status == "ACTIVE" and not (
            snapshot.trading_blocked or snapshot.account_blocked or snapshot.trade_suspended_by_user
        ), f"status={snapshot.account_status}; blocked={snapshot.trading_blocked or snapshot.account_blocked or snapshot.trade_suspended_by_user}"),
        _check("options_capital_clear", snapshot.open_spreads == 0 and not snapshot.pending_opening_orders,
               f"spreads={snapshot.open_spreads}; pending option opens={len(snapshot.pending_opening_orders)}"),
        _check("stock_position_limit", len(snapshot.stock_positions) < config.STOCK_MR_MAX_POSITIONS,
               f"{len(snapshot.stock_positions)} open < {config.STOCK_MR_MAX_POSITIONS}"),
        _check("daily_entry_limit", snapshot.stock_entries_today < config.STOCK_MR_MAX_ENTRIES_PER_DAY,
               f"broker reports {snapshot.stock_entries_today} MR entries today"),
        _check("pending_stock_order", not snapshot.pending_stock_orders,
               f"{len(snapshot.pending_stock_orders)} unresolved MR entry/exit orders"),
    ]
    if not all(check["passed"] for check in checks):
        # Give an option fill/close a few supervisor cycles to reconcile, but
        # make a final auditable decision near the end of the ten-minute window.
        if snapshot.now_et.minute < config.STOCK_MR_DECISION_MINUTE + 8:
            return {"strategy": "NDX30_MR_01", "status": "waiting", "checks": checks}
        return _write_decision(state, snapshot, "VETOED", checks,
                               reason="portfolio_or_broker_preflight")

    signals, errors = fetch_signals(config.STOCK_MR_UNIVERSE, snapshot.now_et)
    data_ok = not errors and len(signals) == len(config.STOCK_MR_UNIVERSE)
    checks.append(_check(
        "complete_sip_data", data_ok,
        f"{len(signals)}/{len(config.STOCK_MR_UNIVERSE)} symbols complete; errors={errors or 'none'}",
    ))
    if not data_ok:
        return _write_decision(state, snapshot, "VETOED", checks, reason="incomplete_sip_data")

    held = {str(position.get("symbol") or "").upper() for position in snapshot.stock_positions}
    candidate = select_candidate(signals, held)
    signal_count = sum(bool(signal.get("passes")) for signal in signals)
    checks.append(_check(
        "mean_reversion_signal", candidate is not None,
        f"{signal_count} symbols pass price>SMA200, rising SMA200, RSI(2)<{config.STOCK_MR_RSI_MAX:g}",
    ))
    if candidate is None:
        return _write_decision(
            state, snapshot, "NO_SIGNAL", checks, reason="no_qualified_mean_reversion",
            scan={"symbols": len(signals), "raw_signals": signal_count},
        )

    qty = position_size(snapshot.equity, snapshot.buying_power,
                        candidate["price"], candidate["atr14"])
    stop_price = round(
        candidate["price"] - config.STOCK_MR_STOP_ATR_MULTIPLE * candidate["atr14"], 2
    )
    checks.extend([
        _check("deterministic_size", qty >= 1,
               f"qty={qty}; risk={config.STOCK_MR_EQUITY_RISK_PCT:.2%}; notional cap={config.STOCK_MR_MAX_NOTIONAL_PCT:.0%}"),
        _check("broker_buying_power", qty * candidate["price"] <= snapshot.buying_power,
               f"notional ${qty * candidate['price']:,.2f} <= buying power ${snapshot.buying_power:,.2f}"),
        _check("hard_stop", 0 < stop_price < candidate["price"],
               f"OTO stop ${stop_price:.2f} = signal - {config.STOCK_MR_STOP_ATR_MULTIPLE:g}x ATR14"),
    ])
    if not all(check["passed"] for check in checks):
        return _write_decision(state, snapshot, "VETOED", checks,
                               reason="deterministic_risk_gate", candidate=candidate)

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
        return _write_decision(state, snapshot, "VETOED", checks,
                               reason="news_context_unavailable", candidate=candidate)

    review, raw_reply = llm.review_stock_candidate(
        candidate_brief(candidate, qty, stop_price, news_text, snapshot.now_et)
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
        return _write_decision(
            state, snapshot, "VETOED", checks, reason="ai_event_risk",
            candidate=candidate, qty=qty, stop_price=stop_price, ai_review=review,
            raw_reply_chars=len(raw_reply), news_count=news_count,
            earnings_calendar_verified=False,
        )

    client_order_id = (
        f"{ENTRY_PREFIX}{snapshot.now_et.strftime('%Y%m%d')}-"
        f"{candidate['symbol'].lower()}-{uuid.uuid4().hex[:6]}"
    )
    response = mcp_client.run(mcp_client.call(
        "place_stock_order",
        symbol=candidate["symbol"], side="buy", qty=str(qty), type="market",
        time_in_force="day", extended_hours=False, client_order_id=client_order_id,
        order_class="oto", stop_loss_stop_price=f"{stop_price:.2f}",
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
        positions[candidate["symbol"]] = {
            "status": "entry_pending",
            "symbol": candidate["symbol"],
            "signal_date": snapshot.session_date,
            "signal_time": candidate["signal_time"],
            "signal_price": candidate["price"],
            "qty": qty,
            "atr14": candidate["atr14"],
            "initial_stop": stop_price,
            "entry_client_order_id": client_order_id,
            "entry_broker_order_id": broker_order.get("id"),
            "thesis": review.get("thesis"),
            "event_risk": review.get("event_risk"),
            "earnings_calendar_verified": False,
        }
    return _write_decision(
        state, snapshot, "SUBMITTED" if submitted else "VETOED", checks,
        reason="approved" if submitted else "broker_reconciliation_failed",
        candidate=candidate, qty=qty, stop_price=stop_price, ai_review=review,
        news_count=news_count, raw_reply_chars=len(raw_reply),
        earnings_calendar_verified=False,
        execution={
            "submitted": submitted, "client_order_id": client_order_id,
            "broker_order_id": (broker_order or {}).get("id"),
            "response_status": (response or {}).get("status") if isinstance(response, dict) else None,
            "broker_status": broker_status or None,
        },
    )


def _stock_positions(payload) -> dict[str, dict]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in _rows(payload, "positions")
        if str(row.get("asset_class") or "us_equity") == "us_equity"
        and len(str(row.get("symbol") or "")) <= 10
    }


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


def _cancel_protective_stops(symbol: str, open_orders: list[dict]) -> list[str]:
    canceled = []
    for order in _flatten_orders(open_orders):
        if (
            str(order.get("symbol") or "").upper() == symbol
            and str(order.get("side") or "").lower() == "sell"
            and str(order.get("type") or order.get("order_type") or "").lower()
            in {"stop", "stop_limit", "trailing_stop"}
            and order.get("id")
        ):
            mcp_client.run(mcp_client.call("cancel_order_by_id", order_id=str(order["id"])))
            canceled.append(str(order["id"]))
    return canceled


def _stops_still_open(symbol: str) -> list[dict]:
    payload = mcp_client.run(mcp_client.call(
        "get_orders", status="open", symbols=symbol, limit=100, nested=True
    ))
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"stop reconciliation failed: {payload['error']}")
    return [
        order for order in _flatten_orders(_rows(payload, "orders"))
        if str(order.get("symbol") or "").upper() == symbol
        and str(order.get("side") or "").lower() == "sell"
        and str(order.get("type") or order.get("order_type") or "").lower()
        in {"stop", "stop_limit", "trailing_stop"}
    ]


def monitor_cycle(clock: dict, open_orders_payload, all_positions_payload,
                  account: dict, execute: bool) -> dict:
    """Reconcile managed stock positions and submit deterministic exits."""
    if not config.STOCK_MR_ENABLED:
        return {"enabled": False}
    now_et = _timestamp(clock.get("timestamp")) or datetime.now(ET)
    positions = _stock_positions(all_positions_payload)
    open_orders = _rows(open_orders_payload, "orders")
    state = load_state()
    managed = state.setdefault("positions", {})
    events = []

    for symbol, record in list(managed.items()):
        status = str(record.get("status") or "")
        broker_position = positions.get(symbol)
        if broker_position and status == "entry_pending":
            record.update({
                "status": "open",
                "opened_at": now_et.isoformat(),
                "filled_qty": _f(broker_position.get("qty")),
                "avg_entry_price": _f(broker_position.get("avg_entry_price")),
            })
            events.append(log("entry_filled", symbol=symbol, broker_position=broker_position))
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
                    "entry_not_filled", symbol=symbol, broker_status=broker_status,
                    client_order_id=record.get("entry_client_order_id"),
                ))
        elif not broker_position and status in {"open", "exit_pending"}:
            record.update({"status": "closed", "closed_at": now_et.isoformat()})
            event = log(
                "position_closed", symbol=symbol,
                reason=record.get("exit_reason") or "broker_stop_or_manual_close",
                entry_client_order_id=record.get("entry_client_order_id"),
                exit_client_order_id=record.get("exit_client_order_id"),
            )
            state["last_exit"] = event
            events.append(event)

    normal_window = (
        bool(clock.get("is_open"))
        and now_et.hour == 15
        and config.STOCK_MR_DECISION_MINUTE <= now_et.minute <= config.STOCK_MR_DECISION_MINUTE + 10
    )
    for symbol, record in managed.items():
        if record.get("status") != "open" or not normal_window:
            continue
        if record.get("last_exit_check_date") == now_et.date().isoformat():
            continue
        signals, errors = fetch_signals([symbol], now_et)
        signal = signals[0] if signals else None
        if errors or signal is None:
            events.append(log("exit_check_failed", symbol=symbol, errors=errors or "incomplete bars"))
            continue
        held_sessions = _holding_sessions(str(record.get("signal_date")), now_et.date())
        recovered = signal["price"] > signal["ema5"]
        timed_out = held_sessions >= config.STOCK_MR_MAX_HOLD_SESSIONS
        reason = "ema5_recovery" if recovered else (
            "max_hold_sessions" if timed_out else None
        )
        record["last_exit_check"] = {
            "price": signal["price"], "ema5": signal["ema5"],
            "held_sessions": held_sessions, "reason": reason,
        }
        if not reason:
            record["last_exit_check_date"] = now_et.date().isoformat()
            events.append(log(
                "exit_hold", symbol=symbol, price=signal["price"], ema5=signal["ema5"],
                held_sessions=held_sessions,
            ))
            continue
        if not execute:
            events.append(log("exit_would_submit", symbol=symbol, reason=reason))
            continue

        canceled = _cancel_protective_stops(symbol, open_orders)
        time.sleep(0.35 if canceled else 0.0)
        remaining_stops = _stops_still_open(symbol)
        if remaining_stops:
            events.append(log(
                "exit_blocked", symbol=symbol, reason="protective_stop_still_open",
                order_ids=[order.get("id") for order in remaining_stops],
            ))
            continue
        qty = abs(_f(positions.get(symbol, {}).get("qty")))
        if qty <= 0:
            continue
        client_order_id = (
            f"{EXIT_PREFIX}{now_et.strftime('%Y%m%d')}-{symbol.lower()}-{uuid.uuid4().hex[:6]}"
        )
        response = mcp_client.run(mcp_client.call(
            "place_stock_order", symbol=symbol, side="sell", qty=f"{qty:g}",
            type="market", time_in_force="day", extended_hours=False,
            client_order_id=client_order_id,
        ))
        broker_order = verify_order(client_order_id)
        if broker_order is None:
            events.append(log(
                "exit_submit_unverified", symbol=symbol, reason=reason,
                client_order_id=client_order_id,
                response_status=(response or {}).get("status") if isinstance(response, dict) else None,
            ))
            continue
        record.update({
            "status": "exit_pending", "exit_reason": reason,
            "last_exit_check_date": now_et.date().isoformat(),
            "exit_client_order_id": client_order_id,
            "exit_broker_order_id": broker_order.get("id"),
            "exit_submitted_at": now_et.isoformat(),
        })
        events.append(log(
            "exit_submitted", symbol=symbol, reason=reason, qty=qty,
            client_order_id=client_order_id, broker_order_id=broker_order.get("id"),
        ))

    save_state(state)
    return {
        "enabled": True,
        "managed": sum(record.get("status") in {"entry_pending", "open", "exit_pending"}
                       for record in managed.values()),
        "broker_stock_positions": len(positions),
        "events": events,
    }
