"""Broker-fill P&L reconciliation tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto.pnl import account_performance, realized_pnl_from_fills  # noqa: E402


def fill(symbol, side, qty, price, ts):
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "ts": ts,
    }


def test_opening_credit_is_not_realized_profit():
    activity = [fill("SPY260828P00757000", "sell", 69, 1.09, "2026-08-26T13:32:24Z")]
    assert realized_pnl_from_fills(activity) == 0.0


def test_fifo_matching_counts_only_closed_option_quantity():
    symbol = "SPY260828P00756000"
    activity = [
        fill(symbol, "sell", 5, 1.24, "2026-08-25T15:18:06Z"),
        fill(symbol, "buy", 4, 1.11, "2026-08-25T15:55:20Z"),
        fill(symbol, "buy", 1, 1.10, "2026-08-25T15:55:50Z"),
    ]
    assert realized_pnl_from_fills(activity) == 66.0


def test_two_leg_spread_matches_known_twenty_one_dollar_result():
    activity = [
        fill("SHORT", "sell", 5, 1.24, "2026-08-25T15:18:06Z"),
        fill("LONG", "buy", 5, 0.65, "2026-08-25T15:18:06Z"),
        fill("SHORT", "buy", 4, 1.11, "2026-08-25T15:55:20Z"),
        fill("LONG", "sell", 4, 0.56, "2026-08-25T15:55:20Z"),
        fill("SHORT", "buy", 1, 1.10, "2026-08-25T15:55:50Z"),
        fill("LONG", "sell", 1, 0.56, "2026-08-25T15:55:50Z"),
    ]
    assert realized_pnl_from_fills(activity) == 21.0


def test_account_performance_uses_fixed_starting_equity():
    total, rate = account_performance(105_851.37, 100_000.0)

    assert total == 5_851.37
    assert rate == 0.0585137
