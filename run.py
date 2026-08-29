#!/usr/bin/env python
"""PacaPounce - the patient AI trading agent.

  LLM (Poe / gemini-3.7-flash)  ->  intent JSON
  Deterministic builder          ->  real contracts from the Alpaca MCP chain
  Gate stack                     ->  operational firewall + economic check
  Executor                       ->  atomic multi-leg limit order, paper only
  NDX30 MR fallback              ->  one tested 15:45 stock decision, paper only

Usage:
  python run.py --check                 environment + connectivity diagnostics
  python run.py --propose               one cycle, gate it, do NOT execute
  python run.py --trade                 one cycle, gate it, execute if approved
  python run.py --measure 25            N proposals, no execution (the statistic)
  python run.py --loop                  autonomous entries + risk monitor
  python run.py --summary               ledger statistics
  python run.py --offline               use a fixed brief + synthetic chain
                                        (LLM + gates testable with no Alpaca keys)
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from veto import (
    builder, config, executor, gates, ledger, llm, mcp_client, regime,
    mean_reversion, risk_state, session,
)
from veto import intent as intent_mod

# Windows consoles default to cp1252 and choke on anything outside it.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


OFFLINE_BRIEF = """Date: 2026-08-25 (OFFLINE TEST BRIEF - synthetic data)
SPY: spot 640.00 | 1D +0.20% | 5D +0.80% | RV20 12.40% | ATM IV(3D) 14.00% | IV/RV 1.13
QQQ: spot 578.00 | 1D +0.35% | 5D +1.20% | RV20 16.10% | ATM IV(3D) 17.20% | IV/RV 1.07
Use only observed fields above; missing fields are unavailable, not zero.
Open positions: 0. Trades today: 0."""
ROOT = Path(__file__).resolve().parent

# These failures describe broker/session state or deterministic invariants. A
# different story from Poe cannot repair them inside the same decision window.
NON_RETRYABLE_GATES = frozenset({
    "defined_risk",
    "allowlist",
    "alpaca_options_eligible",
    "open_positions",
    "daily_trade_limit",
    "annual_target_budget",
    "limit_order_only",
})


def retry_feedback(verdict: gates.Verdict) -> str | None:
    """Return a reason for one useful revision, or stop gate-mining.

    Economic rejection gets one deliberately diversified exploration. Broker
    state and deterministic-invariant failures wait for the next supervisor
    refresh instead of spending another model call.
    """
    failed = {check.name for check in verdict.failures}
    if failed & NON_RETRYABLE_GATES:
        return None
    feedback = verdict.reason
    if "economic_ev" in failed:
        feedback += (
            "\nThis is your only revision in this decision window. Materially "
            "diversify the intent: change the DTE range first; otherwise change "
            "the underlying or strategy/direction. Do not merely rewrite the "
            "thesis or make a small delta/width adjustment."
        )
    return feedback


def market_brief() -> str:
    """Live, compact regime brief through MCP; never an order instruction."""
    try:
        calls = [
            ("get_stock_latest_quote", {"symbols": ",".join(config.ALLOWLIST), "feed": "sip"}),
            ("get_account_info", {}),
            ("get_clock", {}),
        ]
        quotes, account, clock = mcp_client.run(mcp_client.call_many(calls))
        broker_date_text = str(
            (clock or {}).get("timestamp") or date.today().isoformat()
        )[:10]
        try:
            broker_date = date.fromisoformat(broker_date_text)
        except ValueError:
            broker_date = date.today()
        lines = [f"Date: {broker_date}",
                 f"Market open: {(clock or {}).get('is_open')}"]
        qs = (quotes or {}).get("quotes") or (quotes if isinstance(quotes, dict) else {})
        spots: dict[str, float] = {}
        for sym in config.ALLOWLIST:
            row = (qs or {}).get(sym) or {}
            bid, ask = row.get("bp"), row.get("ap")
            if bid and ask:
                spots[sym] = (float(bid) + float(ask)) / 2
        features = regime.snapshot(spots, broker_date)
        for sym in config.ALLOWLIST:
            lines.append(regime.format_feature(
                sym, features.get(sym) or {"spot": spots.get(sym, 0.0)}
            ))
        equity = (account or {}).get("equity")
        if equity:
            lines.append(f"Account equity: ${float(equity):,.0f}")
        lines.append(
            "Regime source: Alpaca MCP. Use only observed fields above; "
            "missing fields are unavailable, not zero."
        )
        return "\n".join(lines)
    except Exception as e:
        return OFFLINE_BRIEF + f"\n(live brief unavailable: {type(e).__name__})"


def spot_for(symbol: str, offline: bool) -> float:
    if offline:
        return {"SPY": 640.00, "QQQ": 578.00}.get(symbol, 500.0)
    q = mcp_client.run(mcp_client.call("get_stock_latest_quote", symbols=symbol, feed="sip"))
    row = ((q or {}).get("quotes") or {}).get(symbol) or {}
    bid, ask = float(row.get("bp", 0) or 0), float(row.get("ap", 0) or 0)
    return (bid + ask) / 2 if bid and ask else (bid or ask)


def synthetic_spread(it, rng: random.Random | None = None) -> dict:
    """Offline stand-in for the chain so the gate path is testable with no keys.

    Priced around FAIR value, not below it. Fair credit for this EV model is
    width * (short_delta + long_delta) / 2 -- the credit at which expected value
    is exactly zero. Real chains trade both sides of fair, so we scatter around
    it. Pricing systematically under fair would make the gate veto everything
    and prove nothing. Nothing here ever reaches a broker.
    """
    rng = rng or random
    spot = spot_for(it.underlying, offline=True)
    width = it.spread_width
    ds = it.short_delta_target
    dl = max(ds - 0.06 * (width / 5), 0.01)
    fair = width * (ds + dl) / 2
    credit = round(max(fair * rng.uniform(0.85, 1.25), 0.05), 2)
    short_k = round(spot * (1 - ds / 3), 0)
    return {
        "underlying": it.underlying, "strategy": it.strategy, "expiry": "OFFLINE",
        "qty": 1, "legs_short": 1, "legs_long": 1, "order_type": "limit",
        "short_symbol": f"{it.underlying}-OFFLINE-P{short_k:.0f}",
        "long_symbol": f"{it.underlying}-OFFLINE-P{short_k - width:.0f}",
        "short_strike": short_k, "long_strike": short_k - width, "width": width,
        "credit": credit, "short_delta": ds, "long_delta": dl, "short_iv": 0.14,
        "short_rel_spread": 0.04, "long_rel_spread": 0.05,
        "short_quote_age": 3, "long_quote_age": 3,
        "friction_usd": gates.friction_usd(credit, credit + 0.04,
                                           credit / 2, credit / 2 + 0.05),
        "spot": spot,
    }


def cycle(
    offline: bool,
    execute: bool,
    verbose: bool = True,
    session_snapshot: session.SessionSnapshot | None = None,
    execution_guard: Callable[[], tuple[bool, str]] | None = None,
) -> dict:
    """Propose -> build -> gate -> optionally execute. Every attempt is logged."""
    ctx = {
        "open_positions": 0,
        "trades_today": 0,
        "held_symbols": [],
        "annual_target_reached": False,
        # Offline mode has no broker account.  These explicit synthetic values
        # keep its gate exercise honest without weakening live fail-closed logic.
        "account_status": "ACTIVE",
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
        "options_approved_level": 3,
        "options_trading_level": 3,
        "options_buying_power": 1_000_000.0,
        "equity": 1_000_000.0,
    }
    live_snapshot = session_snapshot
    reentry = {"active": False, "allowed": True, "reason": "offline_or_first_entry"}
    if not offline:
        try:
            live_snapshot = live_snapshot or session.capture()
            ctx.update(live_snapshot.gate_context())
            reentry = risk_state.reentry_status(live_snapshot.now_et)
            ctx["reentry"] = reentry
            if reentry.get("active"):
                ctx["options_bp_utilization"] = reentry.get(
                    "bp_utilization", config.REENTRY_BP_UTILIZATION
                )
        except Exception as exc:
            if execute:
                if verbose:
                    print(f"  SESSION PREFLIGHT FAILED - {type(exc).__name__}: {exc}")
                return {
                    "stop_reason": "session_unavailable",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "waitable": True,
                    "context": ctx,
                }

    if execute and live_snapshot is not None:
        decision = session.entry_decision(live_snapshot, reentry)
        if not decision.allowed:
            if verbose:
                print(f"  ENTRY BLOCKED - {decision.reason}: {decision.detail}")
            return {
                "stop_reason": decision.reason,
                "detail": decision.detail,
                "waitable": decision.waitable,
                "context": ctx,
                "session": live_snapshot.public(),
            }

    brief = OFFLINE_BRIEF if offline else market_brief()
    feedback, last = None, None

    for attempt in range(1, config.PROPOSAL_BUDGET + 1):
        raw, reply = llm.propose(brief, feedback)
        if raw is None:
            last = ledger.record(None, f"no JSON in reply: {reply[:200]}",
                                 None, None, None, attempt, reply)
            if verbose:
                print(f"  [{attempt}] LLM returned no parseable intent")
            feedback = "Return exactly one valid JSON intent matching the required schema."
            continue

        it, err = intent_mod.parse(raw)
        if it is None:
            err = err or "intent failed validation"
            last = ledger.record(raw, err, None, None, None, attempt, reply)
            if verbose:
                print(f"  [{attempt}] INTENT REJECTED - {err}")
            feedback = err
            continue

        ok, why = intent_mod.coherence(it)
        if not ok:
            last = ledger.record(raw, why, None, None, None, attempt, reply)
            if verbose:
                print(f"  [{attempt}] INCOHERENT - {why}")
            feedback = why
            continue

        if offline:
            spread, berr = synthetic_spread(it), ""
        else:
            broker_day = live_snapshot.now_et.date() if live_snapshot else date.today()
            spread, berr = builder.build(
                it,
                spot_for(it.underlying, False),
                broker_day,
                ctx,
            )
        if spread is None:
            last = ledger.record(raw, berr, None, None, None, attempt, reply)
            if verbose:
                print(f"  [{attempt}] BUILD FAILED - {berr}")
            feedback = berr
            continue

        verdict = gates.evaluate(spread, ctx)
        execution = None
        if verdict.approved and execute and not offline:
            if execution_guard is not None:
                guard_allowed, guard_detail = execution_guard()
                if not guard_allowed:
                    execution = {
                        "submitted": False,
                        "error": "risk_monitor_unavailable",
                        "detail": guard_detail,
                    }
            # The AI and chain lookup can take time. Refresh immediately before
            # submission so another pending order or a closing bell wins the race.
            if execution is None:
                try:
                    final_snapshot = session.capture()
                    final_reentry = risk_state.reentry_status(final_snapshot.now_et)
                    final_decision = session.entry_decision(final_snapshot, final_reentry)
                    if final_decision.allowed:
                        # Account eligibility and available options BP can change
                        # while Poe and the chain resolver are working. Re-run the
                        # entire deterministic gate against the final MCP snapshot.
                        final_context = final_snapshot.gate_context()
                        final_context["reentry"] = final_reentry
                        if final_reentry.get("active"):
                            final_context["options_bp_utilization"] = final_reentry.get(
                                "bp_utilization", config.REENTRY_BP_UTILIZATION
                            )
                        final_verdict = gates.evaluate(spread, final_context)
                        if final_verdict.approved:
                            execution = executor.submit(spread, final_verdict)
                            if (
                                execution.get("submitted")
                                and final_reentry.get("active")
                            ):
                                risk_state.mark_reentry_submission(
                                    spread, final_verdict.economics, execution
                                )
                        else:
                            execution = {
                                "submitted": False,
                                "error": "final_gate_failed",
                                "detail": final_verdict.reason,
                                "failures": [
                                    check.name for check in final_verdict.failures
                                ],
                                "session": final_snapshot.public(),
                            }
                    else:
                        execution = {
                            "submitted": False,
                            "error": final_decision.reason,
                            "detail": final_decision.detail,
                            "session": final_snapshot.public(),
                        }
                except Exception as exc:
                    execution = {
                        "submitted": False,
                        "error": "session_unavailable",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }

        last = ledger.record(raw, None, spread, verdict, execution, attempt, reply)
        if verbose:
            _print_verdict(it, spread, verdict, execution)
        if verdict.approved:
            return last
        feedback = retry_feedback(verdict)
        if feedback is None:
            return last

    return last or {}


def _print_verdict(it, spread, verdict, execution) -> None:
    e = verdict.economics
    mark = "APPROVED" if verdict.approved else "VETOED  "
    print(f"\n  {mark} {spread['underlying']} {spread['strategy']} "
          f"{spread['short_strike']:.0f}/{spread['long_strike']:.0f} exp {spread['expiry']}")
    print(f"  thesis: {it.thesis[:100]}")
    print(f"    credit ${e['credit']:.2f}   max profit ${e['max_profit_usd']:.0f}   "
          f"max loss ${e['max_loss_usd']:.0f}")
    print(f"    breakeven WR {e['breakeven_wr']:.1%}   market-implied WR {e['implied_wr']:.1%}   "
          f"edge {e['edge_pp']:+.1f}pp")
    print(f"    EV gross ${e['ev_gross_usd']:+.2f}   friction ${e['friction_usd']:.2f}   "
          f"EV net ${e['ev_net_usd']:+.2f}")
    for c in verdict.checks:
        print(f"      {'PASS' if c.passed else 'FAIL'}  {c.name:20} {c.detail}")
    if execution:
        print(f"    execution: {json.dumps(execution)[:200]}")


def check() -> int:
    print("PacaPounce environment check")
    print(f"  gate version         {config.GATE_VERSION}")
    print(f"  POE_KEY              {'set' if config.POE_KEY else 'MISSING'}")
    print(f"  POE_MODEL            {config.POE_MODEL}")
    print(f"  ALPACA key           {'set' if config.ALPACA_KEY else 'MISSING'}")
    print(f"  options feed         {config.OPTIONS_FEED}")
    print(f"  allowlist            {config.ALLOWLIST}")
    print(f"  sizing objective     {config.SIZING_MODE}")
    print(f"  options BP use       {config.OPTIONS_BP_UTILIZATION:.0%}")
    print(
        f"  second Paper strategy {'ON' if config.STOCK_MR_ENABLED else 'OFF'} - "
        f"NDX30 mean reversion, {len(config.STOCK_MR_UNIVERSE)} symbols"
    )

    raw, reply = llm.propose("Date: test. SPY spot 640, calm tape. Propose one trade.")
    print(f"  LLM round-trip       {'OK - intent parsed' if raw else 'FAILED: ' + reply[:120]}")

    try:
        tools = mcp_client.run(mcp_client.list_tools())
        print(f"  MCP server           OK - {len(tools)} tools")
    except Exception as e:
        print(f"  MCP server           FAILED: {type(e).__name__}: {e}")
        return 1
    try:
        acct = mcp_client.run(mcp_client.call("get_account_info"))
        equity = (acct or {}).get("equity")
        if equity:
            live_account = str((acct or {}).get("account_number") or "").strip()
            account_match = bool(config.ALPACA_ACCOUNT_ID) and (
                live_account == config.ALPACA_ACCOUNT_ID
            )
            print(
                f"  Alpaca paper account {'OK' if account_match else 'MISMATCH'} - "
                f"{live_account or 'missing'}, equity ${float(equity):,.0f}"
            )
            if not account_match:
                print(
                    f"  configured account   {config.ALPACA_ACCOUNT_ID or 'MISSING'}"
                )
                return 1
            level = int(float((acct or {}).get("options_trading_level") or 0))
            approved = int(float((acct or {}).get("options_approved_level") or 0))
            options_bp = float((acct or {}).get("options_buying_power") or 0)
            eligible = (
                str((acct or {}).get("status") or "").upper() == "ACTIVE"
                and level >= config.MIN_OPTIONS_TRADING_LEVEL
                and approved >= config.MIN_OPTIONS_TRADING_LEVEL
                and options_bp > 0
                and not (acct or {}).get("trading_blocked")
                and not (acct or {}).get("account_blocked")
                and not (acct or {}).get("trade_suspended_by_user")
            )
            print(
                f"  options eligibility  {'OK' if eligible else 'BLOCKED'} - "
                f"approved L{approved}, enabled L{level}, BP ${options_bp:,.2f}"
            )
        else:
            print(f"  Alpaca paper account reachable but no equity field: {str(acct)[:120]}")
    except Exception as e:
        print(f"  Alpaca paper account FAILED ({type(e).__name__}) - generate PAPER keys")
        return 1
    return 0


def _start_monitor() -> subprocess.Popen:
    """Start the paper-only risk monitor in this terminal's process group."""
    command = [
        sys.executable,
        str(ROOT / "scripts" / "monitor.py"),
        "--execute",
        "--interval",
        str(config.MONITOR_INTERVAL_SEC),
    ]
    process = subprocess.Popen(command, cwd=str(ROOT))
    # Catch immediate configuration/startup failures before allowing an entry.
    time.sleep(0.25)
    if process.poll() is not None:
        raise RuntimeError(f"risk monitor exited during startup (code {process.returncode})")
    print(f"  RISK MONITOR STARTED - pid={process.pid}", flush=True)
    return process


def _ensure_monitor(process: subprocess.Popen | None) -> subprocess.Popen:
    if process is not None and process.poll() is None:
        return process
    if process is not None:
        print(
            f"  RISK MONITOR EXITED - code={process.returncode}; restarting",
            flush=True,
        )
    return _start_monitor()


def _stop_monitor(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    print("  RISK MONITOR STOPPED", flush=True)


def _start_dashboard() -> subprocess.Popen:
    """Start the live dashboard builder bundled with the paper session."""
    command = [
        sys.executable,
        str(ROOT / "scripts" / "build_dashboard.py"),
        "--watch",
        "--interval",
        str(config.DASHBOARD_REFRESH_INTERVAL_SEC),
    ]
    process = subprocess.Popen(command, cwd=str(ROOT))
    time.sleep(0.25)
    if process.poll() is not None:
        raise RuntimeError(f"dashboard builder exited during startup (code {process.returncode})")
    print(
        f"  DASHBOARD STARTED - pid={process.pid}; refresh="
        f"{config.DASHBOARD_REFRESH_INTERVAL_SEC}s; "
        "local=dashboard/runtime/index.html",
        flush=True,
    )
    return process


def _ensure_dashboard(process: subprocess.Popen | None) -> subprocess.Popen:
    if process is not None and process.poll() is None:
        return process
    if process is not None:
        print(
            f"  DASHBOARD EXITED - code={process.returncode}; restarting",
            flush=True,
        )
    return _start_dashboard()


def _stop_dashboard(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        print("  DASHBOARD WATCHER STOPPED", flush=True)

    # Capture the broker's final close/session state after the watcher stops.
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_dashboard.py")],
            cwd=str(ROOT),
            check=True,
            timeout=60,
        )
        print(
            "  DASHBOARD FINAL SNAPSHOT WRITTEN - "
            "dashboard/runtime/index.html",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  DASHBOARD FINAL REFRESH FAILED - {type(exc).__name__}: {exc}",
            flush=True,
        )


def _closed_wait_seconds(snapshot: session.SessionSnapshot) -> int:
    """Wake hourly while closed, then every minute near Alpaca's next open."""
    minimum = max(60, config.SESSION_POLL_INTERVAL_SEC)
    if snapshot.next_open is None:
        return minimum
    until_open = (snapshot.next_open - snapshot.now_et).total_seconds()
    return int(max(minimum, min(until_open - 300, 3600)))


def autonomous_loop(offline: bool) -> int:
    """Run entry hunting and risk monitoring across successive broker sessions."""
    if offline:
        approved = 0
        while approved < config.MAX_TRADES_PER_DAY:
            result = cycle(True, execute=True)
            if (result.get("verdict") or {}).get("approved"):
                approved += 1
            time.sleep(config.SESSION_POLL_INTERVAL_SEC)
        return 0

    print(
        "MCP paper session active: one command owns entry hunting, the risk "
        "monitor, and the live dashboard; it stays resident across market "
        "closes and resumes from Alpaca MCP's next_open."
    )
    monitor_process: subprocess.Popen | None = None
    dashboard_process: subprocess.Popen | None = None
    try:
        while True:
            try:
                snapshot = session.capture()
                reentry = risk_state.reentry_status(snapshot.now_et)
                decision = session.entry_decision(snapshot, reentry)
            except Exception as exc:
                print(
                    f"  SESSION UNAVAILABLE - fail closed; retrying in "
                    f"{config.SESSION_POLL_INTERVAL_SEC}s ({type(exc).__name__}: {exc})",
                    flush=True,
                )
                time.sleep(config.SESSION_POLL_INTERVAL_SEC)
                continue

            status = snapshot.public()
            print(
                f"  {status['now_et']} | {status['phase']} | "
                f"entries={status['trades_today']}/{config.MAX_TRADES_PER_DAY} | "
                f"pending-open={status['pending_opening_orders']} | "
                f"spreads={status['open_spreads']}/{config.MAX_OPEN_POSITIONS} | "
                f"stocks={status['stock_positions']}/{config.STOCK_MR_MAX_POSITIONS} | "
                f"P&L=${status['daily_pnl_usd']:+.2f}/${status['daily_target_usd']:.2f}",
                flush=True,
            )

            if not snapshot.market_open:
                if monitor_process is not None:
                    _stop_monitor(monitor_process)
                    monitor_process = None
                if dashboard_process is not None:
                    _stop_dashboard(dashboard_process)
                    dashboard_process = None
                wait_seconds = _closed_wait_seconds(snapshot)
                next_open = (
                    snapshot.next_open.isoformat() if snapshot.next_open else "unknown"
                )
                print(
                    f"  SESSION WAIT - market_closed: phase={snapshot.phase}; "
                    f"next_open={next_open}; recheck={wait_seconds}s",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue

            try:
                dashboard_process = _ensure_dashboard(dashboard_process)
            except Exception as exc:
                # Observability is not a trading permission dependency. Keep
                # retrying while the execution monitor remains fail-closed.
                print(
                    f"  DASHBOARD UNAVAILABLE - retrying in "
                    f"{config.SESSION_POLL_INTERVAL_SEC}s "
                    f"({type(exc).__name__}: {exc})",
                    flush=True,
                )

            try:
                monitor_process = _ensure_monitor(monitor_process)
            except Exception as exc:
                print(
                    f"  RISK MONITOR UNAVAILABLE - entry blocked; retrying in "
                    f"{config.SESSION_POLL_INTERVAL_SEC}s "
                    f"({type(exc).__name__}: {exc})",
                    flush=True,
                )
                time.sleep(config.SESSION_POLL_INTERVAL_SEC)
                continue

            mr_result = mean_reversion.maybe_enter(snapshot)
            mr_submitted = bool(
                mr_result and mr_result.get("status") == "SUBMITTED"
            )
            if mr_result:
                print(
                    f"  STOCK MR - {mr_result.get('status')}: "
                    f"{mr_result.get('reason') or 'portfolio preflight in progress'}",
                    flush=True,
                )

            if mr_submitted:
                print(
                    "  OPTION ENTRY WAIT - stock MR order reached Alpaca; "
                    "refreshing broker state before any other proposal",
                    flush=True,
                )
            elif decision.allowed:
                cycle(
                    False,
                    execute=True,
                    session_snapshot=snapshot,
                    execution_guard=lambda: (
                        monitor_process is not None
                        and monitor_process.poll() is None,
                        "bundled risk monitor is not running",
                    ),
                )
            elif decision.waitable or decision.reason in {
                "daily_trade_limit", "annual_target_reached",
                "alpaca_options_ineligible",
            }:
                print(f"  ENTRY WAIT - {decision.reason}: {decision.detail}", flush=True)
            else:
                print(f"  SESSION STOP - {decision.reason}: {decision.detail}", flush=True)
                return 0
            time.sleep(config.SESSION_POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\n  SESSION INTERRUPTED - shutting down", flush=True)
        return 130
    finally:
        _stop_monitor(monitor_process)
        if dashboard_process is not None:
            _stop_dashboard(dashboard_process)


def main() -> int:
    p = argparse.ArgumentParser(description="PacaPounce patient AI trading agent")
    p.add_argument("--check", action="store_true", help="environment diagnostics")
    p.add_argument("--propose", action="store_true", help="one cycle, no execution")
    p.add_argument("--trade", action="store_true", help="one cycle, execute if approved")
    p.add_argument("--measure", type=int, metavar="N", help="N proposals, no execution")
    p.add_argument(
        "--loop", action="store_true",
        help="one-command MCP entry session + risk monitor + live dashboard",
    )
    p.add_argument("--summary", action="store_true", help="ledger statistics")
    p.add_argument("--offline", action="store_true", help="no Alpaca; synthetic chain")
    a = p.parse_args()

    if a.check:
        return check()
    if a.summary:
        print(json.dumps(ledger.summary(), indent=2))
        return 0
    if a.measure:
        print(f"Measuring {a.measure} proposals (no execution)...")
        for i in range(a.measure):
            print(f"\n=== proposal {i + 1}/{a.measure} ===")
            cycle(a.offline, execute=False)
        print("\n" + json.dumps(ledger.summary(), indent=2))
        return 0
    if a.loop:
        return autonomous_loop(a.offline)
    if a.propose or a.trade:
        cycle(a.offline, execute=a.trade)
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
