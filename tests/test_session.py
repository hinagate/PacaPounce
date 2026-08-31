"""MCP-backed proposal-session controls."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto import config, executor, gates, session  # noqa: E402
import run as app  # noqa: E402


CALENDAR = [{"date": "2026-08-25", "open": "09:30", "close": "16:00"}]
ELIGIBLE_ACCOUNT = {
    "account_number": "TEST-ACCOUNT",
    "equity": "30561.78",
    "last_equity": "30561.78",
    "status": "ACTIVE",
    "trading_blocked": False,
    "account_blocked": False,
    "trade_suspended_by_user": False,
    "options_approved_level": 3,
    "options_trading_level": 3,
    "options_buying_power": "30561.78",
    "buying_power": "122247.12",
    "multiplier": "4",
}
ACCOUNT = {**ELIGIBLE_ACCOUNT, "equity": "30582.28", "last_equity": "30561.78"}


@pytest.fixture(autouse=True)
def configured_test_account(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ACCOUNT_ID", "TEST-ACCOUNT")


def order(client_id, status="filled", intent="sell_to_open", revision=None):
    cid = client_id if revision is None else f"{client_id}-r{revision}"
    return {
        "id": cid,
        "client_order_id": cid,
        "status": status,
        "filled_qty": "1" if status in {"filled", "partially_filled"} else "0",
        "order_class": "mleg",
        "legs": [{"position_intent": intent}],
    }


def snapshot(*, timestamp="2026-08-25T11:00:00-04:00", is_open=True,
             next_open=None, calendar=None, open_orders=None, orders=None,
             positions=None, account=None):
    clock = {"timestamp": timestamp, "is_open": is_open}
    if next_open:
        clock["next_open"] = next_open
    return session.build_snapshot(
        clock,
        CALENDAR if calendar is None else calendar,
        open_orders or [],
        orders or [],
        positions or [],
        account if account is not None else ELIGIBLE_ACCOUNT,
    )


def test_market_close_blocks_entry():
    snap = snapshot(
        timestamp="2026-08-25T16:00:00-04:00",
        is_open=False,
        next_open="2026-08-26T09:30:00-04:00",
    )
    decision = session.entry_decision(snap)
    assert snap.phase == "after_close"
    assert not decision.allowed
    assert decision.reason == "market_closed"
    assert snap.next_open.isoformat() == "2026-08-26T09:30:00-04:00"
    assert snap.public()["next_open"] == "2026-08-26T09:30:00-04:00"


def test_account_mismatch_fails_before_market_policy(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ACCOUNT_ID", "SUBMISSION-ACCOUNT")
    decision = session.entry_decision(snapshot(account={
        **ELIGIBLE_ACCOUNT, "account_number": "OTHER-ACCOUNT",
    }))
    assert not decision.allowed
    assert decision.reason == "account_mismatch"
    assert "SUBMISSION-ACCOUNT" in decision.detail


def test_missing_configured_account_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ACCOUNT_ID", "")
    decision = session.entry_decision(snapshot())
    assert not decision.allowed
    assert decision.reason == "account_id_unconfigured"


def test_closed_wait_uses_next_open_with_hourly_cap_and_preopen_ramp():
    overnight = snapshot(
        timestamp="2026-08-25T16:00:00-04:00",
        is_open=False,
        next_open="2026-08-26T09:30:00-04:00",
    )
    near_open = snapshot(
        timestamp="2026-08-25T09:27:00-04:00",
        is_open=False,
        next_open="2026-08-25T09:30:00-04:00",
    )
    assert app._closed_wait_seconds(overnight) == 3600
    assert app._closed_wait_seconds(near_open) == 60


def test_pre_open_is_waitable_but_not_allowed():
    decision = session.entry_decision(snapshot(
        timestamp="2026-08-25T09:15:00-04:00", is_open=False,
    ))
    assert not decision.allowed
    assert decision.reason == "market_pre_open"
    assert decision.waitable


def test_pending_opening_order_blocks_new_proposal():
    pending = order("veto-open-20260825-abcd1234", status="new", revision=0)
    decision = session.entry_decision(snapshot(open_orders=[pending]))
    assert not decision.allowed
    assert decision.reason == "opening_order_pending"
    assert decision.waitable


def test_filled_parent_orders_count_once_across_revisions_and_ignore_legs():
    base = "veto-open-20260825-abcd1234"
    orders = [
        order(base, status="partially_filled", revision=0),
        order(base, status="filled", revision=1),
        order("veto-close-deadbeef", intent="buy_to_close"),
        order("manual-order"),
    ]
    snap = snapshot(orders=orders)
    assert snap.trades_today == 1


def test_leg_fill_activity_reconciles_parent_when_order_status_lags():
    parent = order("veto-open-20260825-activity", status="canceled", revision=0)
    parent["legs"][0]["id"] = "leg-order-123"
    snap = session.build_snapshot(
        {"timestamp": "2026-08-25T11:00:00-04:00", "is_open": True},
        CALENDAR,
        [],
        [parent],
        [],
        {"equity": "30561.78", "last_equity": "30561.78"},
        [{"activity_type": "FILL", "order_id": "leg-order-123"}],
    )
    assert snap.trades_today == 1
    assert len(snap.fill_activities) == 1


def test_order_chase_keeps_logical_id_and_advances_revision():
    parent = order("veto-open-20260825-abcd1234", status="new", revision=3)
    replacement_id = session.next_revision_client_id(parent)
    assert replacement_id == "veto-open-20260825-abcd1234-r4"
    assert session.logical_trade_id({"client_order_id": replacement_id}) == (
        "veto-open-20260825-abcd1234"
    )


def test_new_entry_client_id_is_restart_reconcilable():
    client_id = executor.opening_client_order_id("decision123", revision=0)
    assert client_id.startswith("veto-open-")
    assert client_id.endswith("-decision123-r0")
    assert session.logical_trade_id({"client_order_id": client_id}) == client_id[:-3]


def test_legacy_veto_entry_is_restart_reconstructable():
    snap = snapshot(orders=[order("veto-e8dd70003edf4fcd")])
    assert snap.trades_today == 1


def test_legacy_chase_revision_stays_one_logical_trade():
    legacy = order("veto-e8dd70003edf4fcd", status="partially_filled")
    replacement_id = session.next_revision_client_id(legacy)
    replacement = order(replacement_id, status="filled")
    snap = snapshot(orders=[legacy, replacement])
    assert replacement_id == "veto-e8dd70003edf4fcd-r1"
    assert snap.trades_today == 1


def test_daily_target_comes_from_live_account_equity(monkeypatch):
    monkeypatch.setattr(config, "FULL_BUYING_POWER", False)
    snap = snapshot(account=ACCOUNT)
    decision = session.entry_decision(snap)
    assert snap.annual_target_reached
    assert round(snap.daily_pnl_usd, 2) == 20.50
    assert round(snap.daily_target_usd, 2) == 9.34
    assert decision.reason == "annual_target_reached"


def test_full_buying_power_mode_does_not_stop_at_annual_benchmark():
    snap = snapshot(account=ACCOUNT)
    decision = session.entry_decision(snap)
    assert snap.annual_target_reached
    assert decision.allowed


def test_full_buying_power_with_open_spread_waits_before_poe():
    positions = [
        {"symbol": "SPY260828P00757000", "qty": "-69", "side": "short"},
        {"symbol": "SPY260828P00752000", "qty": "69", "side": "long"},
    ]
    depleted = {**ELIGIBLE_ACCOUNT, "options_buying_power": "0"}
    snap = snapshot(positions=positions, account=depleted)
    decision = session.entry_decision(snap)

    assert not decision.allowed
    assert decision.reason == "full_capital_deployed"
    assert decision.waitable
    assert "$0.00 options BP remaining" in decision.detail


def test_non_option_position_fails_options_only_account_boundary():
    snap = snapshot(positions=[{
        "symbol": "AAPL", "asset_class": "us_equity", "qty": "50",
        "side": "long",
    }])

    decision = session.entry_decision(snap)

    assert snap.public()["non_option_positions"] == 1
    assert not decision.allowed
    assert decision.reason == "options_only_violation"
    assert decision.waitable


def test_single_leg_option_mr_entry_is_counted_from_parent_intent():
    option_mr = {
        "id": "mr-1",
        "client_order_id": "paca-callmr-open-20260825-aapl-123456",
        "status": "filled",
        "filled_qty": "1",
        "symbol": "AAPL260918C00095000",
        "side": "buy",
        "position_intent": "buy_to_open",
    }

    snap = snapshot(orders=[option_mr])

    assert snap.option_mr_entries_today == 1


def test_reentry_cooldown_blocks_before_another_poe_proposal():
    decision = session.entry_decision(snapshot(), {
        "active": True,
        "allowed": False,
        "reason": "reentry_cooldown",
        "detail": "profit exit cooling down; 20 minute(s) remaining",
    })
    assert not decision.allowed
    assert decision.reason == "reentry_cooldown"
    assert decision.waitable


def test_session_preflight_blocks_ineligible_options_account():
    snap = snapshot(account={**ELIGIBLE_ACCOUNT, "options_trading_level": 2})
    decision = session.entry_decision(snap)
    assert not decision.allowed
    assert decision.reason == "alpaca_options_ineligible"


def test_option_positions_become_gate_context():
    positions = [
        {"symbol": "SPY260828P00756000", "qty": "-1", "side": "short"},
        {"symbol": "SPY260828P00751000", "qty": "1", "side": "long"},
        {"symbol": "SPY", "qty": "10", "side": "long"},
    ]
    snap = snapshot(positions=positions)
    context = snap.gate_context()
    assert snap.open_spreads == 1
    assert context["open_positions"] == 1
    assert len(context["held_symbols"]) == 2
    assert context["equity"] == 30561.78
    assert context["options_trading_level"] == 3
    assert context["options_buying_power"] == 30561.78


def test_live_cycle_blocks_before_calling_ai_at_market_close(monkeypatch):
    closed = snapshot(timestamp="2026-08-25T16:00:00-04:00", is_open=False)

    def unexpected_ai_call(*_args, **_kwargs):
        raise AssertionError("Poe must not be called for a broker-blocked entry")

    monkeypatch.setattr(app.llm, "propose", unexpected_ai_call)
    result = app.cycle(False, execute=True, verbose=False, session_snapshot=closed)
    assert result["stop_reason"] == "market_closed"


def test_economic_rejection_gets_one_diversified_revision(monkeypatch):
    raw_intent = {
        "underlying": "SPY",
        "direction": "bullish",
        "strategy": "put_credit_spread",
        "dte_range": [2, 5],
        "short_delta_target": 0.20,
        "spread_width": 5,
        "max_loss_usd": 400,
        "thesis": "test",
        "invalidation": "test",
    }
    feedback_seen = []
    verdicts = iter([
        gates.Verdict(
            False,
            checks=[gates.Check("economic_ev", False, "negative EV")],
            reason="economic_ev: negative EV",
        ),
        gates.Verdict(True, reason="approved"),
    ])
    monkeypatch.setattr(config, "PROPOSAL_BUDGET", 2)
    monkeypatch.setattr(
        app.llm,
        "propose",
        lambda _brief, feedback=None: (
            feedback_seen.append(feedback) or raw_intent,
            "{}",
        ),
    )
    monkeypatch.setattr(app.gates, "evaluate", lambda *_args: next(verdicts))
    monkeypatch.setattr(
        app.ledger,
        "record",
        lambda *_args: {"verdict": {"approved": False}},
    )

    app.cycle(True, execute=False, verbose=False)

    assert len(feedback_seen) == 2
    assert feedback_seen[0] is None
    assert "only revision" in feedback_seen[1]
    assert "change the DTE range" in feedback_seen[1]


def test_broker_gate_failure_does_not_spend_revision(monkeypatch):
    raw_intent = {
        "underlying": "SPY",
        "direction": "bullish",
        "strategy": "put_credit_spread",
        "dte_range": [2, 5],
        "short_delta_target": 0.20,
        "spread_width": 5,
        "max_loss_usd": 400,
        "thesis": "test",
        "invalidation": "test",
    }
    calls = []
    monkeypatch.setattr(config, "PROPOSAL_BUDGET", 2)
    monkeypatch.setattr(
        app.llm,
        "propose",
        lambda *_args: (calls.append(True) or raw_intent, "{}"),
    )
    monkeypatch.setattr(
        app.gates,
        "evaluate",
        lambda *_args: gates.Verdict(
            False,
            checks=[gates.Check("alpaca_options_eligible", False, "blocked")],
            reason="alpaca_options_eligible: blocked",
        ),
    )
    monkeypatch.setattr(
        app.ledger,
        "record",
        lambda *_args: {"verdict": {"approved": False}},
    )

    app.cycle(True, execute=False, verbose=False)

    assert len(calls) == 1


def test_submit_refresh_blocks_order_that_appeared_during_proposal(monkeypatch):
    initially_allowed = snapshot()
    pending_order = order("veto-open-20260825-race", status="new", revision=0)
    now_blocked = snapshot(open_orders=[pending_order])
    raw_intent = {
        "underlying": "SPY",
        "direction": "bullish",
        "strategy": "put_credit_spread",
        "dte_range": [2, 5],
        "short_delta_target": 0.20,
        "spread_width": 5,
        "max_loss_usd": 400,
        "thesis": "test",
        "invalidation": "test",
    }

    monkeypatch.setattr(app, "market_brief", lambda: "test brief")
    monkeypatch.setattr(app.llm, "propose", lambda *_args: (raw_intent, "{}"))
    monkeypatch.setattr(app, "spot_for", lambda *_args: 640.0)
    monkeypatch.setattr(app.builder, "build", lambda *_args: ({"spread": True}, ""))
    monkeypatch.setattr(
        app.gates, "evaluate", lambda *_args: gates.Verdict(True, reason="approved")
    )
    monkeypatch.setattr(app.session, "capture", lambda: now_blocked)
    monkeypatch.setattr(
        app.executor,
        "submit",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not submit")),
    )
    monkeypatch.setattr(
        app.ledger,
        "record",
        lambda _raw, _err, _spread, _verdict, execution, *_args: {
            "verdict": {"approved": True}, "execution": execution,
        },
    )

    result = app.cycle(
        False, execute=True, verbose=False, session_snapshot=initially_allowed,
    )
    assert result["execution"]["submitted"] is False
    assert result["execution"]["error"] == "opening_order_pending"


def test_submit_refresh_reruns_gate_against_latest_options_buying_power(monkeypatch):
    initial = snapshot()
    reduced_bp = snapshot(account={**ELIGIBLE_ACCOUNT, "options_buying_power": "1.00"})
    raw_intent = {
        "underlying": "SPY", "direction": "bullish",
        "strategy": "put_credit_spread", "dte_range": [2, 5],
        "short_delta_target": 0.20, "spread_width": 5,
        "max_loss_usd": 400, "thesis": "test", "invalidation": "test",
    }
    verdicts = iter([
        gates.Verdict(True, reason="approved"),
        gates.Verdict(
            False,
            checks=[gates.Check("total_risk_cap", False, "buying power changed")],
            reason="total_risk_cap: buying power changed",
        ),
    ])

    monkeypatch.setattr(app, "market_brief", lambda: "test brief")
    monkeypatch.setattr(app.llm, "propose", lambda *_args: (raw_intent, "{}"))
    monkeypatch.setattr(app, "spot_for", lambda *_args: 640.0)
    monkeypatch.setattr(app.builder, "build", lambda *_args: ({"spread": True}, ""))
    monkeypatch.setattr(app.gates, "evaluate", lambda *_args: next(verdicts))
    monkeypatch.setattr(app.session, "capture", lambda: reduced_bp)
    monkeypatch.setattr(
        app.executor,
        "submit",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not submit")),
    )
    monkeypatch.setattr(
        app.ledger,
        "record",
        lambda _raw, _err, _spread, _verdict, execution, *_args: {
            "verdict": {"approved": True}, "execution": execution,
        },
    )

    result = app.cycle(False, execute=True, verbose=False, session_snapshot=initial)
    assert result["execution"]["submitted"] is False
    assert result["execution"]["error"] == "final_gate_failed"
    assert result["execution"]["failures"] == ["total_risk_cap"]


def test_one_command_starts_monitor_with_paper_auto_exit(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 4242
        returncode = None

        @staticmethod
        def poll():
            return None

    def fake_popen(command, cwd):
        captured.update(command=command, cwd=cwd)
        return FakeProcess()

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    process = app._start_monitor()

    assert process.pid == 4242
    assert captured["command"][0] == sys.executable
    assert Path(captured["command"][1]).parts[-2:] == ("scripts", "monitor.py")
    assert "--execute" in captured["command"]
    assert "--interval" in captured["command"]


def test_one_command_starts_dashboard_watcher(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 4343
        returncode = None

        @staticmethod
        def poll():
            return None

    def fake_popen(command, cwd):
        captured.update(command=command, cwd=cwd)
        return FakeProcess()

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    process = app._start_dashboard()

    assert process.pid == 4343
    assert captured["command"][0] == sys.executable
    assert Path(captured["command"][1]).parts[-2:] == ("scripts", "build_dashboard.py")
    assert "--watch" in captured["command"]
    assert "--interval" in captured["command"]


def test_entry_target_lock_keeps_bundled_monitor_until_market_close(monkeypatch):
    monkeypatch.setattr(config, "FULL_BUYING_POWER", False)
    target_reached = snapshot(account=ACCOUNT)
    after_close = snapshot(
        timestamp="2026-08-25T16:00:00-04:00", is_open=False, account=ACCOUNT,
        next_open="2026-08-26T09:30:00-04:00",
    )
    next_session = snapshot(
        timestamp="2026-08-26T11:00:00-04:00",
        calendar=[{"date": "2026-08-26", "open": "09:30", "close": "16:00"}],
        account=ACCOUNT,
    )
    snapshots = iter([target_reached, after_close, next_session])
    monitor_token, dashboard_token = object(), object()
    events = []

    def capture():
        try:
            return next(snapshots)
        except StopIteration:
            raise KeyboardInterrupt

    monkeypatch.setattr(app.session, "capture", capture)
    monkeypatch.setattr(
        app, "_ensure_monitor", lambda process: events.append("monitor") or monitor_token,
    )
    monkeypatch.setattr(app, "_stop_monitor", lambda process: events.append(("stop", process)))
    monkeypatch.setattr(
        app, "_ensure_dashboard",
        lambda process: events.append("dashboard") or dashboard_token,
    )
    monkeypatch.setattr(
        app, "_stop_dashboard", lambda process: events.append(("stop-dashboard", process)),
    )
    monkeypatch.setattr(
        app,
        "cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("entry must stay locked after reaching target")
        ),
    )
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

    assert app.autonomous_loop(False) == 130
    assert events == [
        "dashboard", "monitor",
        ("stop", monitor_token), ("stop-dashboard", dashboard_token),
        "dashboard", "monitor",
        ("stop", monitor_token), ("stop-dashboard", dashboard_token),
    ]


def test_full_capital_lock_keeps_monitor_and_skips_entry_cycle(monkeypatch):
    positions = [
        {"symbol": "SPY260828P00757000", "qty": "-69", "side": "short"},
        {"symbol": "SPY260828P00752000", "qty": "69", "side": "long"},
    ]
    deployed = snapshot(
        positions=positions,
        account={**ELIGIBLE_ACCOUNT, "options_buying_power": "0"},
    )
    after_close = snapshot(
        timestamp="2026-08-25T16:00:00-04:00",
        is_open=False,
        positions=positions,
        account={**ELIGIBLE_ACCOUNT, "options_buying_power": "0"},
        next_open="2026-08-26T09:30:00-04:00",
    )
    next_session = snapshot(
        timestamp="2026-08-26T11:00:00-04:00",
        calendar=[{"date": "2026-08-26", "open": "09:30", "close": "16:00"}],
        positions=positions,
        account={**ELIGIBLE_ACCOUNT, "options_buying_power": "0"},
    )
    snapshots = iter([deployed, after_close, next_session])
    monitor_token, dashboard_token = object(), object()
    events = []

    def capture():
        try:
            return next(snapshots)
        except StopIteration:
            raise KeyboardInterrupt

    monkeypatch.setattr(app.session, "capture", capture)
    monkeypatch.setattr(
        app, "_ensure_monitor", lambda process: events.append("monitor") or monitor_token,
    )
    monkeypatch.setattr(app, "_stop_monitor", lambda process: events.append(("stop", process)))
    monkeypatch.setattr(
        app, "_ensure_dashboard",
        lambda process: events.append("dashboard") or dashboard_token,
    )
    monkeypatch.setattr(
        app, "_stop_dashboard", lambda process: events.append(("stop-dashboard", process)),
    )
    monkeypatch.setattr(
        app,
        "cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Poe/entry cycle must stay idle while full capital is deployed")
        ),
    )
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

    assert app.autonomous_loop(False) == 130
    assert events == [
        "dashboard", "monitor",
        ("stop", monitor_token), ("stop-dashboard", dashboard_token),
        "dashboard", "monitor",
        ("stop", monitor_token), ("stop-dashboard", dashboard_token),
    ]


def test_order_submission_fails_closed_if_bundled_monitor_dies(monkeypatch):
    raw_intent = {
        "underlying": "SPY", "direction": "bullish",
        "strategy": "put_credit_spread", "dte_range": [2, 5],
        "short_delta_target": 0.20, "spread_width": 5,
        "max_loss_usd": 400, "thesis": "test", "invalidation": "test",
    }
    monkeypatch.setattr(app, "market_brief", lambda: "test brief")
    monkeypatch.setattr(app.llm, "propose", lambda *_args: (raw_intent, "{}"))
    monkeypatch.setattr(app, "spot_for", lambda *_args: 640.0)
    monkeypatch.setattr(app.builder, "build", lambda *_args: ({"spread": True}, ""))
    monkeypatch.setattr(
        app.gates, "evaluate", lambda *_args: gates.Verdict(True, reason="approved")
    )
    monkeypatch.setattr(
        app.executor,
        "submit",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not submit")),
    )
    monkeypatch.setattr(
        app.session,
        "capture",
        lambda: (_ for _ in ()).throw(AssertionError("guard must run first")),
    )
    monkeypatch.setattr(
        app.ledger,
        "record",
        lambda _raw, _err, _spread, _verdict, execution, *_args: {
            "verdict": {"approved": True}, "execution": execution,
        },
    )

    result = app.cycle(
        False,
        execute=True,
        verbose=False,
        session_snapshot=snapshot(),
        execution_guard=lambda: (False, "monitor exited"),
    )
    assert result["execution"]["error"] == "risk_monitor_unavailable"
    assert result["execution"]["detail"] == "monitor exited"
