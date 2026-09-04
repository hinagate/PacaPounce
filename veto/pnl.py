"""Realized and open P&L from the Alpaca paper account.

The account is the source of truth. The verdict ledger records what the agent
DECIDED; this module records what actually HAPPENED, and reconciles the two.
Never report P&L from local state alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import ledger, mcp_client, mean_reversion


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rows(res, key: str) -> list[dict]:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        v = res.get(key)
        if isinstance(v, list):
            return v
    return []


def open_positions() -> list[dict]:
    """All agent-visible positions, marked to market by Alpaca."""
    res = mcp_client.run(mcp_client.call("get_all_positions"))
    out = []
    for p in _rows(res, "positions"):
        sym = str(p.get("symbol", ""))
        out.append({
            "symbol": sym,
            "asset_class": p.get("asset_class") or (
                "us_option" if len(sym) > 10 else "us_equity"
            ),
            "qty": _f(p.get("qty")),
            "avg_entry": _f(p.get("avg_entry_price")),
            "market_value": _f(p.get("market_value")),
            "unrealized_pl": _f(p.get("unrealized_pl")),
            "cost_basis": _f(p.get("cost_basis")),
        })
    return out


def closed_activity(days: int = 30) -> list[dict]:
    """Stock and option fills, including still-open inventory."""
    after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    try:
        res = mcp_client.run(mcp_client.call_all_pages(
            "get_account_activities", activity_types="FILL", after=after))
    except Exception:
        return []
    out = []
    for a in _rows(res, "activities"):
        sym = str(a.get("symbol", ""))
        multiplier = 100 if len(sym) > 10 else 1
        qty, price = _f(a.get("qty")), _f(a.get("price"))
        side = str(a.get("side", ""))
        # Selling premium is a cash inflow; buying is an outflow.
        sign = 1 if side.startswith("sell") else -1
        out.append({
            "symbol": sym,
            "side": side,
            "qty": qty,
            "price": price,
            "multiplier": multiplier,
            "cash": round(sign * qty * price * multiplier, 2),
            "ts": a.get("transaction_time") or a.get("date"),
        })
    return out


def _matched_fills(activity: list[dict]) -> list[dict]:
    """FIFO-match opening and closing fills per OCC symbol.

    Summing fill cash is not realized P&L while an opening position remains: a
    short option's opening credit is paired with a liability.  Matching buys and
    sells removes that open inventory and keeps only completed round trips.

    Returns one row per match so the realized total and the per-position table
    are two views of the same arithmetic. Two implementations of this matching
    would eventually disagree, and both numbers are shown to judges.
    """
    inventory: dict[str, list[list]] = {}
    matches: list[dict] = []
    for fill in sorted(activity, key=lambda row: str(row.get("ts") or "")):
        side = str(fill.get("side") or "").lower()
        if not (side.startswith("buy") or side.startswith("sell")):
            continue
        remaining = float(fill.get("qty") or 0.0)
        if remaining <= 0:
            continue
        incoming_sign = 1.0 if side.startswith("buy") else -1.0
        price = float(fill.get("price") or 0.0)
        multiplier = float(fill.get("multiplier", 100) or 100)
        symbol = str(fill.get("symbol") or "")
        closed_ts = fill.get("ts")
        lots = inventory.setdefault(symbol, [])

        while remaining > 1e-9 and lots and lots[0][0] * incoming_sign < 0:
            held_qty, held_price, held_ts = lots[0]
            matched = min(abs(held_qty), remaining)
            long_entry = held_qty > 0
            pnl = ((price - held_price) if long_entry else (held_price - price))
            matches.append({
                "symbol": symbol,
                "side": "long" if long_entry else "short",
                "qty": matched,
                "entry_price": held_price,
                "exit_price": price,
                "multiplier": multiplier,
                "pnl": pnl * matched * multiplier,
                "opened_ts": held_ts,
                "closed_ts": closed_ts,
            })
            updated = abs(held_qty) - matched
            remaining -= matched
            if updated <= 1e-9:
                lots.pop(0)
            else:
                lots[0][0] = (1.0 if long_entry else -1.0) * updated

        if remaining > 1e-9:
            lots.append([incoming_sign * remaining, price, closed_ts])
    return matches


def realized_pnl_from_fills(activity: list[dict]) -> float:
    """P&L from closed quantity only. See _matched_fills."""
    return round(sum(m["pnl"] for m in _matched_fills(activity)), 2)


def closed_round_trips(activity: list[dict]) -> list[dict]:
    """Completed round trips per symbol, newest close first.

    Quantity-weighted entry and exit so the row reads as one position rather
    than as the several fills it may have taken to open or close it. The sum of
    ``realized`` across these rows is ``realized_pnl_from_fills`` exactly.
    """
    agg: dict[str, dict] = {}
    for m in _matched_fills(activity):
        row = agg.setdefault(m["symbol"], {
            "symbol": m["symbol"], "side": m["side"], "qty": 0.0,
            "entry_notional": 0.0, "exit_notional": 0.0, "realized": 0.0,
            "opened_ts": m["opened_ts"], "closed_ts": m["closed_ts"],
        })
        row["qty"] += m["qty"]
        row["entry_notional"] += m["entry_price"] * m["qty"]
        row["exit_notional"] += m["exit_price"] * m["qty"]
        row["realized"] += m["pnl"]
        if str(m["opened_ts"] or "") < str(row["opened_ts"] or ""):
            row["opened_ts"] = m["opened_ts"]
        if str(m["closed_ts"] or "") > str(row["closed_ts"] or ""):
            row["closed_ts"] = m["closed_ts"]
    out = []
    for row in agg.values():
        qty = row["qty"] or 1.0
        out.append({
            "symbol": row["symbol"],
            "side": row["side"],
            "qty": round(row["qty"], 4),
            "avg_entry": round(row["entry_notional"] / qty, 4),
            "avg_exit": round(row["exit_notional"] / qty, 4),
            "realized": round(row["realized"], 2),
            "opened_ts": row["opened_ts"],
            "closed_ts": row["closed_ts"],
        })
    return sorted(out, key=lambda r: str(r["closed_ts"] or ""), reverse=True)


def account() -> dict:
    try:
        a = mcp_client.run(mcp_client.call("get_account_info")) or {}
        return {
            "account_number": a.get("account_number"),
            "equity": _f(a.get("equity")),
            "last_equity": _f(a.get("last_equity")),
            "cash": _f(a.get("cash")),
            "buying_power": _f(a.get("buying_power")),
            "options_buying_power": _f(a.get("options_buying_power")),
            "multiplier": _f(a.get("multiplier")),
            "options_approved_level": a.get("options_approved_level"),
            "options_level": a.get("options_trading_level"),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def spread_pnl_at_expiry(short_strike: float, long_strike: float,
                         credit: float, underlying_at_expiry: float,
                         is_put: bool = True) -> float:
    """Exact payoff of a defined-risk credit spread held to expiry, per contract.

    Put spread (short K_s, long K_l < K_s):
        S >= K_s          keep the whole credit
        K_l < S < K_s     credit minus the amount in the money
        S <= K_l          credit minus the full width (max loss)
    """
    width = abs(short_strike - long_strike)
    if is_put:
        intrinsic = max(short_strike - underlying_at_expiry, 0.0)
    else:
        intrinsic = max(underlying_at_expiry - short_strike, 0.0)
    return round((credit - min(intrinsic, width)) * 100, 2)


def summary(days: int = 30) -> dict:
    """Reconcile the agent's decisions against the account's actual results."""
    acct = account()
    positions = []
    activity = []
    error = acct.get("error")
    if not error:
        try:
            positions, activity = open_positions(), closed_activity(days)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    realized = realized_pnl_from_fills(activity)
    unrealized = round(sum(p["unrealized_pl"] for p in positions), 2)

    entries = ledger.load()
    executed = [e for e in entries if (e.get("execution") or {}).get("submitted")]
    approved = [e for e in entries if (e.get("verdict") or {}).get("approved")]
    option_mr_executed = [
        row for row in mean_reversion.load_log()
        if row.get("kind") == "decision"
        and row.get("status") == "SUBMITTED"
        and (row.get("execution") or {}).get("submitted")
    ]

    # What the gate PREDICTED across the trades it let through.
    predicted_ev = round(sum(
        (e.get("verdict") or {}).get("economics", {}).get("ev_net_usd", 0.0)
        for e in approved), 2)

    return {
        "error": error,
        "account_number": acct.get("account_number"),

        "equity": acct.get("equity"),
        "last_equity": acct.get("last_equity"),
        "account_daily_pnl": round(
            (acct.get("equity") or 0.0) - (acct.get("last_equity") or 0.0), 2
        ),
        "cash": acct.get("cash"),
        "buying_power": acct.get("buying_power"),
        "options_buying_power": acct.get("options_buying_power"),
        "multiplier": acct.get("multiplier"),
        "options_approved_level": acct.get("options_approved_level"),
        "options_level": acct.get("options_level"),
        "closed_round_trips": closed_round_trips(activity),
        # A spread closes both legs on one order, so distinct close timestamps
        # count closing orders rather than legs.
        "closing_orders": len({
            str(m["closed_ts"])[:19] for m in _matched_fills(activity) if m["closed_ts"]
        }),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": round(realized + unrealized, 2),
        "open_positions": len(positions),
        "positions": positions,
        "fills": len(activity),
        "orders_submitted": len(executed) + len(option_mr_executed),
        "gate_approved": len(approved),
        "predicted_ev_of_approved": predicted_ev,
    }
