"""Restart-safe profit-ratchet and re-entry lifecycle tests."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto import config, ledger, risk_state  # noqa: E402


ET = ZoneInfo("America/New_York")
SPREAD = {
    "underlying": "SPY",
    "right": "P",
    "short_symbol": "SPY260828P00757000",
    "long_symbol": "SPY260828P00752000",
    "short_strike": 757.0,
    "long_strike": 752.0,
    "entry_credit": 0.57,
}


def metrics(pnl: float, max_profit: float = 100.0) -> dict:
    return {
        "quote_ready": True,
        "pnl_executable": pnl,
        "max_profit": max_profit,
        "spot": 765.0,
        "profit_captured": pnl / max_profit,
        "short_quote": {"bid": 1.00, "ask": 1.01},
        "long_quote": {"bid": 0.45, "ask": 0.46},
    }


def test_ratchet_requires_arm_drawdown_slope_and_confirmations(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_LOG", tmp_path / "missing.jsonl")
    monkeypatch.setattr(config, "MONITOR_HIGH_VOL_PCT_MAX_PROFIT", 99.0)
    path = tmp_path / "risk.json"
    now = datetime(2026, 8, 26, 10, 0, tzinfo=ET)

    results = []
    for index, pnl in enumerate((0.0, 25.0, 19.0, 18.0, 17.0)):
        results.append(risk_state.observe(
            SPREAD, metrics(pnl), now + timedelta(seconds=30 * index), path
        ))

    assert results[1]["ratchet_armed"]
    assert results[-1]["ratchet_high_water_pnl"] == 25.0
    assert results[-1]["ratchet_trailing_floor_pnl"] == 20.0
    assert results[-1]["ratchet_breach_count"] == 2
    assert results[-1]["ratchet_exit"]


def test_high_money_volatility_tightens_but_does_not_itself_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_LOG", tmp_path / "missing.jsonl")
    monkeypatch.setattr(config, "MONITOR_HIGH_VOL_PCT_MAX_PROFIT", 0.001)
    path = tmp_path / "risk.json"
    now = datetime(2026, 8, 26, 10, 0, tzinfo=ET)

    risk_state.observe(SPREAD, metrics(0.0), now, path)
    result = risk_state.observe(SPREAD, metrics(25.0), now + timedelta(seconds=30), path)
    result = risk_state.observe(SPREAD, metrics(24.0), now + timedelta(seconds=60), path)

    assert result["pnl_volatility_high"]
    assert result["ratchet_giveback_pct"] == config.MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT
    assert not result["ratchet_exit"]


def test_restart_recovers_full_high_water_not_only_recent_tail(tmp_path, monkeypatch):
    log_path = tmp_path / "session.jsonl"
    rows = []
    for pnl in [10.0, 90.0] + [20.0] * 70:
        rows.append(json.dumps({
            "kind": "cycle",
            "monitored_spreads": [{
                "short_symbol": SPREAD["short_symbol"],
                "long_symbol": SPREAD["long_symbol"],
                "pnl_executable": pnl,
            }],
        }))
    log_path.write_text("\n".join(rows), encoding="utf-8")
    monkeypatch.setattr(config, "SESSION_LOG", log_path)

    result = risk_state.observe(
        SPREAD,
        metrics(30.0),
        datetime(2026, 8, 26, 11, 0, tzinfo=ET),
        tmp_path / "risk.json",
    )
    assert result["ratchet_high_water_pnl"] == 90.0


def test_profit_exit_cools_down_then_requires_comparison(tmp_path):
    path = tmp_path / "risk.json"
    now = datetime(2026, 8, 26, 10, 0, tzinfo=ET)
    state = risk_state._blank()
    state["last_exit"] = {
        "submitted_at": now.isoformat(),
        "cooldown_until": (now + timedelta(minutes=30)).isoformat(),
        "action": "profit_ratchet",
        "eligible": True,
        "reentry_consumed": False,
        "entry_baseline": {"quality": 0.02},
        "post_exit_market_ready": True,
    }
    risk_state.save(state, path)

    cooling = risk_state.reentry_status(now + timedelta(minutes=5), path)
    ready = risk_state.reentry_status(now + timedelta(minutes=31), path)
    assert not cooling["allowed"]
    assert cooling["reason"] == "reentry_cooldown"
    assert ready["allowed"]
    assert ready["reason"] == "reentry_comparison_required"


def test_post_exit_market_must_be_calm_for_ten_minutes(tmp_path):
    path = tmp_path / "risk.json"
    now = datetime(2026, 8, 26, 10, 0, tzinfo=ET)
    state = risk_state._blank()
    state["last_exit"] = {
        "submitted_at": now.isoformat(),
        "cooldown_until": now.isoformat(),
        "action": "profit_ratchet",
        "eligible": True,
        "reentry_consumed": False,
        "short_symbol": SPREAD["short_symbol"],
        "long_symbol": SPREAD["long_symbol"],
        "qty": 69,
        "max_profit": 3933.0,
        "post_exit_values": [],
        "post_exit_stable_since": None,
        "entry_baseline": {"quality": 0.02},
    }
    risk_state.save(state, path)
    short_quote = {"bid": 1.00, "ask": 1.01}
    long_quote = {"bid": 0.45, "ask": 0.46}
    result = None
    for minute in range(13):
        result = risk_state.observe_post_exit_market(
            short_quote, long_quote, now + timedelta(minutes=minute), path
        )

    assert result is not None and result["ready"]
    assert result["stable_minutes"] >= config.REENTRY_STABLE_MIN


def test_risk_exit_locks_reentry_for_session(tmp_path):
    path = tmp_path / "risk.json"
    now = datetime(2026, 8, 26, 10, 0, tzinfo=ET)
    state = risk_state._blank()
    state["last_exit"] = {
        "submitted_at": now.isoformat(),
        "cooldown_until": now.isoformat(),
        "action": "stop_loss",
        "eligible": False,
        "reentry_consumed": False,
    }
    risk_state.save(state, path)

    status = risk_state.reentry_status(now + timedelta(hours=1), path)
    assert not status["allowed"]
    assert status["reason"] == "reentry_risk_exit_lockout"


def test_exit_record_captures_prior_gate_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "load", lambda: [{
        "spread": {
            "short_symbol": SPREAD["short_symbol"],
            "long_symbol": SPREAD["long_symbol"],
        },
        "execution": {"submitted": True},
        "verdict": {
            "approved": True,
            "economics": {
                "ev_net_usd": 8.0,
                "max_loss_usd": 400.0,
                "short_delta": 0.20,
            },
        },
    }])
    now = datetime(2026, 8, 26, 10, 0, tzinfo=ET)
    exit_metrics = metrics(25.0)
    exit_metrics.update({
        "ratchet_high_water_pnl": 40.0,
        "ratchet_trailing_floor_pnl": 32.0,
        "ratchet_breach_count": 2,
        "ratchet_giveback_pct": 0.20,
    })
    recorded = risk_state.record_exit(
        SPREAD,
        exit_metrics,
        "profit_ratchet",
        now,
        {"client_order_id": "veto-close-test"},
        tmp_path / "risk.json",
    )
    assert recorded["eligible"]
    assert recorded["entry_baseline"]["quality"] == 0.02
    assert recorded["safe_buffer_pct"] > 0
    assert recorded["ratchet_high_water_pnl"] == 40.0
    assert recorded["ratchet_trailing_floor_pnl"] == 32.0
    assert recorded["ratchet_breach_count"] == 2
