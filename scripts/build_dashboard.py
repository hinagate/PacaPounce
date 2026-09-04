#!/usr/bin/env python
"""Render the PacaPounce dashboard around the four judging criteria.

Sections map one-to-one onto what judges score:
  1  P&L Performance          realized + open paper P&L, predicted vs actual
  2  Technology Implementation MCP tools actually exercised, with call counts
  3  Creativity & Originality  the gate, its pass rate, and its validation
  4  Presentation & Execution  executions plus representative decisions

Data is embedded rather than fetched, so the page works from file://, GitHub
Pages, or any static host with no CORS configuration.

Usage:
    python scripts/build_dashboard.py              # ignored runtime snapshot
    python scripts/build_dashboard.py --no-live    # skip the Alpaca call
    python scripts/build_dashboard.py --watch      # rebuild continuously
    python scripts/build_dashboard.py --output dashboard/index.html
                                                  # explicit public snapshot
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from veto import config, gates, ledger, mean_reversion, risk_state, sizing  # noqa: E402

PUBLIC_OUT = ROOT / "dashboard" / "index.html"
OUT = ROOT / "dashboard" / "runtime" / "index.html"
RECENT_DECISION_WINDOWS = 10

CSS = """
:root{--bg:#fbfbfa;--panel:#fff;--ink:#1c1c1a;--muted:#6b6b66;--line:#e5e4e0;
--pass:#1a7f4b;--fail:#b3261e;--accent:#2f5fd8;--chip:#f2f1ed;--warn:#8a6100}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--panel:#1e1e1c;--ink:#eceae4;
--muted:#9c9a93;--line:#33322e;--pass:#4ec27f;--fail:#ef6b60;--accent:#7fa2f5;
--chip:#26251f;--warn:#d9a441}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:34px 20px 90px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.025em}
.sub{color:var(--muted);margin:0 0 30px;font-size:14px;max-width:70ch}
section{margin:0 0 38px}
.crit{display:flex;align-items:baseline;gap:10px;margin:0 0 14px;
padding-bottom:8px;border-bottom:1px solid var(--line)}
.crit .n{font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--accent)}
.crit h2{font-size:16px;margin:0;letter-spacing:-.01em}
.crit .why{font-size:12px;color:var(--muted);margin-left:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.tile .v{font-size:25px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
.tile .f{font-size:11px;color:var(--muted);margin-top:2px}
.note{background:var(--chip);border:1px solid var(--line);border-radius:10px;
padding:13px 16px;font-size:13px;color:var(--muted);margin-top:12px}
.note b{color:var(--ink)}
.warn{border-color:var(--warn)}
.warn b{color:var(--warn)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:860px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.ok{background:color-mix(in srgb,var(--pass) 16%,transparent);color:var(--pass)}
.no{background:color-mix(in srgb,var(--fail) 16%,transparent);color:var(--fail)}
.pos{color:var(--pass)}.neg{color:var(--fail)}
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:0 0 30px}
.banner .b-item{background:var(--panel);padding:14px 16px;display:flex;flex-direction:column;gap:3px}
.banner .b-k{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.banner .b-v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.refresh-btn{display:block;width:100%;border:0;border-radius:9px;background:var(--accent);
color:#fff;padding:11px 16px;margin:16px 0 22px;font:600 13px/1.2 inherit;cursor:pointer}
.refresh-btn:hover{filter:brightness(1.08)}
.overview-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:0 0 9px}
.overview-head h2{font-size:17px;margin:0}.overview-head p{font-size:11px;color:var(--muted);margin:0}
.simple-table{overflow-x:auto;border:1px solid var(--line);border-radius:9px;background:var(--panel);
margin:0 0 20px}.simple-table table{min-width:0}.simple-table th,.simple-table td{white-space:normal}
.simple-table th{background:var(--chip)}.simple-table td:first-child{font-weight:600}
.decision-why{min-width:280px;max-width:540px;color:var(--muted);line-height:1.45}
.deep-dive{margin:26px 0 0;border-top:1px solid var(--line);padding-top:18px}
.deep-dive>section:first-of-type{margin-top:28px}
.funnel{display:flex;flex-direction:column;gap:9px;margin:14px 0 6px}
.fn-row{display:flex;align-items:center;gap:12px}
.fn-label{width:150px;flex:none;font-size:12px;color:var(--muted);text-align:right}
.fn-track{flex:1;min-width:0}
.fn-bar{height:34px;border-radius:7px;display:flex;align-items:center;padding:0 12px;color:#fff;
font-weight:600;font-size:13px;font-variant-numeric:tabular-nums;min-width:44px;
background:linear-gradient(90deg,var(--accent),color-mix(in srgb,var(--accent) 45%,var(--panel)))}
.fn-sub{width:120px;flex:none;font-size:11px;color:var(--muted)}
.cbars{display:flex;flex-direction:column;gap:9px;margin:14px 0 6px}
.cbar-row{display:flex;align-items:center;gap:12px}
.cbar-label{width:180px;flex:none;font-size:12px;color:var(--muted);text-align:right}
.cbar-track{flex:1;min-width:0;background:var(--chip);border-radius:6px;overflow:hidden;height:26px}
.cbar-fill{height:100%;border-radius:6px;background:var(--accent)}
.cbar-fill.pos{background:var(--pass)}.cbar-fill.neg{background:var(--fail)}
.cbar-fill.mut{background:color-mix(in srgb,var(--muted) 55%,var(--panel))}
.cbar-val{width:110px;flex:none;font-size:13px;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
@media(max-width:640px){.fn-label,.cbar-label{width:96px}.fn-sub{display:none}}
details summary{cursor:pointer;color:var(--accent);font-size:12px}
.checks{margin:8px 0 2px;padding-left:0;list-style:none;font-size:12px;white-space:normal}
.checks li{padding:2px 0;color:var(--muted)}
.checks .n{color:var(--fail);font-weight:600}.checks .y{color:var(--pass)}
.thesis{white-space:normal;min-width:230px;max-width:300px;color:var(--muted);font-size:12px}
.gate-audit{white-space:normal;min-width:300px}
@media(min-width:1200px){.scroll.full{margin-left:calc(580px - 50vw);margin-right:calc(580px - 50vw)}}
/* 12 nowrap columns needed ~1535px inside a 1080px page, so the two columns
   that matter most - AI rationale and gate audit - sat off-screen behind a
   horizontal scrollbar. Wrapping the headers and tightening the widest cells
   brings the table back inside the page. */
thead th{white-space:normal;vertical-align:bottom;line-height:1.25}
.bar{height:7px;border-radius:4px;background:var(--chip);overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:var(--accent)}
.flow{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:14px 0 12px}
.flow-step{position:relative;background:var(--panel);border:1px solid var(--line);
border-radius:10px;padding:14px;min-height:125px}
.flow-step .step{font-size:10px;font-weight:700;letter-spacing:.09em;color:var(--accent)}
.flow-step h3{font-size:14px;margin:5px 0 6px}.flow-step p{font-size:12px;color:var(--muted);margin:0}
.flow-step:not(:last-child):after{content:'→';position:absolute;right:-10px;top:47px;
z-index:2;color:var(--accent);font-weight:700;background:var(--bg);padding:0 2px}
.intent-list{display:grid;gap:12px;margin-top:12px}
.intent-card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.intent-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:12px}
.intent-head h3{font-size:15px;margin:0}.intent-head .meta{font-size:11px;color:var(--muted)}
.intent-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:8px}
.intent-field{background:var(--chip);border-radius:7px;padding:9px 10px}
.intent-field .k{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.intent-field .v{font-size:13px;font-weight:600;margin-top:2px;overflow-wrap:anywhere}
.intent-copy{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.intent-copy div{border-left:3px solid var(--accent);padding:7px 10px;background:var(--chip);font-size:12px}
.intent-copy b{display:block;font-size:9px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.resolution{margin-top:10px;padding:10px 12px;border:1px dashed var(--line);border-radius:8px;
font-size:12px;color:var(--muted)}.resolution b{color:var(--ink)}
.gate-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:9px;margin:12px 0}
.gate-card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px}
.gate-card .top{display:flex;align-items:center;gap:8px}.gate-card .seq{display:grid;place-items:center;
width:24px;height:24px;border-radius:50%;background:var(--accent);color:white;font-size:11px;font-weight:700}
.gate-card h3{font-size:13px;margin:0}.gate-card p{font-size:11px;color:var(--muted);margin:7px 0 0}
.layer{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--accent);margin-left:auto}
.checks .m{color:var(--warn);font-weight:600}
@media(max-width:800px){.flow{grid-template-columns:1fr}.flow-step{min-height:0}
.flow-step:not(:last-child):after{content:'↓';right:auto;left:20px;top:auto;bottom:-13px}
.intent-copy{grid-template-columns:1fr}}
"""

JS = """
const R=DATA.rows,D=DATA.presentation,T=DATA.tools,G=DATA.gates;
const f=(n,d=2)=>n==null?'-':(n>=0?'+':'')+n.toFixed(d);
const esc=v=>String(v??'').replace(/[&<>\"']/g,ch=>({
  '&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));
const words=v=>esc(String(v??'-').replaceAll('_',' '));

// Canonical current gate sequence, shared with the Python evaluator.
const gp=document.getElementById('gate-pipeline');
G.forEach((g,idx)=>{const d=document.createElement('div');d.className='gate-card';
  d.innerHTML=`<div class="top"><span class="seq">${idx+1}</span><h3>${esc(g.label)}</h3>
    <span class="layer">${esc(g.layer)}</span></div><p>${esc(g.question)}</p>`;
  gp.appendChild(d);});

const tb=document.getElementById('tb');
R.forEach(r=>{
  const v=r.verdict,e=(v&&v.economics)||{},ok=v&&v.approved;
  const checks=(v&&v.checks)||[],byName=new Map(checks.map(c=>[c.name,c]));
  const passed=checks.filter(c=>c.passed).length,failed=checks.length-passed;
  const ordered=G.map((g,idx)=>{
    const c=byName.get(g.name);
    return c
      ? `<li><span class="${c.passed?'y':'n'}">${c.passed?'PASS':'FAIL'}</span> ${idx+1}. ${esc(g.label)} — ${esc(c.detail)}</li>`
      : `<li><span class="m">NOT RECORDED</span> ${idx+1}. ${esc(g.label)} — added after gate ${esc(r.gate_version)}</li>`;
  }).join('');
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${r.n}</td>
   <td><span class="badge ${ok?'ok':'no'}">${ok?'APPROVED':'VETOED'}</span></td>
   <td>${esc(r.label)}</td>
   <td class="num">${e.credit!=null?'$'+e.credit.toFixed(2):'-'}</td>
   <td class="num">${e.max_loss_usd!=null?'$'+e.max_loss_usd.toFixed(0):'-'}</td>
   <td class="num">${e.breakeven_wr!=null?(e.breakeven_wr*100).toFixed(1)+'%':'-'}</td>
   <td class="num">${e.implied_wr!=null?(e.implied_wr*100).toFixed(1)+'%':'-'}</td>
   <td class="num ${e.edge_pp>0?'pos':'neg'}">${e.edge_pp!=null?f(e.edge_pp,1)+'pp':'-'}</td>
   <td class="num">${e.friction_usd!=null?'$'+e.friction_usd.toFixed(2):'-'}</td>
   <td class="num ${e.ev_net_usd>0?'pos':'neg'}"><b>${e.ev_net_usd!=null?'$'+f(e.ev_net_usd):'-'}</b></td>
   <td class="thesis"><b>Thesis:</b> ${esc((r.intent||{}).thesis||'-')}<br><br>
     <b>Invalidation:</b> ${esc((r.intent||{}).invalidation||'-')}</td>
   <td class="gate-audit">${v?`<details><summary>${checks.length}/${G.length} recorded · ${passed} pass · ${failed} fail</summary>
     <ul class="checks">${ordered}</ul></details>`:'Not scored'}</td>`;
  tb.appendChild(tr);
});

const vr=document.getElementById('vr'),vre=Object.entries(D.veto_reasons||{});
if(!vre.length) vr.innerHTML='<div class="note">No decision window has been blocked yet; failed gates will appear here as evidence accumulates.</div>';
vre.forEach(([k,n])=>{
  const pct=D.blocked_windows?(n/D.blocked_windows*100):0;
  const d=document.createElement('div');d.className='tile';
  d.innerHTML=`<div class="k">${esc(k)}</div><div class="v">${n}</div>
    <div class="f">${pct.toFixed(0)}% of blocked windows</div>
    <div class="bar"><i style="width:${pct.toFixed(0)}%"></i></div>`;
  vr.appendChild(d);
});
const tl=document.getElementById('tl');
const te=Object.entries(T).sort((a,b)=>b[1]-a[1]);
if(!te.length){tl.innerHTML='<div class="tile"><div class="k">MCP calls</div><div class="v">0</div><div class="f">run the agent to populate</div></div>';}
te.forEach(([k,n])=>{const d=document.createElement('div');d.className='tile';
  d.innerHTML=`<div class="k">${esc(k)}</div><div class="v">${n}</div><div class="f">MCP tool calls</div>`;
  tl.appendChild(d);});
"""


def label(row: dict) -> str:
    s = row.get("spread")
    if not s:
        return (row.get("intent_error") or "no intent")[:64]
    return (f"{s['underlying']} {s['strategy'].replace('_', ' ')} "
            f"{s['short_strike']:.0f}/{s['long_strike']:.0f}")


def resolved_spread(row: dict) -> dict | None:
    """Return only judge-facing contract fields; never embed broker envelopes."""
    spread = row.get("spread")
    if not spread:
        return None
    keys = ("underlying", "strategy", "expiry", "qty", "short_strike",
            "long_strike", "credit")
    return {key: spread.get(key) for key in keys}


def group_decision_windows(entries: list[dict]) -> list[list[tuple[int, dict]]]:
    """Group retry attempts into the decision windows that produced them."""
    windows: list[list[tuple[int, dict]]] = []
    current: list[tuple[int, dict]] = []
    for number, row in enumerate(entries, 1):
        try:
            attempt = int(row.get("attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1
        if current and attempt <= 1:
            windows.append(current)
            current = []
        current.append((number, row))
    if current:
        windows.append(current)
    return windows


def _representative(window: list[tuple[int, dict]]) -> tuple[int, dict]:
    """Choose the most informative result without changing the raw audit."""
    for item in reversed(window):
        if (item[1].get("execution") or {}).get("submitted"):
            return item
    for item in reversed(window):
        if item[1].get("verdict"):
            return item
    return window[-1]


def presentation_entries(
    entries: list[dict], recent_windows: int = RECENT_DECISION_WINDOWS,
) -> tuple[list[tuple[int, dict]], dict]:
    """Return a bounded judge view and aggregate decision-window statistics.

    The append-only JSONL remains the complete audit. The dashboard keeps every
    approved or submitted result, then adds one representative result from the
    newest decision windows.
    """
    windows = group_decision_windows(entries)
    representatives = [_representative(window) for window in windows]
    selected: dict[int, dict] = {}
    for number, row in enumerate(entries, 1):
        verdict = row.get("verdict") or {}
        if verdict.get("approved") or (row.get("execution") or {}).get("submitted"):
            selected[number] = row
    recent_limit = max(0, recent_windows)
    recent_representatives = representatives[-recent_limit:] if recent_limit else []
    for number, row in recent_representatives:
        selected[number] = row

    approved_windows = sum(
        any((row.get("verdict") or {}).get("approved") for _, row in window)
        for window in windows
    )
    submitted_windows = sum(
        any((row.get("execution") or {}).get("submitted") for _, row in window)
        for window in windows
    )
    blocked_windows = len(windows) - approved_windows
    veto_reasons: dict[str, int] = {}
    for _, row in representatives:
        verdict = row.get("verdict") or {}
        if verdict.get("approved"):
            continue
        failed = [
            check.get("name", "unknown")
            for check in verdict.get("checks", [])
            if not check.get("passed")
        ]
        if not failed:
            failed = ["intent_or_build_failure"]
        for reason in failed:
            veto_reasons[reason] = veto_reasons.get(reason, 0) + 1

    stats = {
        "raw_attempts": len(entries),
        "decision_windows": len(windows),
        "approved_windows": approved_windows,
        "submitted_windows": submitted_windows,
        "blocked_windows": blocked_windows,
        "collapsed_retries": max(0, len(entries) - len(windows)),
        "shown_rows": len(selected),
        "suppressed_rows": max(0, len(entries) - len(selected)),
        "recent_window_limit": recent_limit,
        "veto_reasons": dict(sorted(veto_reasons.items(), key=lambda item: -item[1])),
    }
    return sorted(selected.items(), reverse=True), stats


def money(v, digits: int = 2) -> str:
    if v is None:
        return "-"
    return f"{'-' if v < 0 else ''}${abs(v):,.{digits}f}"


def signed(v, digits: int = 2) -> str:
    """Money with an explicit operator, for sums a reader is meant to check."""
    if v is None:
        return "-"
    return f"{'-' if v < 0 else '+'} ${abs(v):,.{digits}f}"


def overview_time(value: object) -> str:
    """Compact Eastern timestamp for the judge-facing decision table."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo("America/New_York")).strftime(
            "%b %d %H:%M:%S"
        )
    except (TypeError, ValueError):
        return str(value or "-")[:19]


def funnel_svg(stats: dict) -> str:
    """AI-proposes / policy-disposes as one shrinking-bar funnel (inline SVG)."""
    steps = [
        ("Raw AI attempts", stats.get("both_raw_attempts", stats.get("raw_attempts", 0)),
         "model calls, both lanes"),
        ("Decision windows", stats.get("both_decision_windows", stats.get("decision_windows", 0)),
         "retries collapsed, both lanes"),
        ("Trade-ready windows", stats.get("both_approved_windows", stats.get("approved_windows", 0)),
         "gate approved, both lanes"),
        ("Opening orders sent", stats.get("both_opening_orders", stats.get("submitted_windows", 0)),
         "the account's option entries"),
    ]
    top = max((v for _, v, _ in steps), default=0) or 1
    rows = []
    for label_text, value, sub in steps:
        pct = value / top * 100
        width = max(pct, 6)  # keep a sliver visible even at zero
        rows.append(
            f'<div class="fn-row"><span class="fn-label">{html_lib.escape(label_text)}</span>'
            f'<div class="fn-track"><div class="fn-bar" style="width:{width:.1f}%">'
            f'<span class="fn-val">{value:,}</span></div></div>'
            f'<span class="fn-sub">{html_lib.escape(sub)}</span></div>'
        )
    return f'<div class="funnel">{"".join(rows)}</div>'


def bars_svg(pairs: list[tuple[str, float, str]], unit: str = "$") -> str:
    """Simple labelled horizontal bar chart. pairs = (label, value, css_class)."""
    if not pairs:
        return ""
    span = max((abs(v) for _, v, _ in pairs), default=0) or 1
    rows = []
    for label_text, value, cls in pairs:
        width = abs(value) / span * 100
        shown = money(value) if unit == "$" else f"{value:g}"
        rows.append(
            f'<div class="cbar-row"><span class="cbar-label">{html_lib.escape(label_text)}</span>'
            f'<div class="cbar-track"><div class="cbar-fill {cls}" style="width:{width:.1f}%"></div></div>'
            f'<span class="cbar-val {cls}">{shown}</span></div>'
        )
    return f'<div class="cbars">{"".join(rows)}</div>'


def load_validation() -> dict:
    p = ROOT / "data" / "gate_validation.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def load_pnl(live: bool) -> dict:
    if not live:
        return {"error": "live fetch skipped (--no-live)"}
    try:
        from veto import pnl
        return pnl.summary()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def load_tools() -> dict:
    try:
        from veto import mcp_client
        return mcp_client.tool_counts()
    except Exception:
        return {}


def enforce_account_boundary(raw_pl: dict, live: bool) -> tuple[dict, str, str, bool, str | None]:
    """Suppress financial fields unless MCP proves the submission identity."""
    live_account = str(raw_pl.get("account_number") or "").strip()
    configured_account = config.ALPACA_ACCOUNT_ID.strip()
    account_match = bool(
        configured_account and live_account and configured_account == live_account
    )
    identity_error = None
    if live and not raw_pl.get("error"):
        if not configured_account:
            identity_error = "ALPACA_ACCOUNT_ID is not configured"
        elif not live_account:
            identity_error = "Alpaca MCP response omitted account_number"
        elif not account_match:
            identity_error = (
                f"MCP account {live_account} does not match configured submission "
                f"account {configured_account}"
            )
    pl = (
        {"error": identity_error, "account_number": live_account}
        if identity_error else raw_pl
    )
    return pl, live_account, configured_account, account_match, identity_error


def _rows(value: object, key: str) -> list[dict]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return [row for row in value[key] if isinstance(row, dict)]
    return []


def load_exit_evidence(live: bool, pl: dict) -> dict | None:
    """Join the policy's persisted exit reason to broker-owned order state."""
    state = risk_state.load()
    exit_state = state.get("last_exit")
    if not isinstance(exit_state, dict):
        return None

    key = risk_state.spread_key(exit_state)
    position_state = (state.get("positions") or {}).get(key) or {}
    result = {
        "action": exit_state.get("action"),
        "submitted_at": exit_state.get("submitted_at"),
        "client_order_id": exit_state.get("close_client_order_id"),
        "underlying": exit_state.get("underlying"),
        "short_symbol": exit_state.get("short_symbol"),
        "long_symbol": exit_state.get("long_symbol"),
        "qty": exit_state.get("qty"),
        "trigger_pnl": exit_state.get("exit_executable_pnl"),
        "high_water_pnl": (
            exit_state.get("ratchet_high_water_pnl")
            if exit_state.get("ratchet_high_water_pnl") is not None
            else position_state.get("high_water_pnl")
        ),
        "trailing_floor_pnl": (
            exit_state.get("ratchet_trailing_floor_pnl")
            if exit_state.get("ratchet_trailing_floor_pnl") is not None
            else position_state.get("trailing_floor")
        ),
        "breach_count": (
            exit_state.get("ratchet_breach_count")
            if exit_state.get("ratchet_breach_count") is not None
            else position_state.get("breach_count")
        ),
        "high_volatility": exit_state.get("pnl_volatility_high"),
        "broker_confirmed_flat": (
            pl.get("open_positions") == 0 if not pl.get("error") else None
        ),
        "open_account_positions": None,
        "broker_status": None,
        "filled_qty": None,
        "close_debit": None,
        "filled_at": None,
        "realized_gross_pnl": None,
        "broker_error": None,
    }
    if not live:
        return result

    try:
        from veto import mcp_client

        submitted_day = str(exit_state.get("submitted_at") or "")[:10]
        after = f"{submitted_day}T00:00:00-04:00" if submitted_day else None
        order_args = {"status": "all", "limit": 500}
        if after:
            order_args["after"] = after
        orders_result, positions_result = mcp_client.run(mcp_client.call_many([
            ("get_orders", order_args),
            ("get_all_positions", {}),
        ]))
        if isinstance(orders_result, dict) and orders_result.get("error"):
            raise RuntimeError(orders_result["error"])
        if isinstance(positions_result, dict) and positions_result.get("error"):
            raise RuntimeError(positions_result["error"])

        positions = _rows(positions_result, "positions")
        result["open_account_positions"] = len(positions)
        result["broker_confirmed_flat"] = len(positions) == 0
        client_id = exit_state.get("close_client_order_id")
        order = next(
            (row for row in _rows(orders_result, "orders")
             if row.get("client_order_id") == client_id),
            None,
        )
        if order:
            result.update({
                "broker_status": order.get("status"),
                "filled_qty": order.get("filled_qty"),
                "close_debit": order.get("filled_avg_price"),
                "filled_at": order.get("filled_at"),
            })
            try:
                qty = float(order.get("filled_qty") or 0)
                close_debit = float(order.get("filled_avg_price") or 0)
                max_profit = float(exit_state.get("max_profit") or 0)
                if qty > 0 and close_debit > 0 and max_profit:
                    result["realized_gross_pnl"] = round(
                        max_profit - close_debit * qty * 100, 2
                    )
            except (TypeError, ValueError):
                pass
    except Exception as exc:
        result["broker_error"] = f"{type(exc).__name__}: {exc}"
    return result


EXIT_LABELS = {
    "profit_ratchet": "Profit high-water ratchet",
    "profit_target": "50% opening-credit target",
    "stop_loss": "Defined-risk stop loss",
    "long_strike_breach": "Long-strike breach",
    "pin_risk": "Expiry pin-risk cleanup",
    "market_closed": "Market-close cleanup",
    "underlying_stop": "Underlying 2xATR stop",
    "max_hold_sessions": "Holding-session limit",
    "ema5_recovery": "EMA5 recovery",
    "issuer_concentration": "One position per issuer",
    "hold_ev_negative": "Holding lost positive expectancy",
    "budget_resize": "Trimmed to the lane's equity share",
}


def exit_block(exit_evidence: dict | None) -> str:
    if not exit_evidence:
        return ('<div class="note"><b>Latest position lifecycle.</b> '
                'No deterministic exit has been recorded yet.</div>')
    e = exit_evidence
    esc = lambda value: html_lib.escape(str(value if value is not None else "-"))
    action = EXIT_LABELS.get(str(e.get("action")), str(e.get("action") or "Unknown"))
    flat = e.get("broker_confirmed_flat")
    flat_text = "FLAT" if flat is True else ("OPEN" if flat is False else "UNVERIFIED")
    flat_cls = "pos" if flat is True else "neg"
    status = str(e.get("broker_status") or "unverified").upper()
    status_cls = "pos" if status == "FILLED" else "neg"
    close_debit = e.get("close_debit")
    close_debit_text = f"${float(close_debit):.2f}" if close_debit is not None else "-"
    locked = e.get("realized_gross_pnl")
    locked_cls = "neg" if isinstance(locked, (int, float)) and locked < 0 else "pos"
    # Name the position, so one closed trade is not read as the account result.
    locked_what = (
        f"{esc(e.get('underlying'))} "
        f"{str(e.get('short_symbol') or '')[-8:-3].lstrip('0')}/"
        f"{str(e.get('long_symbol') or '')[-8:-3].lstrip('0')} "
        f"x{esc(e.get('qty'))}, this position only"
        if e.get("long_symbol") else "this position only"
    )
    filled_qty = e.get("filled_qty") or "-"
    reason_detail = (
        f"Executable P&amp;L first reached a {money(e.get('high_water_pnl'))} high-water. "
        f"It then printed below the {money(e.get('trailing_floor_pnl'))} trailing floor "
        f"for {esc(e.get('breach_count'))} consecutive "
        f"{config.MONITOR_INTERVAL_SEC}-second observations. "
        f"The exit trigger saw {money(e.get('trigger_pnl'))}; money volatility was "
        f"{'high' if e.get('high_volatility') else 'normal'}, so volatility did not "
        f"independently cause the close."
    ) if e.get("action") == "profit_ratchet" else (
        f"Deterministic monitor action: <code>{esc(e.get('action'))}</code>."
    )
    broker_note = (
        f'<div class="note warn"><b>Broker reconciliation unavailable.</b> '
        f'{esc(e.get("broker_error"))}</div>'
        if e.get("broker_error") else ""
    )
    return f"""
<h2 style="font-size:14px;margin:26px 0 10px">Latest position lifecycle</h2>
<div class="tiles">
  <div class="tile"><div class="k">Exit reason</div><div class="v" style="font-size:18px">{esc(action)}</div>
    <div class="f">deterministic policy, not LLM timing</div></div>
  <div class="tile"><div class="k">Account position state</div><div class="v {flat_cls}">{flat_text}</div>
    <div class="f">{esc(e.get('open_account_positions'))} broker positions remain</div></div>
  <div class="tile"><div class="k">Closing order</div><div class="v {status_cls}">{status}</div>
    <div class="f">{esc(filled_qty)}/{esc(e.get('qty'))} spread contracts</div></div>
  <div class="tile"><div class="k">Closing debit</div><div class="v">{close_debit_text}</div>
    <div class="f">atomic two-leg average fill</div></div>
  <div class="tile"><div class="k">Gross locked P&amp;L</div>
    <div class="v {locked_cls}">{money(locked)}</div>
    <div class="f">{locked_what}, before broker fees</div></div>
  <div class="tile"><div class="k">Filled at</div><div class="v" style="font-size:14px">{esc(e.get('filled_at'))}</div>
    <div class="f">Alpaca broker timestamp</div></div>
</div>
<div class="note"><b>Why it closed.</b> {reason_detail}</div>
{broker_note}"""


def build(live: bool = True, output: Path | None = None,
          published: bool = False) -> Path:
    """Render the dashboard.

    ``published`` renders a frozen copy for static hosting. The runtime page
    refreshes itself every minute because it is watched on a desktop beside a
    running agent; served from GitHub Pages that meta-refresh throws away the
    reader's scroll position every minute, the reload button does nothing, and
    "auto-reload 60s" claims live data for numbers that stopped moving when the
    file was written. A published page says what it is instead: a snapshot,
    with the account it can be checked against.
    """
    destination = output or OUT
    if not destination.is_absolute():
        destination = ROOT / destination
    entries = ledger.load()
    summary = ledger.summary()
    selected_entries, presentation = presentation_entries(entries)
    val = load_validation()
    raw_pl = load_pnl(live)
    # Never mix judge-facing P&L with the wrong account's local evidence. Keep
    # only identity fields when the boundary fails so every financial tile is
    # visibly unavailable rather than plausibly stale.
    pl, live_account, configured_account, account_match, identity_error = (
        enforce_account_boundary(raw_pl, live)
    )
    tools = load_tools()
    exit_evidence = load_exit_evidence(live and account_match, pl)
    lifecycle_block = exit_block(exit_evidence)
    mr_log = mean_reversion.load_log()
    mr_decisions = [row for row in mr_log if row.get("kind") == "decision"]
    mr_state = mean_reversion.load_state()
    mr_managed = [
        row for row in (mr_state.get("positions") or {}).values()
        if isinstance(row, dict)
        and row.get("status") in {"entry_pending", "open", "exit_pending"}
    ]
    mr_latest = mr_decisions[-1] if mr_decisions else None
    built_at = datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    try:
        audit_rel = config.VERDICT_LOG.relative_to(ROOT).as_posix()
    except ValueError:
        # The ledger can be pointed outside the repository; link it as-is.
        audit_rel = config.VERDICT_LOG.as_posix()
    audit_href = "../" + audit_rel

    rows = [{"n": i, "label": label(r),
             "ts": r.get("ts"),
             "gate_version": r.get("gate_version"),
             "intent": r.get("intent") or {},
             "intent_error": r.get("intent_error"),
             "raw_reply_chars": r.get("raw_reply_chars", 0),
             "spread": resolved_spread(r),
             "verdict": r.get("verdict"),
             "executed": bool((r.get("execution") or {}).get("submitted"))}
            for i, r in selected_entries]
    payload = json.dumps({
        "rows": rows,
        "presentation": presentation,
        "tools": tools,
        "gates": list(gates.GATE_CATALOG),
        "ai_model": config.POE_MODEL,
    }, ensure_ascii=False).replace("</", "<\\/")

    perm = val.get("permutation_test") or {}
    pv = perm.get("p_value")
    pv_txt = f"{pv:.4f}" if pv is not None else "-"
    pv_cls = "pos" if (pv is not None and pv < 0.05) else "neg"

    pnl_err = pl.get("error")
    total_pnl = pl.get("total_pnl")
    account_daily_pnl = pl.get("account_daily_pnl")
    pnl_cls = "" if pnl_err else ("pos" if (account_daily_pnl or 0) > 0 else "neg")

    # Judge-facing account overview. The account number is verified against the
    # configured hackathon account so a stale key can't silently misreport it.
    account_display = live_account or configured_account or "-"
    banner_orders = pl.get("orders_submitted", 0) if not pnl_err else "-"
    account_guard_note = (
        '<div class="note"><b>Account boundary verified.</b> Alpaca MCP and '
        f'<code>ALPACA_ACCOUNT_ID</code> both identify '
        f'<code>{html_lib.escape(configured_account)}</code>. Runtime ledger, monitor '
        'state, and MCP telemetry are isolated under this account.</div>'
        if account_match else
        '<div class="note warn"><b>Account boundary failed.</b> Financial results are '
        'suppressed, and the entry agent plus exit monitor fail closed until the live MCP '
        'account exactly matches <code>ALPACA_ACCOUNT_ID</code>.</div>'
    )

    account_metrics = [
        ("Paper account", (
            f"{account_display} {'VERIFIED' if account_match else 'UNVERIFIED'}"
        )),
        ("Equity", money(pl.get("equity"))),
        ("Cash", money(pl.get("cash"))),
        ("Options buying power", money(pl.get("options_buying_power"))),
        ("Daily P&L", money(account_daily_pnl)),
        ("Agent activity", (
            f"{pl.get('orders_submitted', 0)} option entries / {pl.get('fills', 0)} broker fills"
            if not pnl_err else "-"
        )),
        ("Last equity (prior close)", money(pl.get("last_equity"))),
    ]
    account_rows_html = "".join(
        f"<tr><td>{html_lib.escape(metric)}</td><td>{html_lib.escape(value)}</td></tr>"
        for metric, value in account_metrics
    )

    position_rows_html = ""
    for position in pl.get("positions") or []:
        position_qty = f"{float(position.get('qty') or 0):g}"
        position_rows_html += (
            f"<tr><td><code>{html_lib.escape(str(position.get('symbol') or '-'))}</code></td>"
            f"<td>{html_lib.escape(position_qty)}</td>"
            f"<td>{money(position.get('avg_entry'))}</td>"
            f"<td>{money(position.get('market_value'))}</td>"
            f"<td class={'pos' if (position.get('unrealized_pl') or 0) >= 0 else 'neg'}>"
            f"{money(position.get('unrealized_pl'))}</td></tr>"
        )
    if not position_rows_html:
        position_rows_html = (
            '<tr><td colspan="5" style="color:var(--muted);font-weight:400">'
            'Account is flat — no open positions reported by Alpaca MCP.</td></tr>'
        )

    # Closed round trips, FIFO-matched from broker fills by veto.pnl. Legs of a
    # credit spread appear separately because that is how the broker fills and
    # reports them; the note under the table says so rather than netting them
    # here, which would hide a leg.
    closed_rows_html = ""
    closed_total = 0.0
    for trip in pl.get("closed_round_trips") or []:
        realized = trip.get("realized") or 0.0
        closed_total += realized
        closed_rows_html += (
            f"<tr><td><code>{html_lib.escape(str(trip.get('symbol') or '-'))}</code></td>"
            f"<td>{html_lib.escape(str(trip.get('side') or '-'))}</td>"
            f"<td>{float(trip.get('qty') or 0):g}</td>"
            f"<td>{money(trip.get('avg_entry'))}</td>"
            f"<td>{money(trip.get('avg_exit'))}</td>"
            f"<td class={'pos' if realized >= 0 else 'neg'}>{money(realized)}</td>"
            f"<td>{html_lib.escape(overview_time(trip.get('closed_ts')))}</td></tr>"
        )
    closed_note = ""
    if closed_rows_html:
        closed_note = (
            '<div class="note">FIFO-matched from broker fills; the column sums to '
            f'{money(round(closed_total, 2))}, the realized P&amp;L above. Each leg of a '
            'credit spread is listed separately because the broker fills and reports '
            'them that way, and netting them here would hide one.</div>'
        )
    else:
        closed_rows_html = (
            '<tr><td colspan="7" style="color:var(--muted);font-weight:400">'
            'No completed round trips yet.</td></tr>'
        )

    overview_entries = {number: row for number, row in selected_entries[:6]}
    submitted_entries = [
        (number, row) for number, row in selected_entries
        if (row.get("execution") or {}).get("submitted")
    ][:2]
    for number, row in submitted_entries:
        overview_entries[number] = row

    decision_rows_html = ""
    for number, row in sorted(overview_entries.items(), reverse=True):
        verdict = row.get("verdict") or {}
        checks = verdict.get("checks") or []
        passed = sum(bool(check.get("passed")) for check in checks)
        failed_labels = [
            str(check.get("name") or "unknown").replace("_", " ")
            for check in checks if not check.get("passed")
        ]
        submitted = bool((row.get("execution") or {}).get("submitted"))
        approved = bool(verdict.get("approved"))
        if submitted:
            action, badge_class = "SUBMITTED", "ok"
        elif approved:
            action, badge_class = "APPROVED", "ok"
        elif verdict:
            action, badge_class = "VETOED", "no"
        else:
            action, badge_class = "INVALID", "no"
        thesis = str((row.get("intent") or {}).get("thesis") or "").strip()
        if approved:
            rationale = thesis or "All recorded gates passed."
        elif verdict:
            rationale = str(verdict.get("reason") or "Policy vetoed the proposal")
        else:
            rationale = str(row.get("intent_error") or "Intent was not scored")
        gate_text = f"{passed}/{len(gates.GATE_CATALOG)}"
        if failed_labels:
            gate_text += f" · {', '.join(failed_labels)}"
        decision_rows_html += (
            f"<tr><td>{overview_time(row.get('ts'))} ET</td>"
            f"<td>{html_lib.escape(label(row))}</td>"
            f"<td><span class=\"badge {badge_class}\">{action}</span></td>"
            f"<td class=\"decision-why\">{html_lib.escape(rationale)}</td>"
            f"<td>{gate_text}</td></tr>"
        )
    for row in reversed(mr_decisions[-3:]):
        candidate = row.get("candidate") or {}
        checks = row.get("checks") or []
        passed = sum(bool(check.get("passed")) for check in checks)
        status = str(row.get("status") or "UNKNOWN").upper()
        badge_class = "ok" if status == "SUBMITTED" else "no"
        rationale = (
            str((row.get("ai_review") or {}).get("thesis") or "").strip()
            or str(row.get("reason") or "deterministic scan completed")
        )
        contract = row.get("contract") or {}
        candidate_text = (
            f"{candidate.get('symbol')} long call · RSI(2) "
            f"{float(candidate.get('rsi2') or 0):.2f} · "
            f"{contract.get('dte', '-')} DTE"
            if candidate else "NDX30 options mean-reversion scan"
        )
        decision_rows_html = (
            f"<tr><td>{overview_time(row.get('ts'))} ET</td>"
            f"<td>{html_lib.escape(candidate_text)}</td>"
            f"<td><span class=\"badge {badge_class}\">{html_lib.escape(status)}</span></td>"
            f"<td class=\"decision-why\">{html_lib.escape(rationale)}</td>"
            f"<td>{passed}/{len(checks)} · MR policy</td></tr>"
            + decision_rows_html
        )
    if not decision_rows_html:
        decision_rows_html = (
            '<tr><td colspan="5" style="color:var(--muted);font-weight:400">'
            'No decision windows recorded yet.</td></tr>'
        )

    mr_latest_status = str((mr_latest or {}).get("status") or "NOT RUN")
    mr_latest_reason = str(
        (mr_latest or {}).get("reason")
        or "awaiting the next 15:45 ET regular-session scan"
    )
    mr_windows = len(mr_decisions)
    mr_approved = sum(1 for row in mr_decisions if row.get("status") == "SUBMITTED")
    second_strategy_block = f"""
<h3 style="font-size:14px;margin:24px 0 8px">Second options strategy · NDX30_CALL_MR_01</h3>
<div class="tiles">
  <div class="tile"><div class="k">Staging status</div><div class="v" style="font-size:18px">{html_lib.escape(mr_latest_status)}</div>
    <div class="f">{html_lib.escape(mr_latest_reason)}</div></div>
  <div class="tile"><div class="k">Managed long calls</div><div class="v">{len(mr_managed)}</div>
    <div class="f">maximum {config.OPTION_MR_MAX_POSITIONS}; {config.OPTION_MR_MAX_ENTRIES_PER_DAY} new entries/day</div></div>
  <div class="tile"><div class="k">Decision windows</div><div class="v">{mr_windows}</div>
    <div class="f">{mr_approved} produced an entry</div></div>
  <div class="tile"><div class="k">Options contract</div><div class="v" style="font-size:18px">{config.OPTION_MR_DTE_MIN}-{config.OPTION_MR_DTE_MAX} DTE call</div>
    <div class="f">target delta {config.OPTION_MR_DELTA_TARGET:.2f} · bid/ask cap {config.OPTION_MR_MAX_SPREAD_PCT:.0%}</div></div>
  <div class="tile"><div class="k">Frozen signal</div><div class="v" style="font-size:18px">RSI(2) &lt; {config.OPTION_MR_RSI_MAX:g}</div>
    <div class="f">price &gt; rising SMA200 · {len(config.OPTION_MR_DECISION_WINDOWS)} scans/session</div></div>
</div>
<div class="note"><b>Options-only execution.</b> Python scans {len(config.OPTION_MR_UNIVERSE)}
liquid Nasdaq underlyings, resolves one real OCC long call from Alpaca, sizes it under the
active <code>{config.OPTION_MR_SIZING_MODE}</code> objective - a
{config.OPTION_MR_MAX_PREMIUM_PCT:.0%} premium cap per position inside a
{config.OPTION_MR_TOTAL_PREMIUM_PCT:.0%} lane budget, with the modelled 2xATR stop loss capped
at {config.OPTION_MR_MAX_STOP_RISK_PCT:.0%} of equity - and submits only
<code>place_option_order</code> limit orders. No stock order exists in the strategy.</div>
<div class="note warn"><b>Evidence boundary.</b> No historical option returns stand behind
this lane. Every figure shown for it is read from the live paper account above; IV, theta and
option bid/ask effects are measurable only forward, and nothing modelled is mixed into the
account&#39;s P&amp;L.</div>"""

    # --- Charts (proposal-style: more graph, less prose) ---------------------
    # The funnel described the credit-spread lane only, so its last bar could
    # not be reconciled with an account that also holds long calls. Fold the
    # second lane in, and end on the number the account itself reports.

    # Only a candidate that survives every deterministic gate is shown to the
    # model, so these are this lane's model calls.
    mr_ai_calls = sum(
        1 for row in mr_decisions
        if row.get("status") == "SUBMITTED" or row.get("reason") == "ai_event_risk"
    )
    presentation = {
        **presentation,
        "mr_decision_windows": mr_windows,
        "mr_approved_windows": mr_approved,
        "both_raw_attempts": presentation["raw_attempts"] + mr_ai_calls,
        "both_decision_windows": presentation["decision_windows"] + mr_windows,
        "both_approved_windows": presentation["approved_windows"] + mr_approved,
        "both_opening_orders": pl.get("orders_submitted") or 0,
        "closing_orders": pl.get("closing_orders") or 0,
        "mr_ai_calls_display": mr_ai_calls,
    }
    # Quoted in the risk disclosure, so it cannot drift from the sizing.
    spread_lane_budget = sizing.spread_budget(
        pl.get('options_buying_power') or 0.0, pl.get('equity') or 0.0,
        config.SPREAD_EQUITY_PCT)
    funnel_chart = funnel_svg(presentation)

    # Predicted gate EV vs. what the account actually realized.
    ev_pairs = []
    if not pnl_err:
        pev = pl.get("predicted_ev_of_approved")
        realized = pl.get("realized_pnl")
        acct_daily = pl.get("account_daily_pnl")
        if pev is not None:
            ev_pairs.append(("Predicted EV (approved)", pev, "pos" if pev >= 0 else "neg"))
        if realized is not None:
            ev_pairs.append(("Realized strategy P&L", realized, "pos" if realized >= 0 else "neg"))
        if acct_daily is not None:
            ev_pairs.append(("Account daily P&L", acct_daily, "pos" if acct_daily >= 0 else "neg"))
    ev_chart = bars_svg(ev_pairs) if ev_pairs else ""

    # Counterfactual: what each policy earned in the 12k-trial validation.
    policy_pairs = []
    te_all = val.get("total_pnl_if_traded_everything")
    op_only = val.get("total_pnl_operational_gate_only")
    full_gate = val.get("total_pnl_gate_approved_only")
    if te_all is not None:
        policy_pairs.append(("Trade everything", te_all, "mut"))
    if op_only is not None:
        policy_pairs.append(("Operational gates only", op_only, "pos" if op_only >= 0 else "neg"))
    if full_gate is not None:
        policy_pairs.append(("Full economic gate", full_gate, "pos" if full_gate >= 0 else "neg"))
    policy_chart = bars_svg(policy_pairs) if policy_pairs else ""
    if pnl_err:
        error_heading = (
            "Account verification failed" if identity_error
            else "Paper account not reachable"
        )
        pnl_block = (
            f'<div class="note warn"><b>{error_heading}.</b> '
            f'{html_lib.escape(str(pnl_err))}. Judge-facing P&amp;L remains suppressed '
            f'until the configured paper account is verified through Alpaca MCP.</div>'
        )
    else:
        pnl_block = (f'<div class="note"><b>Predicted vs actual.</b> The gate forecast '
                     f'{money(pl.get("predicted_ev_of_approved"))} of expected value across '
                     f'{pl.get("gate_approved", 0)} approved option proposals. Fill-derived strategy '
                     f'P&amp;L: {money(pl.get("realized_pnl"))}; account daily P&amp;L: '
                     f'{money(pl.get("account_daily_pnl"))}. The difference is left visible '
                     f'for broker reconciliation. A short window is dominated by variance - '
                     f'this is a reconciliation, not evidence of edge.</div>')

    if config.MONITOR_PROFIT_EXIT_ENABLED:
        gate9_title = "Profit high-water"
        gate9_body = (
            f"Executable P&amp;L arms a persistent trail after "
            f"{config.MONITOR_RATCHET_ARM_PCT:.0%} credit capture. Two confirmed givebacks "
            f"close at {config.MONITOR_RATCHET_GIVEBACK_PCT:.0%}; high money volatility "
            f"tightens the trail to {config.MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT:.0%} but "
            f"cannot exit alone, and dispersion inside 1.5 ticks per contract is quote "
            f"noise rather than volatility."
        )
    else:
        gate9_title = "Hold to expiry"
        gate9_body = (
            "The entry gate prices the spread on its terminal payoff, so the monitor "
            "delivers that payoff: no profit target and no trail. The spread is held "
            "to expiry and closed at market only if the underlying crosses the long "
            "strike; the final-30-minute pin-risk exit still applies. Disabled profit "
            "exits are recorded as <code>hold_to_expiry</code> in the session log."
        )

    if published:
        refresh_meta = ""
        refresh_button = (
            '<div class="note">This is a frozen snapshot, not a live feed. Every figure '
            f'above is read-only broker state for Alpaca paper account '
            f'<code>{config.ALPACA_ACCOUNT_ID}</code> at the timestamp shown, and can be '
            'verified against that account directly.</div>'
        )
        snapshot_line = f"Snapshot taken {built_at} ET · published copy, not auto-updating"
    else:
        refresh_meta = (
            f'<meta http-equiv="refresh" content="{config.DASHBOARD_REFRESH_INTERVAL_SEC}">'
        )
        refresh_button = (
            '<button class="refresh-btn" type="button" onclick="window.location.reload()">'
            'Reload latest broker snapshot</button>'
        )
        snapshot_line = (
            f"Snapshot generated {built_at} ET · "
            f"auto-reload {config.DASHBOARD_REFRESH_INTERVAL_SEC}s"
        )

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh_meta}
<title>PacaPounce - Patient AI Trading Agent</title><style>{CSS}</style></head><body><div class="wrap">

<h1>PacaPounce</h1>
<p class="sub" style="margin-bottom:8px"><b>Team a-meowmeow</b> · The patient AI trading agent</p>
<p class="sub">AI hunts. Alpaca MCP verifies. PacaPounce trades only when the opportunity survives.
The LLM is creative but untrusted: it may suggest any thesis, but only deterministic,
independently validated policy is allowed to touch the broker. Gate {summary.get('gate_version')}.</p>

<section aria-labelledby="live-overview">
<div class="overview-head"><div><h2 id="live-overview">Live account overview</h2>
<p>Read-only broker state and the newest bounded decision windows.</p></div>
<p>{snapshot_line}</p></div>
{refresh_button}

<h3 style="font-size:14px;margin:0 0 8px">Account</h3>
<div class="simple-table"><table><thead><tr><th>Metric</th><th>Value</th></tr></thead>
<tbody>{account_rows_html}</tbody></table></div>
{account_guard_note}

<h3 style="font-size:14px;margin:24px 0 8px">Open positions</h3>
<div class="simple-table"><table><thead><tr><th>Asset</th><th>Qty</th>
<th>Avg entry</th><th>Market value</th><th>Unrealized P&amp;L</th></tr></thead>
<tbody>{position_rows_html}</tbody></table></div>

<h3 style="font-size:14px;margin:24px 0 8px">Closed positions</h3>
<div class="simple-table"><table><thead><tr><th>Asset</th><th>Side</th><th>Qty</th>
<th>Avg entry</th><th>Avg exit</th><th>Realized P&amp;L</th><th>Closed (ET)</th></tr></thead>
<tbody>{closed_rows_html}</tbody></table></div>
{closed_note}

<div class="overview-head"><div><h2>Decision log — what the agent did and why</h2>
<p>Newest six bounded windows plus submitted decisions; retries are collapsed.</p></div>
<p>{presentation['approved_windows']} approved · {presentation['blocked_windows']} blocked</p></div>
<div class="simple-table"><table><thead><tr><th>Time</th><th>Candidate</th><th>Decision</th>
<th>Rationale</th><th>Gates</th></tr></thead><tbody>{decision_rows_html}</tbody></table></div>
{second_strategy_block}
</section>

<div class="deep-dive">

<section>
<div class="crit"><span class="n">HOW</span><h2>How AI intelligence becomes a paper trade</h2>
<span class="why">AI proposes; deterministic policy disposes</span></div>
<div class="flow">
  <div class="flow-step"><span class="step">01 · ALPACA MCP</span><h3>Session permission</h3>
    <p>Account identity, competition date, clock, orders, positions, and authorization
    decide whether a new AI proposal is permitted. A broker-side block prevents the Poe call.</p></div>
  <div class="flow-step"><span class="step">02 · AI</span><h3>Strategy intent</h3>
    <p>{config.POE_MODEL} reads an Alpaca MCP brief with 1D/5D returns, RV20,
    nearest-ATM IV, IV/RV, and quote time, then returns direction, strategy,
    DTE, target delta, width, thesis, and invalidation. Missing data stays missing.</p></div>
  <div class="flow-step"><span class="step">03 · VALIDATE</span><h3>Strict intent schema</h3>
    <p>Unknown fields, unsupported structures, and incoherent values are rejected before
    a contract is priced.</p></div>
  <div class="flow-step"><span class="step">04 · ALPACA MCP</span><h3>Resolve contracts</h3>
    <p>Live chain, Greeks, OPRA bid/ask, bars, account, and positions determine the exact
    contracts and executable economics.</p></div>
  <div class="flow-step"><span class="step">05 · POLICY</span><h3>{len(gates.GATE_CATALOG)} ordered gates</h3>
    <p>Every current gate runs without short-circuiting. One failure vetoes the entire
    proposal and all reasons remain visible.</p></div>
  <div class="flow-step"><span class="step">06 · PAPER ONLY</span><h3>Execute and monitor</h3>
    <p><code>run.py --loop</code> owns both entry hunting and the deterministic risk monitor,
    including a restart-safe executable-profit ratchet, volatility response, re-entry
    lifecycle, and close-of-session cleanup.</p></div>
</div>
<div class="note"><b>Authority boundary.</b> AI decides <em>what idea to investigate and why</em>.
It never chooses an OCC symbol, trusts a price, calculates quantity, or sends an order.
Alpaca data plus versioned policy decide the exact contract, whether it may trade, and how it exits.</div>
<h2 style="font-size:14px;margin:26px 0 6px">Proposes freely, trades only what survives</h2>
<p class="sub" style="margin-bottom:2px"><b>Signal, not retry noise.</b> {presentation['both_raw_attempts']:,} model
calls collapse into {presentation['both_decision_windows']:,} decision windows, and the funnel shows how
many survive each stage to reach the broker. These bars count <b>both lanes</b>:
{presentation['raw_attempts']:,} spread-lane model calls plus {presentation['mr_ai_calls_display']} long-call
news reviews, over {presentation['decision_windows']:,} spread windows and
{presentation['mr_decision_windows']} long-call windows. The last bar is opening orders, and it is the
same {presentation['both_opening_orders']} the account reports as option entries above. Those positions
were then closed by {presentation['closing_orders']} closing orders, for
{presentation['both_opening_orders'] + presentation['closing_orders']} orders in all; the account's
fill count is larger because it counts legs, and a spread fills two legs per order.
The full per-decision thesis, economics, and gate
audit live in the <b>Presentation &amp; Execution</b> table below and in
<a href="{html_lib.escape(audit_href)}"><code>{html_lib.escape(audit_rel)}</code></a>.</p>
{funnel_chart}
</section>

<section>
<div class="crit"><span class="n">01</span><h2>P&amp;L Performance</h2>
<span class="why">Alpaca paper account, live</span></div>
<div class="tiles">
  <div class="tile"><div class="k">Account daily P&amp;L</div>
    <div class="v {pnl_cls}">{money(account_daily_pnl)}</div>
    <div class="f">equity minus last equity</div></div>
  <div class="tile"><div class="k">Strategy P&amp;L (realized + open)</div>
    <div class="v">{money(total_pnl)}</div>
    <div class="f">{money(pl.get('realized_pnl'))} realized, {money(pl.get('unrealized_pnl'))} open;
    fill-derived from {pl.get('fills', 0)} broker fills</div></div>
  <div class="tile"><div class="k">Open P&amp;L</div><div class="v">{money(pl.get('unrealized_pnl'))}</div>
    <div class="f">{pl.get('open_positions', 0)} positions</div></div>
  <div class="tile"><div class="k">Equity</div><div class="v">{money(pl.get('equity'), 0)}</div>
    <div class="f">options approved L{pl.get('options_approved_level', '-')} / enabled L{pl.get('options_level', '-')}</div></div>
  <div class="tile"><div class="k">Options buying power</div>
    <div class="v">{money(pl.get('options_buying_power'), 0)}</div>
    <div class="f">the binding budget; options are authorised separately from margin</div></div>
  <div class="tile"><div class="k">Orders sent</div><div class="v">{banner_orders}</div>
    <div class="f">both lanes; {summary.get('approved', 0)} of them spread-lane gate approvals</div></div>
</div>
<div class="note"><b>Reconciling the two P&amp;L figures.</b> The tile above is
realized plus open ({money(pl.get('realized_pnl'))} {signed(pl.get('unrealized_pnl'))} =
{money(total_pnl)}). The chart below compares the gate's forecast against
<em>realized</em> P&amp;L only, because an open position has not settled its
forecast yet. Account daily P&amp;L is a third number and a wider one: it is
equity minus the prior close, so it also moves with positions this agent opened
on earlier sessions.</div>
<h2 style="font-size:14px;margin:26px 0 4px">Did the gate's forecast hold up?</h2>
<p class="sub" style="margin-bottom:2px">What the gate <em>predicted</em> across approved trades,
next to what the account actually realized. A gate that forecasts well keeps these bars close.</p>
{ev_chart if ev_chart else '<div class="note">Predicted-vs-actual populates once the live account has approved fills.</div>'}
{pnl_block}
{lifecycle_block}
<div class="note warn"><b>Tournament risk disclosure.</b> The configured
<code>{config.SIZING_MODE}</code> objective sizes each lane from its own share of equity
rather than from a prudent risk budget. The credit-spread lane may carry
{config.SPREAD_EQUITY_PCT:.0%} of equity as defined maximum loss
({money(spread_lane_budget, 0)} at the equity above). The long-call lane may hold up to
{config.OPTION_MR_TOTAL_PREMIUM_PCT:.0%} of equity in premium, and for a long option the
premium paid <em>is</em> the maximum loss; one position's modelled 2xATR stop is capped at
{config.OPTION_MR_MAX_STOP_RISK_PCT:.0%} of equity. Both lanes can be open at once, so a bad
week can consume a large share of the account.</div>
</section>

<section>
<div class="crit"><span class="n">02</span><h2>Technology Implementation</h2>
<span class="why">Official Alpaca MCP server, stdio</span></div>
<div class="tiles" id="tl"></div>
<h2 style="font-size:14px;margin:26px 0 4px">MCP session supervisor · policy {config.SESSION_POLICY_VERSION}</h2>
<p class="sub" style="margin-bottom:10px">Before Poe is called, the entry loop reconstructs
permission from Alpaca. No daily trade counter or pending-order lock lives only in process memory.</p>
<div class="gate-grid">
  <div class="gate-card"><div class="top"><span class="seq">A</span><h3>Account identity</h3>
    <span class="layer">MCP</span></div><p><code>get_account_info.account_number</code> must
    exactly match <code>ALPACA_ACCOUNT_ID</code>. Entry, monitor mutation, and financial
    presentation fail closed on mismatch; runtime evidence is account-scoped.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">B</span><h3>Regime-grounded AI</h3>
    <span class="layer">MCP + AI</span></div><p><code>get_stock_latest_quote</code>,
    <code>get_stock_bars</code>, <code>get_option_contracts</code>, and
    <code>get_option_snapshot</code> create a timestamped brief of spot, 1D/5D returns,
    RV20, nearest-ATM IV, and IV/RV. It is cached for {config.REGIME_CACHE_SEC}s;
    unavailable VIX or IV rank is never fabricated.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">1</span><h3>Trading session</h3>
    <span class="layer">MCP</span></div><p><code>get_clock</code> + <code>get_calendar</code>
    pause outside the regular session, including shortened days. MCP <code>next_open</code>
    rolls the same process across nights, holidays, and weekends.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">2</span><h3>Pending-order lock</h3>
    <span class="layer">MCP</span></div><p><code>get_orders(status=open)</code> blocks a new
    proposal while any opening or closing multi-leg order is unresolved.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">3</span><h3>Restart-safe count</h3>
    <span class="layer">MCP</span></div><p><code>get_orders(status=all)</code> groups parent
    PacaPounce entries and chase revisions; <code>get_account_activities(FILL)</code> corroborates
    child-leg execution without counting two legs as two trades.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">4</span><h3>Live exposure</h3>
    <span class="layer">MCP</span></div><p><code>get_all_positions</code> reconstructs option
    spreads and held symbols instead of trusting a local ledger.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">5</span><h3>Options authorization</h3>
    <span class="layer">MCP</span></div><p><code>get_account_info</code> must report ACTIVE,
    unblocked, approved and enabled at Level 3+, with positive options buying power.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">6</span><h3>Full-capital sizing</h3>
    <span class="layer">MCP</span></div><p>The largest integer quantity whose complete
    defined loss fits inside live <code>options_buying_power</code> is computed, gated,
    and checked again immediately before submission.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">7</span><h3>Full-capital wait</h3>
    <span class="layer">MCP</span></div><p>Once a full-buying-power spread is confirmed
    open, entry hunting pauses before Poe is called while risk monitoring continues.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">8</span><h3>Fill-aware repricing</h3>
    <span class="layer">MCP + POLICY</span></div><p>An unfilled opening order improves by at
    most one cent per monitor cycle, never below the live natural credit. Before replacement,
    its reserved defined loss is released in preflight; after cancellation,
    <code>get_account_info</code> must confirm enough options BP. EV is recomputed at the
    actual replacement limit, without charging the bid/ask concession twice. An MCP
    <code>accepted</code> envelope is not success: <code>get_orders(status=all)</code> must
    return the same client ID and an Alpaca broker order ID.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">9</span><h3>{gate9_title}</h3>
    <span class="layer">POLICY</span></div><p>{gate9_body}</p></div>
  <div class="gate-card"><div class="top"><span class="seq">10</span><h3>Better re-entry</h3>
    <span class="layer">MCP + POLICY</span></div><p>Profit exits wait
    {config.REENTRY_COOLDOWN_MIN} minutes plus {config.REENTRY_STABLE_MIN} calm, liquid minutes.
    A new pair must improve post-cost EV/risk by {config.REENTRY_MIN_QUALITY_MULTIPLIER - 1:.0%},
    worsen no risk dimension, and uses {config.REENTRY_BP_UTILIZATION:.0%} of options BP.
    Risk exits lock the session.</p></div>
</div>
<h2 style="font-size:14px;margin:26px 0 4px">Second-strategy MCP lane</h2>
<div class="gate-grid">
  <div class="gate-card"><div class="top"><span class="seq">S1</span><h3>One bounded scan</h3>
    <span class="layer">MCP + QUANT</span></div><p><code>get_stock_bars</code> supplies adjusted
    daily and completed 15-minute SIP bars. Python computes SMA200, Wilder RSI(2), EMA5,
    ATR14, ranking, quantity, and stop exactly once at 15:45 ET on normal sessions.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">S2</span><h3>Auditable event review</h3>
    <span class="layer">MCP + AI</span></div><p><code>get_news</code> supplies timestamped articles
    for the selected numerical candidate. Poe may veto concrete event risk and write the thesis.
    The feed is not mislabeled as a verified earnings calendar.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">S3</span><h3>Exact long-call execution</h3>
    <span class="layer">MCP</span></div><p><code>get_option_chain</code> resolves a liquid
    {config.OPTION_MR_DTE_MIN}-{config.OPTION_MR_DTE_MAX} DTE call near
    {config.OPTION_MR_DELTA_TARGET:.2f} delta. <code>place_option_order</code> submits a
    buy-to-open limit; <code>get_orders</code> must confirm it before SUBMITTED.</p></div>
  <div class="gate-card"><div class="top"><span class="seq">S4</span><h3>Deterministic lifecycle</h3>
    <span class="layer">MCP + POLICY</span></div><p>The 30-second monitor reconciles broker
    option positions, checks the underlying 2×ATR14 stop, and sends a sell-to-close
    limit on stop, EMA5 recovery, or session three. The LLM never times exits.</p></div>
</div>
</section>

<section>
<div class="crit"><span class="n">03</span><h2>Creativity &amp; Originality</h2>
<span class="why">The gate, and its validation</span></div>
<div class="tiles">
  <div class="tile"><div class="k">Decision windows</div><div class="v">{presentation['decision_windows']}</div>
    <div class="f">independent hunt cycles</div></div>
  <div class="tile"><div class="k">Trade-ready windows</div><div class="v">{presentation['approved_windows']}</div>
    <div class="f">at least one approved result</div></div>
  <div class="tile"><div class="k">Blocked windows</div><div class="v">{presentation['blocked_windows']}</div>
    <div class="f">policy preserved capital</div></div>
  <div class="tile"><div class="k">Raw AI attempts</div><div class="v">{presentation['raw_attempts']}</div>
    <div class="f">includes {presentation['collapsed_retries']} bounded revisions</div></div>
  <div class="tile"><div class="k">Validation p-value</div>
    <div class="v {pv_cls}">{pv_txt}</div>
    <div class="f">{val.get('trials', 0):,} trials, permutation</div></div>
</div>
<h2 style="font-size:14px;margin:26px 0 4px">Current entry stack: {len(gates.GATE_CATALOG)} gates, one by one</h2>
<p class="sub" style="margin-bottom:10px">Intent parsing, coherence validation, and contract
construction happen first. A fully resolved candidate then passes through every gate below in
this exact order; evaluation never stops at the first failure.</p>
<div class="gate-grid" id="gate-pipeline"></div>
<div class="note"><b>Version transparency.</b> The current stack is gate
{summary.get('gate_version')}. Historical live entries were recorded under gate 1.0.0 with 12
checks; 1.1.0 added the annual-target budget, and 1.2.0 added broker-owned options eligibility
plus options-buying-power sizing. Gate 1.3.0 added symmetric call-tail economics and the
call-rebound guard. Historical detail marks later checks <em>not recorded</em>
instead of retroactively claiming they ran.</div>
<div class="note"><b>The gate was tested against outcomes, not asserted.</b>
The harness scores the trades the gate <em>rejected</em> as well as the ones it approved -
the counterfactual every naive version skips. Across {val.get('trials', 0):,} independent
opportunities, approved trades averaged {money(perm.get('mean_approved'))} and vetoed trades
{money(perm.get('mean_vetoed'))}, a separation of {money(perm.get('observed_separation'))}
per trade at p={pv_txt}. Reproduce with
<code>python scripts/validate_gate.py --trials {val.get('trials', 12000)}</code>.</div>
<h2 style="font-size:14px;margin:26px 0 4px">Why an economic gate at all</h2>
<p class="sub" style="margin-bottom:2px">Total P&amp;L by policy across the same
{val.get('trials', 0):,} simulated opportunities. Operational rules alone (defined risk, size caps,
liquidity, limit orders) prevent malformed trades, not unprofitable ones — they can score
<em>worse</em> than no filter because they reject trades for being large, not underpaid.</p>
{policy_chart if policy_chart else ''}
<div class="note">Option delta is the market's own estimate of P(finishing ITM); comparing it
against the payoff on offer, net of measured bid/ask friction, requires no forecast at all.</div>
<h2 style="font-size:14px;margin:26px 0 10px">Why decision windows were blocked</h2>
<p class="sub" style="margin-bottom:10px">One representative result per window prevents five
revisions of the same opportunity from masquerading as five independent vetoes.</p>
<div class="tiles" id="vr"></div>
</section>

<section>
<div class="crit"><span class="n">04</span><h2>Presentation &amp; Execution</h2>
<span class="why">Executions plus recent decision evidence</span></div>
<div class="scroll full"><table><thead><tr>
<th>#</th><th>Verdict</th><th>Trade</th><th class="num">Credit</th><th class="num">Max loss</th>
<th class="num">Breakeven WR</th><th class="num">Implied WR</th><th class="num">Edge</th>
<th class="num">Friction</th><th class="num">EV net</th><th>AI rationale</th><th>Gate audit</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<div class="note"><b>Presentation is bounded; audit is complete.</b> Showing
{presentation['shown_rows']} of {presentation['raw_attempts']} raw rows: <b>every</b> approved or
submitted result, plus the newest {presentation['recent_window_limit']} decision windows at one
row each. Selection is by recency, not by sampling, so approvals are over-represented in this
table by design - {presentation['approved_windows']} of {presentation['shown_rows']} rows here
against {presentation['approved_windows']} of {presentation['decision_windows']} windows overall.
The funnel and the rejection breakdown above are computed over all
{presentation['decision_windows']} windows, not over these rows.
{presentation['suppressed_rows']} older or retry-level rows are omitted from this page. They are
not deleted: the full append-only record, including every gate result and gate version, remains in
<a href="{html_lib.escape(audit_href)}"><code>{html_lib.escape(audit_rel)}</code></a>.</div>
</section>

</div>
</div><script>const DATA={payload};{JS}</script></body></html>"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, destination)
    return destination


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true", help="skip the Alpaca P&L fetch")
    ap.add_argument("--watch", action="store_true", help="rebuild until interrupted")
    ap.add_argument(
        "--published", action="store_true",
        help="frozen copy for static hosting: no meta-refresh, no reload button",
    )
    ap.add_argument(
        "--output", type=Path,
        help="output path; default dashboard/runtime/index.html is gitignored",
    )
    ap.add_argument(
        "--interval", type=int, default=config.DASHBOARD_REFRESH_INTERVAL_SEC,
        help="watch rebuild interval in seconds",
    )
    a = ap.parse_args()
    interval = max(10, a.interval)
    try:
        while True:
            p = build(live=not a.no_live, output=a.output, published=a.published)
            print(f"wrote {p} ({p.stat().st_size:,} bytes)", flush=True)
            if not a.watch:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
