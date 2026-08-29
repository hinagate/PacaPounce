"""Order execution via the Alpaca MCP server. Paper account only."""
from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import mcp_client
from .gates import Verdict

ET = ZoneInfo("America/New_York")


def opening_client_order_id(token: str | None = None, revision: int = 0) -> str:
    """Encode the logical entry and revision for restart-safe reconciliation."""
    day = datetime.now(ET).strftime("%Y%m%d")
    decision = (token or uuid.uuid4().hex[:12]).lower()
    return f"veto-open-{day}-{decision}-r{revision}"


def submit(spread: dict, verdict: Verdict) -> dict:
    """Submit an approved spread as an atomic multi-leg limit order.

    Refuses anything the gate did not approve - execution is not a place where
    a judgement call gets made.
    """
    if not verdict.approved:
        return {"submitted": False, "error": "gate did not approve this spread"}

    credit = spread["credit"]
    legs = [
        {"symbol": spread["short_symbol"], "side": "sell",
         "ratio_qty": "1", "position_intent": "sell_to_open"},
        {"symbol": spread["long_symbol"], "side": "buy",
         "ratio_qty": "1", "position_intent": "buy_to_open"},
    ]
    client_order_id = opening_client_order_id()
    result = mcp_client.run(mcp_client.call(
        "place_option_order",
        qty=str(spread["qty"]),
        type="limit",
        time_in_force="day",
        order_class="mleg",
        # Negative = credit. Positive here would let Alpaca fill at ANY credit.
        limit_price=f"{-abs(credit):.2f}",
        legs=legs,
        client_order_id=client_order_id,
    ))
    return {
        "submitted": True,
        "client_order_id": client_order_id,
        "response": result,
    }


def open_option_positions() -> list[dict]:
    """API is the source of truth - never trust the local ledger for this."""
    res = mcp_client.run(mcp_client.call("get_all_positions"))
    rows = res if isinstance(res, list) else (res or {}).get("positions", [])
    return [p for p in rows if len(str(p.get("symbol", ""))) > 10]
