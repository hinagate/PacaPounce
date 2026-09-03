#!/usr/bin/env python
"""Paper monitor for PacaPounce option spreads and MR long calls.

The monitor treats Alpaca as the source of truth, polls through the official
Alpaca MCP server, and reconstructs each spread from the live option legs. It
never opens a new trade. Without ``--execute`` it is observation-only; with
``--execute`` it may chase an already-submitted opening order or atomically
close an open spread when a deterministic guard fires.

Exit policy:

* take profit when 50% of the opening credit can be captured;
* after 20% capture, trail executable P&L by 20% (10% in high volatility),
  requiring two confirmed observations and a non-positive slope;
* on non-expiry days, exit at 70% of defined max loss or beyond the long leg;
* on expiry day, suppress the ordinary stop and close only for late pin risk;
* stop polling when the regular market session closes.

Managed NDX30_CALL_MR_01 long calls use the same 30-second loop. It checks the
underlying 2x ATR stop during the regular session, then exits on EMA5 recovery
or the third normal session at 15:45 ET. Every exit is an options sell-to-close
limit order reconciled against Alpaca.

Usage:
    python scripts/monitor.py                         # observe only
    python scripts/monitor.py --execute               # paper auto-exits enabled
    python scripts/monitor.py --execute --interval 30
    python scripts/monitor.py --once                  # one diagnostic cycle
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, time as wall_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from veto import config, mcp_client, mean_reversion, risk_state, session, skew  # noqa: E402
from veto.builder import refresh_realized_vol  # noqa: E402
from veto.gates import expected_value, friction_usd  # noqa: E402
from veto.sizing import buying_power_contracts  # noqa: E402

LOG = config.SESSION_LOG
ET = ZoneInfo("America/New_York")
OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
CHASE_STEP = float(__import__("os").environ.get("PACAPOUNCE_CHASE_STEP", "0.01"))
# Exits that close at market because waiting is the risk. Everything else
# closes with a limit, and a resize never pre-empts one of these.
EMERGENCY_ACTIONS = {"stop_loss", "long_strike_breach", "pin_risk"}
CHASE_BP_REFRESH_ATTEMPTS = 3
CHASE_BP_REFRESH_INTERVAL_SEC = 0.5
CHASE_VERIFY_ATTEMPTS = 10
CHASE_VERIFY_INTERVAL_SEC = 0.5
_FAILED_ORDER_STATES = {
    "canceled", "done_for_day", "expired", "rejected", "replaced",
    "stopped", "suspended",
}


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rows(payload, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key, [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _verify_replacement_order(client_order_id: str) -> tuple[dict | None, object | None]:
    """Require the replacement to exist in Alpaca, not just in an MCP envelope."""
    last_error = None
    after = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    for attempt in range(CHASE_VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(CHASE_VERIFY_INTERVAL_SEC)
        payload = mcp_client.run(mcp_client.call(
            "get_orders", status="all", limit=500, after=after
        ))
        if isinstance(payload, dict) and payload.get("error"):
            last_error = payload["error"]
            continue
        for broker_order in _rows(payload, "orders"):
            if (
                str(broker_order.get("client_order_id") or "").lower()
                == client_order_id.lower()
                and broker_order.get("id")
            ):
                return broker_order, last_error
    return None, last_error


def log(kind: str, **payload) -> dict:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
    return row


def ensure_paper_only() -> None:
    """Refuse every monitor start unless both local layers are paper-pinned."""
    expected = "https://paper-api.alpaca.markets"
    if config.TRADE_BASE.rstrip("/") != expected:
        raise RuntimeError(f"refusing non-paper trade base: {config.TRADE_BASE}")
    if mcp_client._env().get("ALPACA_PAPER_TRADE", "").lower() != "true":
        raise RuntimeError("refusing MCP session without ALPACA_PAPER_TRADE=true")


def annual_target_status(account: dict) -> dict:
    """Translate the annual objective into today's live account benchmark."""
    equity = _f(account.get("equity"))
    last_equity = _f(account.get("last_equity"))
    daily_rate = (
        (1.0 + config.ANNUAL_RETURN_TARGET)
        ** (1.0 / config.TRADING_DAYS_PER_YEAR)
        - 1.0
    )
    daily_target_usd = last_equity * daily_rate
    daily_pnl = equity - last_equity
    return {
        "annual_rate": config.ANNUAL_RETURN_TARGET,
        "daily_rate": daily_rate,
        "equity": round(equity, 2),
        "last_equity": round(last_equity, 2),
        "annual_target_usd": round(last_equity * config.ANNUAL_RETURN_TARGET, 2),
        "daily_target_usd": round(daily_target_usd, 2),
        "daily_pnl": round(daily_pnl, 2),
        "daily_progress": daily_pnl / daily_target_usd if daily_target_usd > 0 else 0.0,
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
        "symbol": symbol,
        "underlying": root,
        "expiry": expiry,
        "right": right,
        "strike": int(strike_raw) / 1000,
    }


def option_positions(payload) -> list[dict]:
    rows = _rows(payload, "positions")
    return [row for row in rows if parse_occ(str(row.get("symbol", ""))) is not None]


def pair_spreads(positions: list[dict]) -> tuple[list[dict], list[str]]:
    """Pair equal-size short and long OCC legs into defined-risk spreads."""
    parsed: list[dict] = []
    errors: list[str] = []
    for position in positions:
        occ = parse_occ(str(position.get("symbol", "")))
        qty = _f(position.get("qty"))
        if occ is None or qty == 0:
            continue
        parsed.append({**occ, "qty": qty, "position": position})

    shorts = [leg for leg in parsed if leg["qty"] < 0]
    longs = [leg for leg in parsed if leg["qty"] > 0]
    used_long_symbols: set[str] = set()
    spreads: list[dict] = []

    for short in shorts:
        candidates = []
        for long_leg in longs:
            if long_leg["symbol"] in used_long_symbols:
                continue
            same_contract = (
                long_leg["underlying"] == short["underlying"]
                and long_leg["expiry"] == short["expiry"]
                and long_leg["right"] == short["right"]
                and abs(long_leg["qty"]) == abs(short["qty"])
            )
            protects_short = (
                long_leg["strike"] < short["strike"]
                if short["right"] == "P"
                else long_leg["strike"] > short["strike"]
            )
            if same_contract and protects_short:
                candidates.append(long_leg)

        if not candidates:
            errors.append(f"unpaired short option leg: {short['symbol']}")
            continue
        long_leg = min(candidates, key=lambda leg: abs(leg["strike"] - short["strike"]))
        used_long_symbols.add(long_leg["symbol"])

        entry_credit = (
            _f(short["position"].get("avg_entry_price"))
            - _f(long_leg["position"].get("avg_entry_price"))
        )
        if entry_credit <= 0:
            errors.append(
                f"unsupported non-credit pair: {short['symbol']}/{long_leg['symbol']}"
            )
            continue

        spreads.append({
            "underlying": short["underlying"],
            "expiry": short["expiry"].isoformat(),
            "right": short["right"],
            "qty": int(abs(short["qty"])),
            "short_symbol": short["symbol"],
            "long_symbol": long_leg["symbol"],
            "short_strike": short["strike"],
            "long_strike": long_leg["strike"],
            "width": abs(short["strike"] - long_leg["strike"]),
            "entry_credit": entry_credit,
            "broker_unrealized_pl": (
                _f(short["position"].get("unrealized_pl"))
                + _f(long_leg["position"].get("unrealized_pl"))
            ),
        })

    for long_leg in longs:
        if long_leg["symbol"] not in used_long_symbols:
            errors.append(f"unpaired long option leg: {long_leg['symbol']}")
    return spreads, errors


def _clock_time(clock: dict) -> datetime:
    raw = str(clock.get("timestamp") or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except ValueError:
        return datetime.now(ET)


def _option_quotes(payload) -> dict[str, dict]:
    if not isinstance(payload, dict):
        return {}
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, dict):
        snapshots = payload
    out: dict[str, dict] = {}
    for symbol, snapshot in snapshots.items():
        quote = (snapshot or {}).get("latestQuote") or {}
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        if bid > 0 and ask > 0 and ask >= bid:
            out[symbol] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    return out


def _stock_spots(payload) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    quotes = payload.get("quotes")
    if not isinstance(quotes, dict):
        quotes = payload
    out: dict[str, float] = {}
    for symbol, quote in quotes.items():
        bid, ask = _f((quote or {}).get("bp")), _f((quote or {}).get("ap"))
        value = (bid + ask) / 2 if bid and ask else (bid or ask)
        if value > 0:
            out[symbol] = value
    return out


def _order_symbols(order: dict) -> set[str]:
    return {
        str(leg.get("symbol"))
        for leg in (order.get("legs") or [])
        if leg.get("symbol")
    }


def _is_opening_order(order: dict) -> bool:
    intents = {
        str(leg.get("position_intent") or "")
        for leg in (order.get("legs") or [])
    }
    return bool(intents & {"buy_to_open", "sell_to_open"})


def _is_closing_order(order: dict) -> bool:
    intents = {
        str(leg.get("position_intent") or "")
        for leg in (order.get("legs") or [])
    }
    return bool(intents & {"buy_to_close", "sell_to_close"})


def decide_exit(
    metrics: dict,
    now_et: datetime,
    market_open: bool,
    profit_target: float = config.MONITOR_PROFIT_TARGET_PCT,
    stop_max_loss: float = config.MONITOR_STOP_MAX_LOSS_PCT,
    pin_buffer: float = config.MONITOR_PIN_BUFFER_USD,
    profit_exits: bool | None = None,
) -> tuple[str | None, str]:
    """Return a deterministic exit action and a human-readable decision.

    ``profit_exits`` False keeps the position to expiry unless the underlying
    breaches the long strike: the entry gate prices the spread on its terminal
    payoff, and on a $2-wide spread the profit target and trail were giving
    back most of that edge in early exits (see MONITOR_PROFIT_EXIT_ENABLED).
    """
    if profit_exits is None:
        profit_exits = config.MONITOR_PROFIT_EXIT_ENABLED
    if not market_open:
        return None, "market_closed"
    if not metrics.get("quote_ready") or metrics.get("spot", 0) <= 0:
        return None, "market_data_unavailable"

    projected_daily = metrics.get("projected_daily_pnl_after_exit")
    daily_target = metrics.get("account_daily_target_usd")
    if (
        not config.FULL_BUYING_POWER
        and
        projected_daily is not None
        and daily_target is not None
        and metrics.get("pnl_executable", 0) > 0
        and projected_daily >= daily_target
    ):
        return "annual_target_lock", (
            f"projected daily P&L ${projected_daily:.2f} >= ${daily_target:.2f} target"
        )

    if profit_exits and metrics["profit_captured"] >= profit_target:
        return "profit_target", f"captured {metrics['profit_captured']:.1%} of credit"

    if profit_exits and metrics.get("ratchet_exit"):
        volatility = "high-volatility " if metrics.get("pnl_volatility_high") else ""
        return "profit_ratchet", (
            f"{volatility}profit trail: executable ${metrics.get('pnl_executable', 0):+.2f} "
            f"<= ${metrics.get('ratchet_trailing_floor_pnl', 0):.2f} floor after "
            f"${metrics.get('ratchet_high_water_pnl', 0):.2f} high"
        )

    right = metrics["right"]
    spot = metrics["spot"]
    short_k = metrics["short_strike"]
    long_k = metrics["long_strike"]
    is_expiry = metrics["is_expiry_day"]

    if right == "P":
        beyond_long = spot <= long_k
        in_spread = long_k < spot < short_k
        safe_distance = spot - short_k
    else:
        beyond_long = spot >= long_k
        in_spread = short_k < spot < long_k
        safe_distance = short_k - spot

    if is_expiry:
        if now_et.time() >= wall_time(15, 30) and in_spread:
            return "pin_risk", "underlying between strikes in final 30 minutes"
        if safe_distance < pin_buffer:
            return None, f"expiry_near_short_strike ({safe_distance:+.2f})"
        return None, "expiry_hold_defined_risk"

    if beyond_long:
        return "long_strike_breach", "underlying moved beyond protective long strike"
    if metrics["loss_used"] >= stop_max_loss:
        return "stop_loss", f"used {metrics['loss_used']:.1%} of defined max loss"

    # The entry gate's own model, re-run on the held position (hold_ev_review).
    # Ordered after the defined-risk exits: those are market orders because
    # waiting is the risk; this is a limit because the risk is expectancy.
    hold_ev = metrics.get("hold_ev") or {}
    if hold_ev.get("close"):
        return "hold_ev_negative", (
            f"holding has negative expectancy: {hold_ev.get('detail', '')} "
            f"({hold_ev.get('negatives')} consecutive reviews)"
        )
    return None, "hold" if profit_exits else "hold_to_expiry"


def spread_metrics(
    spread: dict,
    quotes: dict[str, dict],
    spot: float,
    now_et: datetime,
    market_open: bool,
    account_target: dict | None = None,
) -> dict:
    short_q = quotes.get(spread["short_symbol"])
    long_q = quotes.get(spread["long_symbol"])
    base = {
        **spread,
        "spot": round(spot, 4),
        "is_expiry_day": spread["expiry"] == now_et.date().isoformat(),
        "quote_ready": bool(short_q and long_q),
    }
    if not short_q or not long_q:
        action, decision = decide_exit(base, now_et, market_open)
        return {**base, "action": action, "decision": decision}

    mid_debit = max(short_q["mid"] - long_q["mid"], 0.0)
    executable_debit = max(short_q["ask"] - long_q["bid"], 0.01)
    qty = spread["qty"]
    entry_credit = spread["entry_credit"]
    max_profit = entry_credit * 100 * qty
    max_loss = max(spread["width"] - entry_credit, 0.01) * 100 * qty
    pnl_mid = (entry_credit - mid_debit) * 100 * qty
    pnl_executable = (entry_credit - executable_debit) * 100 * qty
    metrics = {
        **base,
        "mid_debit": round(mid_debit, 4),
        "executable_debit": round(executable_debit, 4),
        "pnl_mid": round(pnl_mid, 2),
        "pnl_executable": round(pnl_executable, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        # Profit exits use the executable natural price, not an optimistic mid.
        "profit_captured": (entry_credit - executable_debit) / entry_credit,
        "loss_used": max(0.0, -pnl_mid) / max_loss,
        "short_quote": short_q,
        "long_quote": long_q,
    }
    if account_target:
        projected_daily = (
            account_target["daily_pnl"]
            - spread.get("broker_unrealized_pl", 0.0)
            + pnl_executable
        )
        metrics.update({
            "account_daily_target_usd": account_target["daily_target_usd"],
            "account_daily_pnl": account_target["daily_pnl"],
            "projected_daily_pnl_after_exit": round(projected_daily, 2),
        })
    action, decision = decide_exit(metrics, now_et, market_open)
    return {**metrics, "action": action, "decision": decision}


def hold_expectancy(
    spread: dict,
    quotes: dict[str, dict],
    spot: float,
    now_et: datetime,
) -> dict:
    """Re-run the entry gate's EV model on a held spread.

    The gate approved the spread because, at the entry credit, realised vol
    (EWMA) with the chain's skew and the equity risk premium as drift said the
    expected terminal loss was below what the position was paid. The same
    question is asked of the live position with one substitution: the credit
    is the executable debit to close now, because that is what the position
    still earns by holding rather than closing. Friction is not subtracted -
    closing pays it, holding does not, and the executable debit already
    contains it. A position that fails this test has negative expectancy
    against closing under the model that opened it.
    """
    short_q = quotes.get(spread["short_symbol"])
    long_q = quotes.get(spread["long_symbol"])
    dte = (date.fromisoformat(spread["expiry"]) - now_et.date()).days
    if not short_q or not long_q:
        return {"evaluated": False, "reason": "quotes_unavailable"}
    if dte < 1:
        # Expiry day delivers the payoff; the pin-risk rule owns it.
        return {"evaluated": False, "reason": "expiry_day"}
    if spot <= 0:
        return {"evaluated": False, "reason": "spot_unavailable"}
    is_put = spread["right"] == "P"
    underlying = spread["underlying"]
    lo, hi = min(spread["short_strike"], spread["long_strike"]), max(
        spread["short_strike"], spread["long_strike"]
    )
    (chain_payload,) = mcp_client.run(mcp_client.call_many_all_pages([
        ("get_option_chain", {
            "underlying_symbol": underlying,
            "expiration_date": spread["expiry"],
            "type": "put" if is_put else "call",
            "strike_price_gte": lo - 5,
            "strike_price_lte": hi + 5,
            "limit": 400,
        }),
    ]))
    snapshots = (chain_payload or {}).get("snapshots") or {}
    legs, deltas = [], {}
    for symbol, snapshot in snapshots.items():
        occ = parse_occ(symbol)
        quote = (snapshot or {}).get("latestQuote") or {}
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        if occ and bid > 0 and ask > 0:
            legs.append({
                "strike": occ["strike"],
                "mid": (ask + bid) / 2,
                "bid": bid,
                "ask": ask,
                "iv": _f((snapshot or {}).get("impliedVolatility")),
            })
            deltas[symbol] = abs(_f(((snapshot or {}).get("greeks") or {}).get("delta")))
    smile = skew.build_smile(spot, dte / 252, legs, is_put=is_put)
    atm = _f(skew.atm_vol(smile, spot)) if smile else 0.0
    rvol = _f(refresh_realized_vol(underlying))
    if rvol <= 0:
        # Without a realised-vol level the model degrades to the chain's own
        # probabilities, which price every spread at ~zero. Fail safe: hold.
        return {"evaluated": False, "reason": "realized_vol_unavailable"}
    executable_debit = max(short_q["ask"] - long_q["bid"], 0.01)
    mid_debit = max(short_q["mid"] - long_q["mid"], 0.0)
    econ = expected_value(
        executable_debit, spread["width"],
        deltas.get(spread["short_symbol"], 0.0), deltas.get(spread["long_symbol"], 0.0),
        spot=spot, short_strike=spread["short_strike"], long_strike=spread["long_strike"],
        dte=dte, realized_vol=rvol, smile=smile, is_put=is_put,
    )
    hold_ev = _f(econ.get("ev_gross_usd"))
    expected_loss = _f(econ.get("expected_loss_real_usd"))
    hold_ok = hold_ev > config.MIN_EV_USD
    return {
        "evaluated": True,
        "hold_ok": hold_ok,
        "checked_at": now_et.isoformat(),
        "dte": dte,
        "spot": round(spot, 4),
        "executable_debit": round(executable_debit, 4),
        "mid_debit": round(mid_debit, 4),
        "realized_vol": round(rvol, 4),
        "atm_iv": round(atm, 4),
        "vrp_ratio": econ.get("vrp_ratio"),
        "ev_basis": econ.get("ev_basis"),
        "expected_loss_usd": round(expected_loss, 2),
        "hold_ev_usd": round(hold_ev, 2),
        "hold_ev_zero_drift_usd": econ.get("ev_at_zero_drift_usd"),
        "hold_ev_total_usd": round(hold_ev * float(spread.get("qty") or 0), 2),
        "min_ev_usd": config.MIN_EV_USD,
        "detail": (
            f"hold EV ${hold_ev:+.2f}/contract: expected terminal loss "
            f"${expected_loss:.2f} vs ${executable_debit * 100:.2f} retained by "
            f"holding at {executable_debit:.2f} debit; realised {rvol:.1%} vs "
            f"ATM implied {atm:.1%} ({econ.get('ev_basis')}), {dte}d to expiry"
        ),
    }


# Per-pair review state. Lives in the process: a restart costs one fresh
# evaluation, never a stale decision.
_HOLD_EV: dict[str, dict] = {}


def hold_ev_review(
    spread: dict,
    quotes: dict[str, dict],
    spot: float,
    now_et: datetime,
    market_open: bool,
    evaluate=None,
) -> dict | None:
    """Schedule hold_expectancy and confirm its verdict across reviews.

    Evaluated at most every MONITOR_HOLD_EV_INTERVAL_MIN minutes from
    MONITOR_HOLD_EV_AFTER_ET, so the opening rotation's quotes never decide
    anything. ``close`` needs MONITOR_HOLD_EV_CONFIRMATIONS consecutive
    negative evaluations; one positive resets the count; an evaluation that
    could not run (expiry day, missing data, an error) leaves it unchanged.
    """
    if not config.MONITOR_HOLD_EV_EXIT_ENABLED:
        return None
    evaluate = evaluate or hold_expectancy
    key = f"{spread['short_symbol']}|{spread['long_symbol']}"
    session = now_et.date().isoformat()
    record = _HOLD_EV.get(key)
    if record is None or record["session"] != session:
        record = {"session": session, "negatives": 0, "last": None, "checked_at": None}
        _HOLD_EV[key] = record
    hours, minutes = (int(part) for part in config.MONITOR_HOLD_EV_AFTER_ET.split(":"))
    due = (
        market_open
        and now_et.time() >= wall_time(hours, minutes)
        and (
            record["checked_at"] is None
            or (now_et - record["checked_at"]).total_seconds()
            >= config.MONITOR_HOLD_EV_INTERVAL_MIN * 60
        )
    )
    if due:
        try:
            result = evaluate(spread, quotes, spot, now_et)
        except Exception as exc:  # noqa: BLE001 - a failed review must not stop the monitor
            result = {"evaluated": False, "reason": f"{type(exc).__name__}: {exc}"}
            log("hold_ev_error", short_symbol=spread["short_symbol"],
                long_symbol=spread["long_symbol"], error=result["reason"])
        record["checked_at"] = now_et
        record["last"] = result
        if result.get("evaluated"):
            record["negatives"] = 0 if result["hold_ok"] else record["negatives"] + 1
    last = record["last"] or {"evaluated": False, "reason": "not_yet_reviewed"}
    return {
        **last,
        "negatives": record["negatives"],
        "confirmations": config.MONITOR_HOLD_EV_CONFIRMATIONS,
        "close": record["negatives"] >= config.MONITOR_HOLD_EV_CONFIRMATIONS,
        "last_review_at": record["checked_at"].isoformat() if record["checked_at"] else None,
    }


def budget_resize(spread: dict, equity: float) -> dict:
    """Contracts a held spread may keep under the lane's current equity share.

    Equity x SPREAD_EQUITY_PCT over the defined loss per contract, floored.
    Live options buying power is deliberately not in the formula: a held
    position's collateral is already posted, so remaining BP is near zero by
    construction and would close everything.
    """
    max_loss_per_contract = max(_f(spread.get("width")) - _f(spread.get("entry_credit")), 0.01) * 100
    budget = max(_f(equity), 0.0) * config.SPREAD_EQUITY_PCT
    allowed = int(budget // max_loss_per_contract)
    qty = int(_f(spread.get("qty")))
    return {
        "budget_usd": round(budget, 2),
        "share_pct": config.SPREAD_EQUITY_PCT,
        "max_loss_per_contract": round(max_loss_per_contract, 2),
        "allowed_qty": allowed,
        "close_qty": max(qty - allowed, 0),
    }


def close_order_request(
    spread: dict,
    action: str,
    executable_debit: float,
    client_order_id: str,
) -> dict:
    """Build an atomic close request using Alpaca's documented leg intents."""
    emergency = action in EMERGENCY_ACTIONS
    request = {
        "qty": str(spread["qty"]),
        "type": "market" if emergency else "limit",
        "time_in_force": "day",
        "order_class": "mleg",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": spread["short_symbol"],
                "side": "buy",
                "ratio_qty": "1",
                "position_intent": "buy_to_close",
            },
            {
                "symbol": spread["long_symbol"],
                "side": "sell",
                "ratio_qty": "1",
                "position_intent": "sell_to_close",
            },
        ],
    }
    if not emergency:
        request["limit_price"] = (
            f"{max(0.01, executable_debit + config.MONITOR_LIMIT_SLIPPAGE):.2f}"
        )
    return request


def submit_close(
    spread: dict,
    metrics: dict,
    now_et: datetime | None = None,
) -> dict:
    ensure_paper_only()
    bucket = int(time.time() // 60)
    digest = hashlib.sha1(
        f"{spread['short_symbol']}|{spread['long_symbol']}|{metrics['action']}|{bucket}".encode()
    ).hexdigest()[:16]
    client_order_id = f"veto-close-{digest}"
    request = close_order_request(
        spread, metrics["action"], metrics["executable_debit"], client_order_id
    )
    result = mcp_client.run(mcp_client.call("place_option_order", **request))
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"Alpaca close rejected: {result['error']}")
    submission = {
        "action": metrics["action"],
        "client_order_id": client_order_id,
        "order_type": request["type"],
        "limit_price": request.get("limit_price"),
        "response": result,
    }
    if metrics["action"] not in {"annual_target_resize", "budget_resize"}:
        risk_state.record_exit(
            spread,
            metrics,
            metrics["action"],
            now_et or datetime.now(ET),
            submission,
        )
    return submission


def ev_at_credit(
    credit: float,
    spot: float,
    short_k: float,
    long_k: float,
    dte: float,
    smile: dict,
    ratio: float,
    friction: float,
    is_put: bool = True,
) -> float:
    if dte <= 0 or not smile:
        return credit * 100 - friction
    expected_loss = skew.expected_loss(
        spot,
        short_k,
        long_k,
        max(dte, 0.5) / 252,
        smile,
        ratio,
        steps=150,
        drift=config.DRIFT_ANNUAL,
        is_put=is_put,
    )
    return (credit - expected_loss) * 100 - friction


def _build_opening_context(order: dict) -> dict:
    """Rebuild pricing context only when an opening order still needs chasing."""
    legs = order.get("legs") or []
    short_leg = next((leg for leg in legs if str(leg.get("side")) == "sell"), None)
    long_leg = next((leg for leg in legs if str(leg.get("side")) == "buy"), None)
    if not short_leg or not long_leg:
        raise ValueError("opening order does not contain one short and one long leg")
    short_occ, long_occ = parse_occ(short_leg["symbol"]), parse_occ(long_leg["symbol"])
    if short_occ is None or long_occ is None:
        raise ValueError("opening order contains an invalid OCC symbol")

    # get_option_snapshot and get_option_chain both accept page_token; the
    # latest-quote lookup returns none and completes on its first page.
    quote_payload, spot_payload, chain_payload = mcp_client.run(mcp_client.call_many_all_pages([
        ("get_option_snapshot", {
            "symbols": f"{short_leg['symbol']},{long_leg['symbol']}",
            "feed": config.OPTIONS_FEED,
        }),
        ("get_stock_latest_quote", {"symbols": short_occ["underlying"], "feed": "sip"}),
        ("get_option_chain", {
            "underlying_symbol": short_occ["underlying"],
            "expiration_date": short_occ["expiry"].isoformat(),
            "type": "put" if short_occ["right"] == "P" else "call",
            "strike_price_gte": min(short_occ["strike"], long_occ["strike"]) - 5,
            "strike_price_lte": max(short_occ["strike"], long_occ["strike"]) + 5,
            "limit": 400,
        }),
    ]))
    quotes = _option_quotes(quote_payload)
    spots = _stock_spots(spot_payload)
    current_spot = spots.get(short_occ["underlying"], 0.0)

    snapshots = (chain_payload or {}).get("snapshots") or {}
    chain_legs = []
    for symbol, snapshot in snapshots.items():
        occ = parse_occ(symbol)
        quote = (snapshot or {}).get("latestQuote") or {}
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        if occ and bid > 0 and ask > 0:
            chain_legs.append({
                "strike": occ["strike"],
                "mid": (ask + bid) / 2,
                "bid": bid,
                "ask": ask,
                "iv": _f((snapshot or {}).get("impliedVolatility")),
            })
    smile = skew.build_smile(
        current_spot,
        max((short_occ["expiry"] - date.today()).days, 1) / 252,
        chain_legs,
        is_put=short_occ["right"] == "P",
    )
    atm = skew.atm_vol(smile, current_spot)
    from veto.builder import realized_vol

    realized = realized_vol(short_occ["underlying"])
    short_q, long_q = quotes.get(short_leg["symbol"]), quotes.get(long_leg["symbol"])
    if not short_q or not long_q:
        raise ValueError("opening order is missing a live quote for one or more legs")
    friction = friction_usd(
        short_q["bid"], short_q["ask"], long_q["bid"], long_q["ask"]
    )
    natural_credit = round(max(short_q["bid"] - long_q["ask"], 0.0), 2)
    return {
        "spot": current_spot,
        "short_k": short_occ["strike"],
        "long_k": long_occ["strike"],
        "dte": max((short_occ["expiry"] - date.today()).days, 0),
        "smile": smile,
        "ratio": realized / atm if atm > 0 else 1.0,
        "friction": friction,
        "natural_credit": natural_credit,
        "is_put": short_occ["right"] == "P",
    }


def chase_opening_order(order: dict, options_buying_power: float) -> dict:
    """Improve an opening order while preserving EV and broker collateral.

    A pending full-capital order reserves its collateral, so Alpaca can report
    near-zero remaining options buying power. Preflight against the remaining
    balance plus the old order's releasable defined loss, then cancel, refresh
    the broker balance, and size once more from that post-cancel source of truth.
    """
    context = _build_opening_context(order)
    current_credit = abs(_f(order.get("limit_price")))
    natural_credit = _f(context.pop("natural_credit", 0.0))
    if natural_credit <= 0:
        return log("chase_stop", reason="no executable natural credit", credit=natural_credit)
    new_credit = round(max(natural_credit, round(current_credit - CHASE_STEP, 2)), 2)
    if new_credit >= current_credit:
        return log(
            "chase_stop",
            reason="order is already at or through natural credit",
            credit=current_credit,
            natural_credit=natural_credit,
        )
    if new_credit <= 0.05:
        return log("chase_stop", reason="credit floor reached", credit=new_credit)
    # This is an executable limit, not a midpoint estimate. Subtracting the
    # midpoint-to-natural friction again would double-count the same concession.
    ev = ev_at_credit(new_credit, **{**context, "friction": 0.0})
    if ev <= 0:
        return log("chase_stop", reason="EV would go negative", credit=new_credit, ev=round(ev, 2))

    old_qty = int(_f(order.get("qty")))
    width = abs(context["short_k"] - context["long_k"])
    old_max_loss_per_contract = round((width - current_credit) * 100, 2)
    releasable_collateral = max(old_qty, 0) * max(old_max_loss_per_contract, 0.0)
    preflight_buying_power = max(options_buying_power, 0.0) + releasable_collateral
    max_loss_per_contract = round((width - new_credit) * 100, 2)
    preflight = buying_power_contracts(
        preflight_buying_power,
        max_loss_per_contract,
        max_n=max(old_qty, 0),
    )
    if preflight["contracts"] < 1:
        return log(
            "chase_stop",
            reason="replacement cannot be collateralized",
            credit=new_credit,
            options_buying_power=round(options_buying_power, 2),
            releasable_collateral=round(releasable_collateral, 2),
            max_loss_per_contract=max_loss_per_contract,
        )

    cancel_result = mcp_client.run(
        mcp_client.call("cancel_order_by_id", order_id=order["id"])
    )
    if isinstance(cancel_result, dict) and cancel_result.get("error"):
        return log(
            "chase_stop",
            reason="broker did not confirm cancellation",
            credit=current_credit,
            response=cancel_result,
        )

    refreshed_buying_power = 0.0
    refresh_error = None
    for attempt in range(CHASE_BP_REFRESH_ATTEMPTS):
        if attempt:
            time.sleep(CHASE_BP_REFRESH_INTERVAL_SEC)
        account = mcp_client.run(mcp_client.call("get_account_info"))
        if isinstance(account, dict) and account.get("error"):
            refresh_error = account["error"]
            continue
        refreshed_buying_power = _f(
            account.get("options_buying_power") if isinstance(account, dict) else None
        )
        if refreshed_buying_power >= max_loss_per_contract:
            break

    final_size = buying_power_contracts(
        refreshed_buying_power,
        max_loss_per_contract,
        max_n=max(old_qty, 0),
    )
    new_qty = final_size["contracts"]
    if new_qty < 1:
        return log(
            "chase_stop",
            reason="collateral unavailable after cancellation",
            credit=new_credit,
            options_buying_power=round(refreshed_buying_power, 2),
            max_loss_per_contract=max_loss_per_contract,
            refresh_error=refresh_error,
        )

    replacement = {
        "qty": str(new_qty),
        "type": "limit",
        "time_in_force": "day",
        "order_class": "mleg",
        "limit_price": f"{-new_credit:.2f}",
        "client_order_id": session.next_revision_client_id(order),
        "legs": [
            {
                "symbol": leg["symbol"],
                "side": leg["side"],
                "ratio_qty": str(leg.get("ratio_qty") or "1"),
                "position_intent": leg.get("position_intent"),
            }
            for leg in (order.get("legs") or [])
        ],
    }
    result = mcp_client.run(mcp_client.call("place_option_order", **replacement))
    evidence = {
        "old_credit": current_credit,
        "new_credit": new_credit,
        "natural_credit": natural_credit,
        "old_qty": old_qty,
        "new_qty": new_qty,
        "released_collateral": round(releasable_collateral, 2),
        "refreshed_options_buying_power": round(refreshed_buying_power, 2),
        "max_loss_per_contract": max_loss_per_contract,
        "total_defined_loss": round(new_qty * max_loss_per_contract, 2),
        "ev_at_new": round(ev, 2),
        "client_order_id": replacement["client_order_id"],
        "response": result,
    }
    if isinstance(result, dict) and result.get("error"):
        return log(
            "chase_stop",
            reason="replacement submission rejected",
            **evidence,
        )

    broker_order, reconciliation_error = _verify_replacement_order(
        replacement["client_order_id"]
    )
    if broker_order is None:
        return log(
            "chase_stop",
            reason="replacement not found in Alpaca get_orders",
            reconciliation_error=reconciliation_error,
            **evidence,
        )

    broker_status = str(broker_order.get("status") or "unknown").lower()
    broker_filled_qty = _f(broker_order.get("filled_qty"))
    if broker_status in _FAILED_ORDER_STATES and broker_filled_qty <= 0:
        return log(
            "chase_stop",
            reason=f"replacement reached broker as {broker_status}",
            broker_order_id=broker_order["id"],
            broker_status=broker_status,
            broker_filled_qty=broker_filled_qty,
            **evidence,
        )

    return log(
        "chase",
        broker_order_id=broker_order["id"],
        broker_status=broker_status,
        broker_filled_qty=broker_filled_qty,
        **evidence,
    )


def cycle(execute: bool = False) -> dict:
    clock_payload, order_payload, position_payload, account_payload = mcp_client.run(mcp_client.call_many([
        ("get_clock", {}),
        ("get_orders", {"status": "open", "nested": True}),
        ("get_all_positions", {}),
        ("get_account_info", {}),
    ]))
    for name, payload in (
        ("clock", clock_payload),
        ("orders", order_payload),
        ("positions", position_payload),
        ("account", account_payload),
    ):
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"{name} MCP call failed: {payload['error']}")

    clock = clock_payload if isinstance(clock_payload, dict) else {}
    now_et = _clock_time(clock)
    market_open = bool(clock.get("is_open"))
    orders = _rows(order_payload, "orders")
    positions = option_positions(position_payload)
    account = account_payload if isinstance(account_payload, dict) else {}
    live_account = str(account.get("account_number") or "").strip()
    if not config.ALPACA_ACCOUNT_ID:
        raise RuntimeError("ALPACA_ACCOUNT_ID is required before monitoring can execute")
    if live_account != config.ALPACA_ACCOUNT_ID:
        raise RuntimeError(
            f"MCP account {live_account or 'missing'} does not match configured "
            f"submission account {config.ALPACA_ACCOUNT_ID}"
        )
    target = annual_target_status(account)
    opening_orders = [
        order for order in orders
        if _is_opening_order(order) and session.is_veto_order(order)
    ]
    closing_orders = [order for order in orders if _is_closing_order(order)]

    state: dict = {
        "is_open": market_open,
        "now": now_et.isoformat(),
        "open_orders": len(orders),
        "opening_orders": len(opening_orders),
        "closing_orders": len(closing_orders),
        "position_legs": len(positions),
        "execute": execute,
        "feed": config.OPTIONS_FEED,
        "account_number": live_account,
        "annual_target": target,
        "target_max_contracts": config.MAX_CONTRACTS,
    }

    # The same paper-only monitor owns the second options strategy's underlying
    # stop plus deterministic EMA5/time exit. This keeps run.py --loop a single
    # command and prevents an unmonitored option entry.
    state["option_mean_reversion"] = mean_reversion.monitor_cycle(
        clock, order_payload, position_payload, account, execute
    )

    if execute and market_open:
        chase_results = []
        for order in opening_orders:
            try:
                chase_results.append(chase_opening_order(
                    order,
                    _f(account.get("options_buying_power")),
                ))
            except Exception as exc:
                chase_results.append({"error": f"{type(exc).__name__}: {exc}"})
        if chase_results:
            state["chase_results"] = chase_results

    mr_contracts = set(state["option_mean_reversion"].get("managed_contracts") or [])
    spread_positions = [
        position for position in positions
        if str(position.get("symbol") or "").upper() not in mr_contracts
    ]
    spreads, pairing_errors = pair_spreads(spread_positions)
    state["spreads"] = len(spreads)
    if pairing_errors:
        state["pairing_errors"] = pairing_errors
    if positions:
        state["unrealized_pl"] = round(sum(_f(pos.get("unrealized_pl")) for pos in positions), 2)

    if spreads:
        symbols = sorted({
            symbol
            for spread in spreads
            for symbol in (spread["short_symbol"], spread["long_symbol"])
        })
        underlyings = sorted({spread["underlying"] for spread in spreads})
        quote_payload, stock_payload = mcp_client.run(mcp_client.call_many([
            ("get_option_snapshot", {
                "symbols": ",".join(symbols),
                "feed": config.OPTIONS_FEED,
            }),
            ("get_stock_latest_quote", {
                "symbols": ",".join(underlyings),
                "feed": "sip",
            }),
        ]))
        quotes = _option_quotes(quote_payload)
        spots = _stock_spots(stock_payload)
        monitored = []
        close_submissions = []

        for spread in spreads:
            metrics = spread_metrics(
                spread,
                quotes,
                spots.get(spread["underlying"], 0.0),
                now_et,
                market_open,
                target,
            )
            ratchet = risk_state.observe(spread, metrics, now_et)
            metrics.update(ratchet)
            review = hold_ev_review(
                spread, quotes, spots.get(spread["underlying"], 0.0), now_et, market_open
            )
            if review is not None:
                metrics["hold_ev"] = review
            metrics["action"], metrics["decision"] = decide_exit(
                metrics, now_et, market_open
            )
            spread_symbols = {spread["short_symbol"], spread["long_symbol"]}
            pending_close = any(
                spread_symbols <= _order_symbols(order) for order in closing_orders
            )
            metrics["pending_close"] = pending_close
            if pending_close:
                metrics["action"] = None
                metrics["decision"] = "close_order_pending"
            elif spread["qty"] > config.MAX_CONTRACTS:
                close_qty = spread["qty"] - config.MAX_CONTRACTS
                metrics["action"] = "annual_target_resize"
                metrics["close_qty"] = close_qty
                metrics["resize_to_qty"] = config.MAX_CONTRACTS
                metrics["decision"] = (
                    f"annual target cap trims {close_qty}; keep {config.MAX_CONTRACTS}"
                )
                if execute:
                    try:
                        submission = submit_close(
                            {**spread, "qty": close_qty}, metrics, now_et
                        )
                        submission["close_qty"] = close_qty
                        close_submissions.append(submission)
                        metrics["decision"] = "submitted_annual_target_resize"
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        close_submissions.append({
                            "action": metrics["action"],
                            "close_qty": close_qty,
                            "error": error,
                        })
                        metrics["decision"] = f"resize_submit_failed: {error}"
            elif (
                config.MONITOR_BUDGET_RESIZE_ENABLED
                and not metrics.get("action")
                and budget_resize(spread, target.get("equity", 0.0))["close_qty"] > 0
            ):
                # Only while the monitor would otherwise hold: any exit,
                # protective or expectancy, takes the whole position first.
                resize = budget_resize(spread, target.get("equity", 0.0))
                close_qty = resize["close_qty"]
                metrics["action"] = "budget_resize"
                metrics["close_qty"] = close_qty
                metrics["resize_to_qty"] = resize["allowed_qty"]
                metrics["budget_resize"] = resize
                metrics["decision"] = (
                    f"spread share {resize['share_pct']:.0%} of equity "
                    f"(${resize['budget_usd']:,.0f}) allows {resize['allowed_qty']} "
                    f"at ${resize['max_loss_per_contract']:.0f} defined loss each; "
                    f"trims {close_qty}, keeps {resize['allowed_qty']}"
                )
                if execute:
                    try:
                        submission = submit_close(
                            {**spread, "qty": close_qty}, metrics, now_et
                        )
                        submission["close_qty"] = close_qty
                        close_submissions.append(submission)
                        metrics["decision"] = f"submitted_budget_resize: {metrics['decision']}"
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        close_submissions.append({
                            "action": metrics["action"],
                            "close_qty": close_qty,
                            "error": error,
                        })
                        metrics["decision"] = f"resize_submit_failed: {error}"
                else:
                    metrics["decision"] = f"would_budget_resize: {metrics['decision']}"
            elif metrics.get("action") and execute:
                try:
                    submission = submit_close(spread, metrics, now_et)
                    close_submissions.append(submission)
                    metrics["decision"] = f"submitted_{metrics['action']}"
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    close_submissions.append({"action": metrics["action"], "error": error})
                    metrics["decision"] = f"close_submit_failed: {error}"
            elif metrics.get("action"):
                metrics["decision"] = f"would_{metrics['action']}: {metrics['decision']}"
            monitored.append(metrics)

        state["monitored_spreads"] = monitored
        if close_submissions:
            state["close_submissions"] = close_submissions
    elif market_open:
        # After a profitable close the monitor keeps observing the exited pair.
        # Re-entry remains blocked until its executable quote path has been calm
        # and liquid for the configured reset window.
        watch = risk_state.exit_market_watch(now_et)
        if watch:
            symbols = f"{watch['short_symbol']},{watch['long_symbol']}"
            watch_payload = mcp_client.run(mcp_client.call(
                "get_option_snapshot",
                symbols=symbols,
                feed=config.OPTIONS_FEED,
            ))
            watch_quotes = _option_quotes(watch_payload)
            short_quote = watch_quotes.get(watch["short_symbol"])
            long_quote = watch_quotes.get(watch["long_symbol"])
            if short_quote and long_quote:
                state["reentry_market_reset"] = risk_state.observe_post_exit_market(
                    short_quote, long_quote, now_et
                )
            else:
                state["reentry_market_reset"] = {
                    "ready": False,
                    "reason": "exited_pair_quotes_unavailable",
                }

    return log("cycle", **state)


def _validate_policy() -> None:
    if not 0 < config.MONITOR_PROFIT_TARGET_PCT < 1:
        raise ValueError("PACAPOUNCE_MONITOR_PROFIT_TARGET_PCT must be between 0 and 1")
    if not 0 < config.MONITOR_STOP_MAX_LOSS_PCT <= 1:
        raise ValueError("PACAPOUNCE_MONITOR_STOP_MAX_LOSS_PCT must be between 0 and 1")
    if config.MONITOR_INTERVAL_SEC < 10:
        raise ValueError("PACAPOUNCE_MONITOR_INTERVAL_SEC must be at least 10 seconds")
    if not 0 < config.MONITOR_RATCHET_ARM_PCT < config.MONITOR_PROFIT_TARGET_PCT:
        raise ValueError("PACAPOUNCE_MONITOR_RATCHET_ARM_PCT must be below the hard target")
    if not 0 < config.MONITOR_RATCHET_GIVEBACK_PCT < 1:
        raise ValueError("PACAPOUNCE_MONITOR_RATCHET_GIVEBACK_PCT must be between 0 and 1")
    if not 0 < config.MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT < 1:
        raise ValueError("PACAPOUNCE_MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT must be between 0 and 1")
    if config.MONITOR_RATCHET_CONFIRMATIONS < 1:
        raise ValueError("PACAPOUNCE_MONITOR_RATCHET_CONFIRMATIONS must be positive")
    if config.MONITOR_VOL_WINDOW_SAMPLES < 3:
        raise ValueError("PACAPOUNCE_MONITOR_VOL_WINDOW_SAMPLES must be at least 3")
    try:
        hours, minutes = (int(part) for part in config.MONITOR_HOLD_EV_AFTER_ET.split(":"))
        wall_time(hours, minutes)
    except (TypeError, ValueError):
        raise ValueError("PACAPOUNCE_MONITOR_HOLD_EV_AFTER_ET must be HH:MM") from None
    if not 0 < config.REENTRY_BP_UTILIZATION <= 1:
        raise ValueError("PACAPOUNCE_REENTRY_BP_UTILIZATION must be in (0, 1]")
    if config.REENTRY_COOLDOWN_MIN < 0 or config.REENTRY_STABLE_MIN < 1:
        raise ValueError("re-entry cooldown must be non-negative and stability positive")
    if not 0 < config.ANNUAL_RETURN_TARGET < 1:
        raise ValueError("PACAPOUNCE_ANNUAL_RETURN_TARGET must be between 0 and 1")
    if config.TRADING_DAYS_PER_YEAR <= 0:
        raise ValueError("PACAPOUNCE_TRADING_DAYS_PER_YEAR must be positive")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PacaPounce intraday paper spread monitor"
    )
    parser.add_argument("--once", action="store_true", help="run one polling cycle")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="allow paper opening-order chase and deterministic auto-exits",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.MONITOR_INTERVAL_SEC,
        help="target seconds between cycle starts (minimum 10)",
    )
    args = parser.parse_args()

    ensure_paper_only()
    _validate_policy()
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds")

    mode = "PAPER AUTO-EXIT" if args.execute else "OBSERVE ONLY"
    print(
        f"PacaPounce monitor: {mode} | interval={args.interval}s | feed={config.OPTIONS_FEED} | "
        f"objective={config.SIZING_MODE} | "
        f"annual_target={config.ANNUAL_RETURN_TARGET:.1%} | "
        f"profit={config.MONITOR_PROFIT_TARGET_PCT:.0%} | "
        f"stop={config.MONITOR_STOP_MAX_LOSS_PCT:.0%} max loss",
        flush=True,
    )

    while True:
        started = time.monotonic()
        try:
            row = cycle(execute=args.execute)
            message = (
                f"[{row.get('now')}] open={row.get('is_open')} "
                f"orders={row.get('open_orders')} spreads={row.get('spreads')}"
            )
            if "unrealized_pl" in row:
                message += f" broker_uPL=${row['unrealized_pl']:+.2f}"
            annual = row.get("annual_target") or {}
            if annual:
                message += (
                    f" daily=${annual.get('daily_pnl', 0):+.2f}/"
                    f"${annual.get('daily_target_usd', 0):.2f}"
                )
            for monitored in row.get("monitored_spreads", []):
                message += (
                    f" | {monitored['underlying']} {monitored['short_strike']:.0f}/"
                    f"{monitored['long_strike']:.0f} spot={monitored['spot']:.2f} "
                    f"execPnL=${monitored.get('pnl_executable', 0):+.2f} "
                    f"{monitored['decision']}"
                )
            option_mr = row.get("option_mean_reversion") or {}
            if option_mr.get("enabled"):
                message += (
                    f" | MR calls={option_mr.get('broker_option_positions', 0)}"
                    f"/{option_mr.get('managed', 0)} managed"
                )
                if option_mr.get("events"):
                    message += f" events={len(option_mr['events'])}"
            print(message, flush=True)
            if not row.get("is_open"):
                print("regular market session closed - monitor exiting for today", flush=True)
                return 0
        except Exception as exc:
            log("error", error=f"{type(exc).__name__}: {exc}")
            print(f"monitor error: {type(exc).__name__}: {exc}", flush=True)

        if args.once:
            return 0
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    sys.exit(main())
