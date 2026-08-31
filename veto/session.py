"""MCP-backed trading-session truth for autonomous entry decisions.

No daily risk counter in this module is process-local.  Every snapshot is
rebuilt from Alpaca's clock, calendar, orders, positions, and account so a
restart cannot forget a filled trade or an opening order that is still live.
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import config, mcp_client

ET = ZoneInfo("America/New_York")
_OPTION_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_REVISION_RE = re.compile(r"^(veto-[a-z0-9-]+)-r(\d+)$", re.I)
_FILLED_STATES = {"filled", "partially_filled"}


def _rows(payload, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except (TypeError, ValueError):
        return datetime.now(ET)


def _optional_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except (TypeError, ValueError):
        return None


def _calendar_time(day: datetime, value: object) -> datetime | None:
    raw = str(value or "").strip().replace(":", "")
    if len(raw) != 4 or not raw.isdigit():
        return None
    hour, minute = int(raw[:2]), int(raw[2:])
    if hour > 23 or minute > 59:
        return None
    return datetime.combine(day.date(), time(hour, minute), tzinfo=ET)


def _leg_intents(order: dict) -> set[str]:
    intents = {
        str(leg.get("position_intent") or "").lower()
        for leg in (order.get("legs") or [])
        if isinstance(leg, dict)
    }
    parent_intent = str(order.get("position_intent") or "").lower()
    if parent_intent:
        intents.add(parent_intent)
    return intents


def is_opening_order(order: dict) -> bool:
    return bool(_leg_intents(order) & {"buy_to_open", "sell_to_open"})


def is_closing_order(order: dict) -> bool:
    return bool(_leg_intents(order) & {"buy_to_close", "sell_to_close"})


def is_veto_order(order: dict) -> bool:
    return str(order.get("client_order_id") or "").lower().startswith("veto-")


def is_option_mr_order(order: dict) -> bool:
    return str(order.get("client_order_id") or "").lower().startswith("paca-callmr-")


def logical_trade_id(order: dict) -> str:
    """Group cancel/re-submit revisions into one logical entry decision."""
    client_id = str(order.get("client_order_id") or "").lower()
    match = _REVISION_RE.match(client_id)
    if match:
        return match.group(1)
    return client_id or str(order.get("id") or "unknown")


def next_revision_client_id(order: dict) -> str:
    """Return the next client ID while preserving one logical trade identity."""
    client_id = str(order.get("client_order_id") or "").lower()
    match = _REVISION_RE.match(client_id)
    if match:
        current_revision = int(match.group(2))
        return f"{match.group(1)}-r{current_revision + 1}"
    if client_id.startswith("veto-"):
        return f"{client_id}-r1"
    day = _timestamp(order.get("created_at")).strftime("%Y%m%d")
    return f"veto-open-{day}-{uuid.uuid4().hex[:12]}-r1"


def _has_fill(order: dict, activity_order_ids: set[str] | None = None) -> bool:
    status = str(order.get("status") or "").lower().split(".")[-1]
    if status in _FILLED_STATES or _f(order.get("filled_qty")) > 0:
        return True
    broker_ids = {str(order.get("id") or "")}
    broker_ids.update(
        str(leg.get("id") or "")
        for leg in (order.get("legs") or [])
        if isinstance(leg, dict)
    )
    return bool((activity_order_ids or set()) & broker_ids)


def count_veto_entries(session_orders: list[dict], activities: list[dict] | None = None) -> int:
    """Count filled parent spreads, using leg activities only as fill evidence."""
    activity_order_ids = {
        str(activity.get("order_id") or "")
        for activity in (activities or [])
        if str(activity.get("activity_type") or activity.get("type") or "").lower()
        in {"fill", "partial_fill"}
    }
    return len({
        logical_trade_id(order)
        for order in session_orders
        if is_veto_order(order)
        and is_opening_order(order)
        and _has_fill(order, activity_order_ids)
    })


def count_option_mr_entries(
    session_orders: list[dict], activities: list[dict] | None = None
) -> int:
    """Count filled long-call MR entries from broker state, never memory."""
    activity_order_ids = {
        str(activity.get("order_id") or "")
        for activity in (activities or [])
        if str(activity.get("activity_type") or activity.get("type") or "").lower()
        in {"fill", "partial_fill"}
    }
    return len({
        str(order.get("client_order_id") or order.get("id") or "").lower()
        for order in session_orders
        if str(order.get("client_order_id") or "").lower().startswith("paca-callmr-open-")
        and str(order.get("side") or "").lower() == "buy"
        and is_opening_order(order)
        and _has_fill(order, activity_order_ids)
    })


def _is_option_position(position: dict) -> bool:
    return bool(_OPTION_RE.match(str(position.get("symbol") or "")))


def _spread_count(option_positions: list[dict]) -> int:
    """Count credit-spread exposure, which is what full-BP sizing consumes.

    The second strategy's long call is a debit position with no short leg. It
    must not read as an open spread: doing so would report the account as fully
    deployed and block the credit-spread lane for as long as the call is held.
    """
    short_legs = 0
    unknown = 0
    for position in option_positions:
        qty = _f(position.get("qty"))
        side = str(position.get("side") or "").lower()
        if qty < 0 or side == "short":
            short_legs += 1
        elif qty > 0 or side == "long":
            continue  # a long leg: a spread's hedge, or a long-call position
        else:
            unknown += 1
    # A payload carrying neither side nor signed quantity still represents
    # paired option exposure; fail conservatively by counting those as pairs.
    return short_legs or math.ceil(unknown / 2)


@dataclass(frozen=True)
class SessionSnapshot:
    now_et: datetime
    session_date: str
    phase: str
    market_open: bool
    next_open: datetime | None
    regular_open: datetime | None
    regular_close: datetime | None
    open_orders: tuple[dict, ...]
    pending_opening_orders: tuple[dict, ...]
    pending_closing_orders: tuple[dict, ...]
    fill_activities: tuple[dict, ...]
    option_positions: tuple[dict, ...]
    open_spreads: int
    trades_today: int
    equity: float
    last_equity: float
    daily_pnl_usd: float
    daily_target_usd: float
    annual_target_reached: bool
    account_number: str
    account_status: str
    trading_blocked: bool
    account_blocked: bool
    trade_suspended_by_user: bool
    options_approved_level: int
    options_trading_level: int
    options_buying_power: float
    buying_power: float
    multiplier: float
    non_option_positions: tuple[dict, ...] = ()
    pending_option_mr_orders: tuple[dict, ...] = ()
    option_mr_entries_today: int = 0

    def gate_context(self) -> dict:
        return {
            "open_positions": self.open_spreads,
            "trades_today": self.trades_today,
            "held_symbols": [
                str(position.get("symbol") or "")
                for position in self.option_positions
            ],
            "annual_target_reached": self.annual_target_reached,
            "equity": round(self.equity, 2),
            "last_equity": round(self.last_equity, 2),
            "daily_target_usd": round(self.daily_target_usd, 2),
            "daily_pnl_usd": round(self.daily_pnl_usd, 2),
            "account_number": self.account_number,
            "account_status": self.account_status,
            "trading_blocked": self.trading_blocked,
            "account_blocked": self.account_blocked,
            "trade_suspended_by_user": self.trade_suspended_by_user,
            "options_approved_level": self.options_approved_level,
            "options_trading_level": self.options_trading_level,
            "options_buying_power": round(self.options_buying_power, 2),
            "buying_power": round(self.buying_power, 2),
            "multiplier": self.multiplier,
            "non_option_positions": len(self.non_option_positions),
            "option_mr_entries_today": self.option_mr_entries_today,
        }

    def public(self) -> dict:
        return {
            "now_et": self.now_et.isoformat(),
            "session_date": self.session_date,
            "phase": self.phase,
            "market_open": self.market_open,
            "next_open": self.next_open.isoformat() if self.next_open else None,
            "regular_open": self.regular_open.isoformat() if self.regular_open else None,
            "regular_close": self.regular_close.isoformat() if self.regular_close else None,
            "open_orders": len(self.open_orders),
            "pending_opening_orders": len(self.pending_opening_orders),
            "pending_closing_orders": len(self.pending_closing_orders),
            "fill_activities": len(self.fill_activities),
            "option_positions": len(self.option_positions),
            "open_spreads": self.open_spreads,
            "trades_today": self.trades_today,
            "daily_pnl_usd": round(self.daily_pnl_usd, 2),
            "daily_target_usd": round(self.daily_target_usd, 2),
            "annual_target_reached": self.annual_target_reached,
            "equity": round(self.equity, 2),
            "last_equity": round(self.last_equity, 2),
            "account_number": self.account_number,
            "account_status": self.account_status,
            "trading_blocked": self.trading_blocked,
            "account_blocked": self.account_blocked,
            "trade_suspended_by_user": self.trade_suspended_by_user,
            "options_approved_level": self.options_approved_level,
            "options_trading_level": self.options_trading_level,
            "options_buying_power": round(self.options_buying_power, 2),
            "buying_power": round(self.buying_power, 2),
            "multiplier": self.multiplier,
            "sizing_mode": config.SIZING_MODE,
            "non_option_positions": len(self.non_option_positions),
            "pending_option_mr_orders": len(self.pending_option_mr_orders),
            "option_mr_entries_today": self.option_mr_entries_today,
        }


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str
    detail: str
    waitable: bool = False


def build_snapshot(
    clock: dict,
    calendar_payload,
    open_orders_payload,
    session_orders_payload,
    positions_payload,
    account: dict,
    activities_payload=None,
) -> SessionSnapshot:
    now_et = _timestamp(clock.get("timestamp"))
    session_date = now_et.date().isoformat()
    calendar = next(
        (row for row in _rows(calendar_payload, "calendar")
         if str(row.get("date")) == session_date),
        None,
    )
    regular_open = _calendar_time(now_et, (calendar or {}).get("open"))
    regular_close = _calendar_time(now_et, (calendar or {}).get("close"))

    if calendar is None or regular_open is None or regular_close is None:
        phase = "closed_day"
    elif now_et < regular_open:
        phase = "pre_open"
    elif now_et >= regular_close:
        phase = "after_close"
    else:
        phase = "open"
    market_open = phase == "open" and bool(clock.get("is_open"))
    if phase == "open" and not market_open:
        phase = "broker_closed"

    open_orders = _rows(open_orders_payload, "orders")
    session_orders = _rows(session_orders_payload, "orders")
    fill_activities = _rows(activities_payload, "activities")
    option_positions = [
        position for position in _rows(positions_payload, "positions")
        if _is_option_position(position)
    ]
    non_option_positions = [
        position for position in _rows(positions_payload, "positions")
        if not _is_option_position(position)
        and str(position.get("asset_class") or "us_equity") == "us_equity"
    ]
    pending_option_mr_orders = [
        order for order in open_orders
        if str(order.get("client_order_id") or "").lower().startswith(
            ("paca-callmr-open-", "paca-callmr-exit-")
        )
    ]

    equity = _f(account.get("equity"))
    last_equity = _f(account.get("last_equity"))
    daily_pnl = equity - last_equity
    daily_rate = (
        (1.0 + config.ANNUAL_RETURN_TARGET)
        ** (1.0 / config.TRADING_DAYS_PER_YEAR)
        - 1.0
    )
    daily_target = last_equity * daily_rate

    return SessionSnapshot(
        now_et=now_et,
        session_date=session_date,
        phase=phase,
        market_open=market_open,
        next_open=_optional_timestamp(clock.get("next_open")),
        regular_open=regular_open,
        regular_close=regular_close,
        open_orders=tuple(open_orders),
        pending_opening_orders=tuple(o for o in open_orders if is_opening_order(o)),
        pending_closing_orders=tuple(o for o in open_orders if is_closing_order(o)),
        fill_activities=tuple(fill_activities),
        option_positions=tuple(option_positions),
        open_spreads=_spread_count(option_positions),
        trades_today=count_veto_entries(session_orders, fill_activities),
        equity=equity,
        last_equity=last_equity,
        daily_pnl_usd=daily_pnl,
        daily_target_usd=daily_target,
        annual_target_reached=daily_target > 0 and daily_pnl >= daily_target,
        account_number=str(account.get("account_number") or "").strip(),
        account_status=str(account.get("status") or "").upper(),
        trading_blocked=bool(account.get("trading_blocked", False)),
        account_blocked=bool(account.get("account_blocked", False)),
        trade_suspended_by_user=bool(account.get("trade_suspended_by_user", False)),
        options_approved_level=int(_f(account.get("options_approved_level"))),
        options_trading_level=int(_f(account.get("options_trading_level"))),
        options_buying_power=_f(account.get("options_buying_power")),
        buying_power=_f(account.get("buying_power")),
        multiplier=_f(account.get("multiplier")),
        non_option_positions=tuple(non_option_positions),
        pending_option_mr_orders=tuple(pending_option_mr_orders),
        option_mr_entries_today=count_option_mr_entries(session_orders, fill_activities),
    )


def entry_decision(
    snapshot: SessionSnapshot,
    reentry: dict | None = None,
) -> EntryDecision:
    if not config.ALPACA_ACCOUNT_ID:
        return EntryDecision(
            False,
            "account_id_unconfigured",
            "ALPACA_ACCOUNT_ID is required before paper orders can be submitted",
        )
    if snapshot.account_number != config.ALPACA_ACCOUNT_ID:
        return EntryDecision(
            False,
            "account_mismatch",
            f"MCP account {snapshot.account_number or 'missing'} does not match "
            f"configured submission account {config.ALPACA_ACCOUNT_ID}",
        )
    if snapshot.phase == "pre_open":
        return EntryDecision(False, "market_pre_open", "regular session has not opened", True)
    if not snapshot.market_open:
        return EntryDecision(False, "market_closed", f"session phase is {snapshot.phase}")
    if snapshot.pending_opening_orders:
        return EntryDecision(
            False, "opening_order_pending",
            f"{len(snapshot.pending_opening_orders)} opening order(s) still pending", True,
        )
    if snapshot.pending_closing_orders:
        return EntryDecision(
            False, "closing_order_pending",
            f"{len(snapshot.pending_closing_orders)} closing order(s) still pending", True,
        )
    if snapshot.non_option_positions:
        return EntryDecision(
            False,
            "options_only_violation",
            f"Alpaca reports {len(snapshot.non_option_positions)} non-option position(s); "
            "PacaPounce is competition-options-only",
            True,
        )
    # Full-buying-power mode deliberately deploys the complete broker budget
    # into the first approved spread. Once that exposure exists, another Poe
    # proposal cannot produce an affordable order and only burns model calls.
    # Keep this check before options eligibility: the normal post-fill state can
    # legitimately have $0 remaining BP, but the supervisor must keep waiting
    # (and monitoring) rather than stop the whole session as "ineligible".
    if config.FULL_BUYING_POWER and snapshot.open_spreads > 0:
        return EntryDecision(
            False,
            "full_capital_deployed",
            f"{snapshot.open_spreads} spread(s) open; full-buying-power mode has "
            f"${snapshot.options_buying_power:,.2f} options BP remaining",
            True,
        )
    option_eligible = (
        snapshot.account_status == "ACTIVE"
        and not snapshot.trading_blocked
        and not snapshot.account_blocked
        and not snapshot.trade_suspended_by_user
        and snapshot.options_approved_level >= config.MIN_OPTIONS_TRADING_LEVEL
        and snapshot.options_trading_level >= config.MIN_OPTIONS_TRADING_LEVEL
        and snapshot.options_buying_power > 0
    )
    if not option_eligible:
        return EntryDecision(
            False,
            "alpaca_options_ineligible",
            f"status={snapshot.account_status or 'missing'}, "
            f"approved=L{snapshot.options_approved_level}, "
            f"enabled=L{snapshot.options_trading_level}, "
            f"options BP=${snapshot.options_buying_power:,.2f}, "
            f"blocked={snapshot.trading_blocked or snapshot.account_blocked or snapshot.trade_suspended_by_user}",
        )
    if snapshot.trades_today >= config.MAX_TRADES_PER_DAY:
        return EntryDecision(
            False, "daily_trade_limit",
            f"Alpaca reports {snapshot.trades_today} filled PacaPounce entries today "
            f">= limit {config.MAX_TRADES_PER_DAY}",
        )
    if reentry and reentry.get("active") and not reentry.get("allowed"):
        return EntryDecision(
            False,
            str(reentry.get("reason") or "reentry_blocked"),
            str(reentry.get("detail") or "re-entry policy did not authorize a proposal"),
            True,
        )
    if snapshot.open_spreads >= config.MAX_OPEN_POSITIONS:
        return EntryDecision(
            False, "open_position_limit",
            f"Alpaca reports {snapshot.open_spreads} open spreads "
            f">= limit {config.MAX_OPEN_POSITIONS}", True,
        )
    if snapshot.annual_target_reached and not config.FULL_BUYING_POWER:
        return EntryDecision(
            False, "annual_target_reached",
            f"daily P&L ${snapshot.daily_pnl_usd:+.2f} >= "
            f"${snapshot.daily_target_usd:.2f} target",
        )
    return EntryDecision(True, "allowed", "MCP session preflight passed")


def capture() -> SessionSnapshot:
    """Build one authoritative entry snapshot entirely through Alpaca MCP."""
    clock = mcp_client.run(mcp_client.call("get_clock")) or {}
    if not isinstance(clock, dict) or clock.get("error"):
        raise RuntimeError(f"clock MCP call failed: {clock}")
    now_et = _timestamp(clock.get("timestamp"))
    day = now_et.date().isoformat()
    start = datetime.combine(now_et.date(), time.min, tzinfo=ET).isoformat()
    # get_account_activities paginates by token, and a truncated page would
    # under-count today's filled entries against the daily cap.
    calendar, open_orders, session_orders, activities, positions, account = mcp_client.run(
        mcp_client.call_many_all_pages([
            ("get_calendar", {"start": day, "end": day}),
            ("get_orders", {"status": "open", "limit": 500}),
            ("get_orders", {"status": "all", "limit": 500, "after": start}),
            ("get_account_activities", {"activity_types": "FILL", "after": day}),
            ("get_all_positions", {}),
            ("get_account_info", {}),
        ])
    )
    for name, payload in (
        ("calendar", calendar), ("open orders", open_orders),
        ("session orders", session_orders), ("positions", positions),
        ("activities", activities), ("account", account),
    ):
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"{name} MCP call failed: {payload['error']}")
    return build_snapshot(
        clock, calendar, open_orders, session_orders, positions,
        account if isinstance(account, dict) else {}, activities,
    )
