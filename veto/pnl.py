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


def realized_pnl_from_fills(activity: list[dict]) -> float:
    """Match fills FIFO per OCC symbol and return P&L from closed quantity only.

    Summing fill cash is not realized P&L while an opening position remains: a
    short option's opening credit is paired with a liability.  Matching buys and
    sells removes that open inventory and keeps only completed round trips.
    """
    inventory: dict[str, list[list[float]]] = {}
    realized = 0.0
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
        lots = inventory.setdefault(str(fill.get("symbol") or ""), [])

        while remaining > 1e-9 and lots and lots[0][0] * incoming_sign < 0:
            held_qty, held_price = lots[0]
            matched = min(abs(held_qty), remaining)
            if held_qty > 0:  # sell closes an existing long
                realized += (price - held_price) * matched * multiplier
            else:             # buy closes an existing short
                realized += (held_price - price) * matched * multiplier
            updated = abs(held_qty) - matched
            remaining -= matched
            if updated <= 1e-9:
                lots.pop(0)
            else:
                lots[0][0] = (1.0 if held_qty > 0 else -1.0) * updated

        if remaining > 1e-9:
            lots.append([incoming_sign * remaining, price])
    return round(realized, 2)


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
