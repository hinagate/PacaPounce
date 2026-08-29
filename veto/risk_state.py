"""Restart-safe profit-ratchet and re-entry lifecycle state.

Alpaca remains the source of truth for positions, orders, fills, and buying
power.  This module persists only path-dependent policy facts that the broker
does not own: an executable-P&L high-water mark and the comparison baseline for
one possible re-entry after a profitable exit.
"""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config


ET = ZoneInfo("America/New_York")
STATE_VERSION = "1.0.0"


def _state_path(path: Path | None = None) -> Path:
    return path or config.RISK_STATE_FILE


def _blank() -> dict:
    return {"version": STATE_VERSION, "positions": {}, "last_exit": None}


def load(path: Path | None = None) -> dict:
    target = _state_path(path)
    if not target.exists():
        return _blank()
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return _blank()
        state.setdefault("version", STATE_VERSION)
        state.setdefault("positions", {})
        state.setdefault("last_exit", None)
        return state
    except (OSError, json.JSONDecodeError):
        # Existing positions recover their high-water from the append-only
        # session log. Broker-owned daily trade counts remain the independent
        # protection if this supplemental lifecycle file is unavailable.
        return _blank()


def save(state: dict, path: Path | None = None) -> None:
    target = _state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def spread_key(spread: dict) -> str:
    return f"{spread.get('short_symbol', '')}|{spread.get('long_symbol', '')}"


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except (TypeError, ValueError):
        return None


def _bootstrap_history(spread: dict, limit: int = 60) -> tuple[list[float], float]:
    """Recover this open position's P&L path from the monitor audit log."""
    path = config.SESSION_LOG
    if not path.exists():
        return [], 0.0
    short_symbol = spread.get("short_symbol")
    long_symbol = spread.get("long_symbol")
    values: list[float] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for monitored in row.get("monitored_spreads") or []:
                if (
                    monitored.get("short_symbol") == short_symbol
                    and monitored.get("long_symbol") == long_symbol
                    and monitored.get("pnl_executable") is not None
                ):
                    values.append(float(monitored["pnl_executable"]))
    except OSError:
        return [], 0.0
    return values[-limit:], max(values, default=0.0)


def _rolling_change_volatility(history: list[float]) -> float:
    window = history[-config.MONITOR_VOL_WINDOW_SAMPLES :]
    if len(window) < 3:
        return 0.0
    changes = [window[i] - window[i - 1] for i in range(1, len(window))]
    return statistics.stdev(changes) if len(changes) > 1 else 0.0


def observe(
    spread: dict,
    metrics: dict,
    now_et: datetime,
    path: Path | None = None,
) -> dict:
    """Update one spread's path and return deterministic ratchet evidence."""
    state = load(path)
    key = spread_key(spread)
    positions = state.setdefault("positions", {})
    position = positions.get(key)
    if not isinstance(position, dict):
        recovered, recovered_high = _bootstrap_history(spread)
        position = {
            "short_symbol": spread.get("short_symbol"),
            "long_symbol": spread.get("long_symbol"),
            "opened_credit": spread.get("entry_credit"),
            "history": recovered,
            "high_water_pnl": recovered_high,
            # Confirmations must be fresh after a restart even though the high
            # water itself is reconstructed.
            "breach_count": 0,
        }
        positions[key] = position

    pnl = float(metrics.get("pnl_executable") or 0.0)
    history = [float(value) for value in position.get("history") or []]
    history.append(pnl)
    history = history[-60:]
    high_water = max(float(position.get("high_water_pnl") or 0.0), pnl)
    max_profit = max(float(metrics.get("max_profit") or 0.0), 0.01)
    arm_threshold = config.MONITOR_RATCHET_ARM_PCT * max_profit
    armed = high_water >= arm_threshold

    change_volatility = _rolling_change_volatility(history)
    volatility_ratio = change_volatility / max_profit
    high_volatility = volatility_ratio >= config.MONITOR_HIGH_VOL_PCT_MAX_PROFIT
    giveback_pct = (
        config.MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT
        if high_volatility
        else config.MONITOR_RATCHET_GIVEBACK_PCT
    )
    trailing_floor = high_water * (1.0 - giveback_pct) if armed else 0.0

    nonpositive_slope = len(history) >= 3 and history[-1] <= history[-3]
    below_floor = (
        armed
        and pnl > 0
        and pnl <= trailing_floor
        and nonpositive_slope
        and bool(metrics.get("quote_ready"))
    )
    breach_count = int(position.get("breach_count") or 0) + 1 if below_floor else 0
    ratchet_exit = breach_count >= config.MONITOR_RATCHET_CONFIRMATIONS

    position.update({
        "history": history,
        "high_water_pnl": round(high_water, 2),
        "breach_count": breach_count,
        "last_pnl": round(pnl, 2),
        "last_seen": now_et.isoformat(),
        "ratchet_armed": armed,
        "change_volatility_usd": round(change_volatility, 2),
        "volatility_ratio": round(volatility_ratio, 6),
        "high_volatility": high_volatility,
        "trailing_floor": round(trailing_floor, 2),
    })
    save(state, path)
    return {
        "ratchet_armed": armed,
        "ratchet_high_water_pnl": round(high_water, 2),
        "ratchet_arm_threshold_pnl": round(arm_threshold, 2),
        "ratchet_giveback_pct": giveback_pct,
        "ratchet_trailing_floor_pnl": round(trailing_floor, 2),
        "ratchet_breach_count": breach_count,
        "ratchet_exit": ratchet_exit,
        "pnl_change_volatility_usd": round(change_volatility, 2),
        "pnl_volatility_ratio": round(volatility_ratio, 6),
        "pnl_volatility_high": high_volatility,
        "pnl_slope_nonpositive": nonpositive_slope,
    }


def _relative_quote_width(quote: dict | None) -> float:
    quote = quote or {}
    bid = float(quote.get("bid") or 0.0)
    ask = float(quote.get("ask") or 0.0)
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid > 0 and ask >= bid else 1.0


def _entry_baseline(spread: dict) -> dict | None:
    from . import ledger

    for row in reversed(ledger.load()):
        candidate = row.get("spread") or {}
        execution = row.get("execution") or {}
        verdict = row.get("verdict") or {}
        if not execution.get("submitted") or not verdict.get("approved"):
            continue
        if (
            candidate.get("short_symbol") == spread.get("short_symbol")
            and candidate.get("long_symbol") == spread.get("long_symbol")
        ):
            economics = verdict.get("economics") or {}
            maximum_loss = float(economics.get("max_loss_usd") or 0.0)
            quality = (
                float(economics.get("ev_net_usd") or 0.0) / maximum_loss
                if maximum_loss > 0 else 0.0
            )
            return {
                "quality": quality,
                "ev_net_usd": economics.get("ev_net_usd"),
                "max_loss_usd": economics.get("max_loss_usd"),
                "short_delta": economics.get("short_delta", candidate.get("short_delta")),
                "short_symbol": spread.get("short_symbol"),
                "long_symbol": spread.get("long_symbol"),
            }
    return None


def record_exit(
    spread: dict,
    metrics: dict,
    action: str,
    now_et: datetime,
    submission: dict,
    path: Path | None = None,
) -> dict:
    """Persist the comparison baseline when an atomic close is submitted."""
    state = load(path)
    baseline = _entry_baseline(spread)
    spot = float(metrics.get("spot") or 0.0)
    short_strike = float(spread.get("short_strike") or 0.0)
    safe_buffer = (
        spot - short_strike
        if spread.get("right") == "P"
        else short_strike - spot
    )
    state["last_exit"] = {
        "submitted_at": now_et.astimezone(ET).isoformat(),
        "cooldown_until": (
            now_et.astimezone(ET) + timedelta(minutes=config.REENTRY_COOLDOWN_MIN)
        ).isoformat(),
        "action": action,
        "eligible": action in {"profit_target", "profit_ratchet"},
        "reentry_consumed": False,
        "underlying": spread.get("underlying"),
        "right": spread.get("right"),
        "short_symbol": spread.get("short_symbol"),
        "long_symbol": spread.get("long_symbol"),
        "safe_buffer_pct": safe_buffer / spot if spot > 0 else None,
        "worst_relative_quote_width": max(
            _relative_quote_width(metrics.get("short_quote")),
            _relative_quote_width(metrics.get("long_quote")),
        ),
        "exit_executable_pnl": metrics.get("pnl_executable"),
        "exit_profit_captured": metrics.get("profit_captured"),
        "ratchet_high_water_pnl": metrics.get("ratchet_high_water_pnl"),
        "ratchet_trailing_floor_pnl": metrics.get("ratchet_trailing_floor_pnl"),
        "ratchet_breach_count": metrics.get("ratchet_breach_count"),
        "ratchet_giveback_pct": metrics.get("ratchet_giveback_pct"),
        "qty": spread.get("qty"),
        "max_profit": metrics.get("max_profit"),
        "pnl_volatility_ratio": metrics.get("pnl_volatility_ratio"),
        "pnl_volatility_high": metrics.get("pnl_volatility_high"),
        "post_exit_values": [],
        "post_exit_stable_since": None,
        "entry_baseline": baseline,
        "close_client_order_id": submission.get("client_order_id"),
    }
    save(state, path)
    return state["last_exit"]


def exit_market_watch(now_et: datetime, path: Path | None = None) -> dict | None:
    """Return the exited contract pair while its re-entry reset is active."""
    exit_state = load(path).get("last_exit")
    if not isinstance(exit_state, dict) or not exit_state.get("eligible"):
        return None
    submitted = _parse_time(exit_state.get("submitted_at"))
    if (
        submitted is None
        or submitted.date() != now_et.astimezone(ET).date()
        or exit_state.get("reentry_consumed")
    ):
        return None
    if not exit_state.get("short_symbol") or not exit_state.get("long_symbol"):
        return None
    return exit_state


def observe_post_exit_market(
    short_quote: dict,
    long_quote: dict,
    now_et: datetime,
    path: Path | None = None,
) -> dict:
    """Require ten calm minutes in the exited pair before re-entry.

    The value is the immediately executable closing debit of the old spread,
    converted to dollars at its prior quantity.  We care about changes in that
    value, not its level.
    """
    state = load(path)
    exit_state = state.get("last_exit")
    if not isinstance(exit_state, dict):
        return {"ready": False, "reason": "no_exit_state"}
    short_ask = float((short_quote or {}).get("ask") or 0.0)
    long_bid = float((long_quote or {}).get("bid") or 0.0)
    qty = max(int(exit_state.get("qty") or 0), 1)
    maximum_profit = max(float(exit_state.get("max_profit") or 0.0), 0.01)
    quotes_ready = short_ask > 0 and long_bid > 0
    value = max(short_ask - long_bid, 0.01) * 100 * qty if quotes_ready else 0.0
    values = [float(item) for item in exit_state.get("post_exit_values") or []]
    if quotes_ready:
        values.append(value)
    values = values[-60:]
    change_volatility = _rolling_change_volatility(values)
    volatility_ratio = change_volatility / maximum_profit
    liquid = max(
        _relative_quote_width(short_quote),
        _relative_quote_width(long_quote),
    ) <= config.MAX_LEG_SPREAD_PCT
    calm_now = (
        len(values) >= 3
        and volatility_ratio < config.MONITOR_HIGH_VOL_PCT_MAX_PROFIT
        and liquid
    )
    stable_since = _parse_time(exit_state.get("post_exit_stable_since"))
    if calm_now and stable_since is None:
        stable_since = now_et.astimezone(ET)
    elif not calm_now:
        stable_since = None
    stable_for = (
        (now_et.astimezone(ET) - stable_since).total_seconds() / 60.0
        if stable_since else 0.0
    )
    ready = stable_for >= config.REENTRY_STABLE_MIN
    exit_state.update({
        "post_exit_values": values,
        "post_exit_change_volatility_usd": round(change_volatility, 2),
        "post_exit_volatility_ratio": round(volatility_ratio, 6),
        "post_exit_liquid": liquid,
        "post_exit_stable_since": stable_since.isoformat() if stable_since else None,
        "post_exit_stable_minutes": round(stable_for, 2),
        "post_exit_market_ready": ready,
        "post_exit_last_seen": now_et.astimezone(ET).isoformat(),
    })
    save(state, path)
    return {
        "ready": ready,
        "calm_now": calm_now,
        "stable_minutes": round(stable_for, 2),
        "volatility_ratio": round(volatility_ratio, 6),
        "liquid": liquid,
    }


def reentry_status(now_et: datetime, path: Path | None = None) -> dict:
    """Describe whether today's last exit permits a fresh proposal."""
    exit_state = load(path).get("last_exit")
    if not isinstance(exit_state, dict):
        return {"active": False, "allowed": True, "reason": "first_entry"}
    submitted = _parse_time(exit_state.get("submitted_at"))
    if submitted is None or submitted.date() != now_et.astimezone(ET).date():
        return {"active": False, "allowed": True, "reason": "new_session"}
    common = {
        "active": True,
        "exit": exit_state,
        "bp_utilization": config.REENTRY_BP_UTILIZATION,
    }
    if exit_state.get("reentry_consumed"):
        return {
            **common,
            "allowed": False,
            "reason": "reentry_already_used",
            "detail": "the single same-day re-entry has already been submitted",
        }
    if not exit_state.get("eligible"):
        return {
            **common,
            "allowed": False,
            "reason": "reentry_risk_exit_lockout",
            "detail": f"{exit_state.get('action')} exit locks this session",
        }
    cooldown_until = _parse_time(exit_state.get("cooldown_until"))
    if cooldown_until and now_et.astimezone(ET) < cooldown_until:
        remaining = math.ceil(
            (cooldown_until - now_et.astimezone(ET)).total_seconds() / 60.0
        )
        return {
            **common,
            "allowed": False,
            "reason": "reentry_cooldown",
            "detail": f"profit exit cooling down; {remaining} minute(s) remaining",
            "cooldown_until": cooldown_until.isoformat(),
        }
    if not exit_state.get("post_exit_market_ready"):
        stable_minutes = float(exit_state.get("post_exit_stable_minutes") or 0.0)
        return {
            **common,
            "allowed": False,
            "reason": "reentry_volatility_reset",
            "detail": (
                f"exited spread has been calm for {stable_minutes:.1f}/"
                f"{config.REENTRY_STABLE_MIN} required minute(s)"
            ),
        }
    return {
        **common,
        "allowed": True,
        "reason": "reentry_comparison_required",
        "detail": "candidate must be materially better than the exited trade",
    }


def mark_reentry_submission(
    spread: dict,
    economics: dict,
    execution: dict,
    path: Path | None = None,
) -> None:
    state = load(path)
    exit_state = state.get("last_exit")
    if not isinstance(exit_state, dict):
        return
    maximum_loss = float(economics.get("max_loss_usd") or 0.0)
    exit_state["reentry_consumed"] = True
    exit_state["reentry_submission"] = {
        "at": datetime.now(ET).isoformat(),
        "client_order_id": execution.get("client_order_id"),
        "short_symbol": spread.get("short_symbol"),
        "long_symbol": spread.get("long_symbol"),
        "quality": (
            float(economics.get("ev_net_usd") or 0.0) / maximum_loss
            if maximum_loss > 0 else 0.0
        ),
        "qty": spread.get("qty"),
    }
    save(state, path)
