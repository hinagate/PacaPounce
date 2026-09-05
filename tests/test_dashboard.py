"""Dashboard evidence should explain the AI/policy boundary without narration."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import build_dashboard  # noqa: E402
from veto import gates, mcp_client  # noqa: E402


def test_default_runtime_dashboard_is_separate_from_public_day_zero():
    assert build_dashboard.OUT == (
        build_dashboard.ROOT / "dashboard" / "runtime" / "index.html"
    )
    assert build_dashboard.PUBLIC_OUT == (
        build_dashboard.ROOT / "dashboard" / "index.html"
    )


def test_account_mismatch_suppresses_all_financial_fields(monkeypatch):
    monkeypatch.setattr(build_dashboard.config, "ALPACA_ACCOUNT_ID", "EXPECTED")
    raw = {
        "account_number": "OTHER",
        "equity": 123456.0,
        "account_daily_pnl": 987.0,
        "orders_submitted": 12,
    }

    safe, live_account, configured, matched, error = (
        build_dashboard.enforce_account_boundary(raw, live=True)
    )

    assert live_account == "OTHER"
    assert configured == "EXPECTED"
    assert not matched
    assert "does not match" in error
    assert safe == {"error": error, "account_number": "OTHER"}


def test_exit_evidence_reconciles_reason_fill_and_flat_account(monkeypatch):
    monkeypatch.setattr(build_dashboard.risk_state, "load", lambda: {
        "last_exit": {
            "action": "profit_ratchet",
            "close_client_order_id": "veto-close-test",
            "submitted_at": "2026-08-26T13:17:56-04:00",
            "short_symbol": "SPY260828P00757000",
            "long_symbol": "SPY260828P00752000",
            "qty": 69,
            "max_profit": 3933.0,
            "exit_executable_pnl": 138.0,
        },
        "positions": {
            "SPY260828P00757000|SPY260828P00752000": {
                "high_water_pnl": 897.0,
                "trailing_floor": 717.6,
                "breach_count": 2,
            },
        },
    })
    monkeypatch.setattr(mcp_client, "call_many", lambda calls: calls)
    monkeypatch.setattr(mcp_client, "run", lambda _calls: [[{
        "client_order_id": "veto-close-test",
        "status": "filled",
        "filled_qty": "69",
        "filled_avg_price": "0.55",
        "filled_at": "2026-08-26T17:18:00Z",
    }], []])

    evidence = build_dashboard.load_exit_evidence(True, {"open_positions": 0})

    assert evidence["broker_confirmed_flat"] is True
    assert evidence["broker_status"] == "filled"
    assert evidence["high_water_pnl"] == 897.0
    assert evidence["realized_gross_pnl"] == 138.0


def test_dashboard_contains_ai_intent_and_ordered_gate_catalog(tmp_path, monkeypatch):
    checks = [
        {"name": gate["name"], "passed": True, "detail": "test evidence"}
        for gate in gates.GATE_CATALOG
    ]
    entry = {
        "ts": "2026-08-25T15:17:35+00:00",
        "gate_version": "1.1.0",
        "attempt": 1,
        "intent": {
            "underlying": "SPY",
            "direction": "bullish",
            "strategy": "put_credit_spread",
            "dte_range": [2, 5],
            "short_delta_target": 0.20,
            "spread_width": 5,
            "max_loss_usd": 400,
            "thesis": "AI thesis evidence",
            "invalidation": "AI invalidation evidence",
        },
        "intent_error": None,
        "raw_reply_chars": 439,
        "spread": {
            "underlying": "SPY", "strategy": "put_credit_spread",
            "expiry": "2026-08-28", "qty": 1,
            "short_strike": 756.0, "long_strike": 751.0,
            "credit": 0.59,
        },
        "verdict": {
            "approved": True,
            "reason": "approved",
            "checks": checks,
            "economics": {},
        },
        "execution": {"submitted": True},
    }
    monkeypatch.setattr(build_dashboard.ledger, "load", lambda: [entry])
    monkeypatch.setattr(build_dashboard.ledger, "summary", lambda: {
        "proposals": 1, "scored": 1, "approved": 1, "vetoed": 0,
        "pass_rate": 1.0, "executed": 1, "veto_reasons": {},
        "gate_version": "1.1.0",
    })
    monkeypatch.setattr(build_dashboard, "load_tools", lambda: {})
    monkeypatch.setattr(build_dashboard, "load_validation", lambda: {})
    monkeypatch.setattr(
        build_dashboard,
        "load_exit_evidence",
        lambda _live, _pl: {
            "action": "profit_ratchet",
            "qty": 69,
            "trigger_pnl": 138.0,
            "high_water_pnl": 897.0,
            "trailing_floor_pnl": 717.60,
            "breach_count": 2,
            "high_volatility": False,
            "broker_confirmed_flat": True,
            "open_account_positions": 0,
            "broker_status": "filled",
            "filled_qty": "69",
            "close_debit": "0.55",
            "filled_at": "2026-08-26T17:18:00Z",
            "realized_gross_pnl": 138.0,
            "broker_error": None,
        },
    )
    monkeypatch.setattr(build_dashboard, "OUT", tmp_path / "index.html")

    html = build_dashboard.build(live=False).read_text(encoding="utf-8")

    assert "PacaPounce" in html
    assert "Team a-meowmeow" in html
    assert "Live account overview" in html
    assert "Reload latest broker snapshot" in html
    assert "Open positions" in html
    assert "Decision log — what the agent did and why" in html
    assert "SUBMITTED" in html
    assert "16/16" in html
    # The deep-dive is no longer behind a click; assert the content itself.
    assert "16 gates, one by one" in html
    assert "How AI intelligence becomes a paper trade" in html
    assert "Proposes freely, trades only what survives" in html
    assert "Signal, not retry noise" in html
    # The ledger link points at wherever the ledger actually is: repo-relative
    # in production, absolute when a test redirects it out of the repository.
    assert build_dashboard.config.VERDICT_LOG.name in html
    assert 'class="funnel"' in html
    assert "forecast hold up?" in html
    assert "AI thesis evidence" in html
    assert "AI invalidation evidence" in html
    assert f"Current entry stack: {len(gates.GATE_CATALOG)} gates" in html
    assert "Objective budget" in html
    assert "Alpaca options eligibility" in html
    assert "Tournament risk disclosure" in html
    assert "Options buying power" in html
    assert "Total P&amp;L (vs $100,000 start)" in html
    assert "Total account P&amp;L" in html
    assert "Economic EV" in html
    assert "MCP session supervisor" in html
    assert "Account identity" in html
    assert "Pending-order lock" in html
    assert "get_account_activities(FILL)" in html
    # Those two explainer notes were cut for the submission; what they were
    # evidence of - a bounded proposal budget and a supervised session - is
    # still asserted through the gate catalog and the supervisor block above.
    assert "next_open" in html
    assert "nights, holidays, and weekends" in html
    assert "Profit high-water" in html
    assert "Better re-entry" in html
    assert "Re-entry improvement" in html
    assert "Second options strategy" in html
    assert "NDX30_CALL_MR_01" in html
    # The 2024 signal study is no longer shown: it measured a different
    # configuration on stock, and this lane is judged on the live account.
    assert "PF 1.394" not in html
    assert "No historical option returns stand behind" in html
    assert "Second-strategy MCP lane" in html
    assert "place_option_order" in html
    assert "No stock order exists" in html
    assert "14-30 DTE call" in html
    assert "Evidence boundary" in html
    assert (
        f'http-equiv="refresh" content="{build_dashboard.config.DASHBOARD_REFRESH_INTERVAL_SEC}"'
        in html
    )
    assert "Latest position lifecycle" in html
    assert "Profit high-water ratchet" in html
    assert "FLAT" in html
    assert "69/69 spread contracts" in html
    assert "Gross locked P&amp;L" in html
    assert "$138.00" in html
    assert "Snapshot generated" in html
    assert len(gates.GATE_CATALOG) == 16


def test_total_account_pnl_formats_dollars_and_return():
    assert (
        build_dashboard.account_total_pnl_text(5_851.37, 0.0585137)
        == "$5,851.37 (+5.85%)"
    )


def test_presentation_collapses_retries_and_keeps_approved_results():
    entries = []
    for window in range(15):
        for attempt in range(1, 6):
            approved = window == 0 and attempt == 2
            entries.append({
                "attempt": attempt,
                "intent": {"thesis": f"window-{window}-attempt-{attempt}"},
                "verdict": {
                    "approved": approved,
                    "checks": [{
                        "name": "economic_ev",
                        "passed": approved,
                        "detail": "test",
                    }],
                },
                "execution": {"submitted": approved},
            })

    selected, stats = build_dashboard.presentation_entries(entries, recent_windows=10)
    selected_numbers = [number for number, _ in selected]

    assert stats == {
        "raw_attempts": 75,
        "decision_windows": 15,
        "approved_windows": 1,
        "submitted_windows": 1,
        "blocked_windows": 14,
        "collapsed_retries": 60,
        "shown_rows": 11,
        "suppressed_rows": 64,
        "recent_window_limit": 10,
        "veto_reasons": {"economic_ev": 14},
    }
    assert 2 in selected_numbers
    assert 25 not in selected_numbers
    assert selected_numbers[:2] == [75, 70]

    approved_only, zero_stats = build_dashboard.presentation_entries(
        entries, recent_windows=0,
    )
    assert [number for number, _ in approved_only] == [2]
    assert zero_stats["shown_rows"] == 1


@pytest.fixture
def mr_close_evidence():
    symbol = "AMD260925C00415000"
    entry = {
        "kind": "decision", "status": "SUBMITTED",
        "ts": "2026-09-02T10:01:05-04:00",
        "candidate": {"symbol": "AMD", "rsi2": 11.06},
        "contract": {"symbol": symbol, "dte": 23},
        "ai_review": {"thesis": "Oversold AMD entry thesis"},
        "checks": [{"name": "carry_within_signal_edge", "passed": True}],
    }
    close = {
        "kind": "position_closed", "ts": "2026-09-04T14:28:00Z",
        "contract_symbol": symbol, "underlying": "AMD", "reason": "profit_ratchet",
        "exit_client_order_id": "amd-close", "qty": 4,
        # These submitted/local values must not masquerade as actual fills.
        "exit_limit": 55.6, "exit_trigger_pnl": 3500.0,
        "ratchet_high_water_pnl": 5400.0, "ratchet_floor_pnl": 4050.0,
        "ratchet_breaches": 2,
    }
    scans = [{
        "kind": "decision", "ts": f"2026-09-04T{hour}:45:00-04:00",
        "status": "NO_SIGNAL", "reason": "no_qualified_mean_reversion",
    } for hour in (12, 13, 14, 15)]
    state = {"positions": {symbol: {
        "status": "closed", "underlying": "AMD",
        "opened_at": "2026-09-02T10:01:29-04:00",
        "exit_reason": "profit_ratchet",
    }}}
    pl = {
        "error": None, "positions": [], "open_positions": 0,
        "closed_round_trips": [{
            "symbol": symbol, "side": "long", "qty": 4,
            "avg_entry": 46.85, "avg_exit": 55.7, "realized": 3540.0,
            "opened_ts": "2026-09-02T14:00:59Z",
            "closed_ts": "2026-09-04T14:27:34Z",
        }],
    }
    # Duplicate lifecycle notifications must not duplicate broker P&L.
    return [entry, close, dict(close), *scans], state, pl


def test_mr_presentation_keeps_old_entry_and_broker_close(mr_close_evidence):
    log, state, pl = mr_close_evidence
    rows = build_dashboard.mean_reversion_evidence(log, state, pl)

    assert len(rows) == 5  # All submissions/closes plus only three recent scans.
    assert [row["status"] for row in rows] == [
        "NO_SIGNAL", "NO_SIGNAL", "NO_SIGNAL", "CLOSED", "SUBMITTED",
    ]
    close = rows[3]
    assert close["ts"] == "2026-09-04T14:27:34Z"
    assert "AMD260925C00415000" in close["label"]
    for expected in ("$46.85", "$55.70", "$3,540.00", "$5,400.00", "$4,050.00"):
        assert expected in close["rationale"]
    assert "$55.60" not in close["rationale"]
    assert "$3,500.00" not in close["rationale"]
    assert "2 confirmed below-floor observations" in close["rationale"]
    assert "Oversold AMD entry thesis" in rows[-1]["rationale"]


@pytest.mark.parametrize("unverified", ["missing_fills", "broker_error"])
def test_mr_close_needs_broker_fill_evidence(mr_close_evidence, unverified):
    log, state, pl = mr_close_evidence
    if unverified == "missing_fills":
        pl["closed_round_trips"] = []
    else:
        pl["error"] = "account identity mismatch"
    rows = build_dashboard.mean_reversion_evidence(log, state, pl)
    assert all(row["status"] not in {"CLOSED", "CLOSE FILLED"} for row in rows)
    assert all("$3,540.00" not in row["rationale"] for row in rows)


def test_mr_partial_close_does_not_claim_flat(mr_close_evidence):
    log, state, pl = mr_close_evidence
    pl["positions"] = [{"symbol": "AMD260925C00415000", "qty": 1}]
    rows = build_dashboard.mean_reversion_evidence(log, state, pl)
    assert any(row["status"] == "PARTIAL CLOSE" for row in rows)
    assert not any(row["status"] == "CLOSED" for row in rows)


def test_decision_evidence_escapes_policy_text():
    rendered = build_dashboard.decision_evidence_row({
        "ts": "2026-09-04T14:27:34Z", "status": "CLOSED",
        "label": 'AMD <script>alert("label")</script>',
        "rationale": '<script>alert("reason")</script>',
        "evidence": "Broker <fills>",
    })
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "Sep 04 10:27:34" in rendered


def test_both_dashboard_sections_include_amd_close(tmp_path, monkeypatch, mr_close_evidence):
    log, state, pl = mr_close_evidence
    monkeypatch.setattr(build_dashboard.config, "ALPACA_ACCOUNT_ID", "EXPECTED")
    pl["account_number"] = "EXPECTED"
    monkeypatch.setattr(build_dashboard, "load_pnl", lambda _live: pl)
    monkeypatch.setattr(build_dashboard, "load_exit_evidence", lambda *_args: None)
    monkeypatch.setattr(build_dashboard, "load_tools", lambda: {})
    monkeypatch.setattr(build_dashboard, "load_validation", lambda: {})
    monkeypatch.setattr(build_dashboard.ledger, "load", lambda: [])
    monkeypatch.setattr(build_dashboard.mean_reversion, "load_log", lambda: log)
    monkeypatch.setattr(build_dashboard.mean_reversion, "load_state", lambda: state)
    html = build_dashboard.build(
        live=True, output=tmp_path / "index.html", published=True,
    ).read_text(encoding="utf-8")

    overview = html.split('id="decision-log"', 1)[1].split("Second options strategy", 1)[0]
    execution = html.split('id="presentation-execution"', 1)[1].split("</section>", 1)[0]
    for section in (overview, execution):
        assert "AMD260925C00415000" in section
        assert "CLOSED" in section
        assert "SUBMITTED" in section
        assert "Profit high-water ratchet" in section
        assert "$55.70" in section
        assert "$3,540.00" in section
        assert section.index("Sep 04 15:45:00") < section.index("Sep 04 10:27:34")
    assert "http-equiv=\"refresh\"" not in html
