"""Verdict log - every proposal, approved or vetoed, append-only.

This file is the audit trail, the dashboard's data source, and the input to the
gate-validation replay. Rejected proposals are logged in full: a gate you can't
audit is a gate you can't trust.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config
from .gates import Verdict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(intent_raw: dict | None, intent_err: str | None,
           spread: dict | None, verdict: Verdict | None,
           execution: dict | None, attempt: int, raw_reply: str = "") -> dict:
    entry = {
        "ts": _now(),
        "gate_version": config.GATE_VERSION,
        "attempt": attempt,
        "intent": intent_raw,
        "intent_error": intent_err,
        "spread": spread,
        "verdict": None if verdict is None else {
            "approved": verdict.approved,
            "reason": verdict.reason,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                       for c in verdict.checks],
            "economics": verdict.economics,
        },
        "execution": execution,
        "raw_reply_chars": len(raw_reply),
    }
    config.VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with config.VERDICT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def load() -> list[dict]:
    if not config.VERDICT_LOG.exists():
        return []
    out = []
    for line in config.VERDICT_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def summary() -> dict:
    rows = load()
    scored = [r for r in rows if r.get("verdict")]
    approved = [r for r in scored if r["verdict"]["approved"]]
    executed = [r for r in rows if (r.get("execution") or {}).get("submitted")]

    fail_counts: dict[str, int] = {}
    for r in scored:
        if r["verdict"]["approved"]:
            continue
        for c in r["verdict"]["checks"]:
            if not c["passed"]:
                fail_counts[c["name"]] = fail_counts.get(c["name"], 0) + 1

    return {
        "proposals": len(rows),
        "scored": len(scored),
        "approved": len(approved),
        "vetoed": len(scored) - len(approved),
        "pass_rate": round(len(approved) / len(scored), 4) if scored else None,
        "executed": len(executed),
        "veto_reasons": dict(sorted(fail_counts.items(), key=lambda kv: -kv[1])),
        "gate_version": config.GATE_VERSION,
    }
