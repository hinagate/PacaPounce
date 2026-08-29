"""Configuration - all runtime knobs, loaded from .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
# Deliberately do not load a parent-directory .env. PacaPounce owns its
# credentials and policy locally so another project cannot change its account,
# model, feed, or risk settings implicitly.

# ── LLM (Poe, OpenAI-compatible) ─────────────────────────────────────────────
POE_KEY = os.getenv("POE_KEY", "")
POE_MODEL = os.getenv("POE_MODEL", "gemini-3.7-flash")
POE_BASE = "https://api.poe.com/v1"
# gemini-3.7-flash is a reasoning model: hidden reasoning tokens are billed
# against max_tokens. Anything under ~500 can return empty content.
LLM_MAX_TOKENS = int(os.getenv("PACAPOUNCE_LLM_MAX_TOKENS", "4000"))

# ── Alpaca ───────────────────────────────────────────────────────────────────
# Paper keys are SEPARATE from live keys - generate them in the paper dashboard.
ALPACA_KEY = os.getenv("ALPACA_PAPER_KEY") or os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_PAPER_SECRET") or os.getenv("ALPACA_SECRET_KEY", "")
TRADE_BASE = "https://paper-api.alpaca.markets"   # paper only, by design
# Hackathon submission account. Judges use this ID to pull the trading activity
# they score P&L against. Read from .env so it is never hard-coded to a stale one.
ALPACA_ACCOUNT_ID = os.getenv("ALPACA_ACCOUNT_ID", "")
DATA_BASE = "https://data.alpaca.markets"
# Basic plan serves 'indicative' (derived, not true OPRA). Algo Trader Plus -> 'opra'.
OPTIONS_FEED = os.getenv("PACAPOUNCE_OPTIONS_FEED", "indicative")

# ── Universe & structure ─────────────────────────────────────────────────────
ALLOWLIST = [s.strip().upper() for s in os.getenv("PACAPOUNCE_ALLOWLIST", "SPY,QQQ").split(",")]
DTE_MIN = int(os.getenv("PACAPOUNCE_DTE_MIN", "1"))
DTE_MAX = int(os.getenv("PACAPOUNCE_DTE_MAX", "7"))
# Hackathon mode deliberately maximizes capital deployed into the best spread
# that survives the gate.  Alpaca's live options buying power remains the
# binding limit; this ceiling only prevents an unreasonable broker payload.
SIZING_MODE = os.getenv("PACAPOUNCE_SIZING_MODE", "full_buying_power").strip().lower()
if SIZING_MODE not in {"full_buying_power", "kelly"}:
    raise ValueError("PACAPOUNCE_SIZING_MODE must be full_buying_power or kelly")
FULL_BUYING_POWER = SIZING_MODE == "full_buying_power"
OPTIONS_BP_UTILIZATION = float(os.getenv("PACAPOUNCE_OPTIONS_BP_UTILIZATION", "1.0"))
if not 0 < OPTIONS_BP_UTILIZATION <= 1:
    raise ValueError("PACAPOUNCE_OPTIONS_BP_UTILIZATION must be in (0, 1]")
MIN_OPTIONS_TRADING_LEVEL = int(os.getenv("PACAPOUNCE_MIN_OPTIONS_TRADING_LEVEL", "3"))

# Hard ceiling. Kelly or Alpaca buying power picks the actual size beneath it.
MAX_CONTRACTS = int(os.getenv("PACAPOUNCE_MAX_CONTRACTS", "1000"))
MAX_OPEN_POSITIONS = int(os.getenv("PACAPOUNCE_MAX_OPEN", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("PACAPOUNCE_MAX_TRADES_PER_DAY", "2"))

# ── Operational gate thresholds ──────────────────────────────────────────────
MAX_LOSS_USD = float(os.getenv("PACAPOUNCE_MAX_LOSS_USD", "500"))   # per contract
# Total capital at risk on one position, across all contracts.
MAX_TOTAL_RISK_USD = float(os.getenv("PACAPOUNCE_MAX_TOTAL_RISK_USD", "4000"))
MAX_LEG_SPREAD_PCT = float(os.getenv("PACAPOUNCE_MAX_LEG_SPREAD_PCT", "0.25"))  # bid/ask <=25% of mid
MIN_OPEN_INTEREST = int(os.getenv("PACAPOUNCE_MIN_OI", "100"))
QUOTE_MAX_AGE_SEC = int(os.getenv("PACAPOUNCE_QUOTE_MAX_AGE_SEC", "120"))

# ── Equity risk premium ──────────────────────────────────────────────────────
# The expected annual log return of the underlying, and the ONLY source of this
# strategy's positive expectancy. It is an assumption, not a measurement: 8% is
# the conventional long-run US equity risk premium. The gate reports EV at 0%,
# 4% and 8% so the dependence is visible rather than buried.
DRIFT_ANNUAL = float(os.getenv("PACAPOUNCE_DRIFT_ANNUAL", "0.08"))

# Portfolio objective. This is an expected-return benchmark, never a promise.
# It is converted geometrically to a per-trading-day dollar target for sizing
# and live progress reporting.
ANNUAL_RETURN_TARGET = float(os.getenv("PACAPOUNCE_ANNUAL_RETURN_TARGET", "0.08"))
TRADING_DAYS_PER_YEAR = int(os.getenv("PACAPOUNCE_TRADING_DAYS_PER_YEAR", "252"))

# ── Economic gate ────────────────────────────────────────────────────────────
# Minimum market-implied expected value, per contract, AFTER friction.
MIN_EV_USD = float(os.getenv("PACAPOUNCE_MIN_EV_USD", "0"))
# Proposal budget per decision window (rejection-sampling guard).  One initial
# idea plus at most one reasoned revision prevents gate-mining bursts.
PROPOSAL_BUDGET = int(os.getenv("PACAPOUNCE_PROPOSAL_BUDGET", "2"))
if not 1 <= PROPOSAL_BUDGET <= 2:
    raise ValueError("PACAPOUNCE_PROPOSAL_BUDGET must be 1 or 2")
# How often the autonomous entry supervisor refreshes its broker-owned state.
SESSION_POLL_INTERVAL_SEC = int(os.getenv("PACAPOUNCE_SESSION_POLL_INTERVAL_SEC", "60"))
# Cache the relatively expensive historical-bar + ATM option snapshot brief.
# Current spot/account/clock fields still refresh every proposal window.
REGIME_CACHE_SEC = int(os.getenv("PACAPOUNCE_REGIME_CACHE_SEC", "300"))
# The one-command session rebuilds the static dashboard at this cadence.  The
# page reloads itself on the same cadence, so file:// and static hosting need no
# web server or CORS configuration.
DASHBOARD_REFRESH_INTERVAL_SEC = max(
    10, int(os.getenv("PACAPOUNCE_DASHBOARD_REFRESH_INTERVAL_SEC", "60"))
)

# Live paper-monitor policy.  Mutating actions still require monitor.py's
# explicit --execute flag; these values only determine when it recommends or
# submits an exit.
MONITOR_INTERVAL_SEC = int(os.getenv("PACAPOUNCE_MONITOR_INTERVAL_SEC", "30"))
MONITOR_PROFIT_TARGET_PCT = float(os.getenv("PACAPOUNCE_MONITOR_PROFIT_TARGET_PCT", "0.50"))
MONITOR_STOP_MAX_LOSS_PCT = float(os.getenv("PACAPOUNCE_MONITOR_STOP_MAX_LOSS_PCT", "0.70"))
MONITOR_PIN_BUFFER_USD = float(os.getenv("PACAPOUNCE_MONITOR_PIN_BUFFER_USD", "1.00"))
MONITOR_LIMIT_SLIPPAGE = float(os.getenv("PACAPOUNCE_MONITOR_LIMIT_SLIPPAGE", "0.02"))
# Path-dependent profit protection.  The ratchet is armed only after a spread
# has earned 20% of its opening credit, then protects its executable (natural
# bid/ask) high-water mark.  High P&L volatility tightens rather than directly
# triggers the trail, so a noisy mark cannot close a position by itself.
MONITOR_RATCHET_ARM_PCT = float(os.getenv("PACAPOUNCE_MONITOR_RATCHET_ARM_PCT", "0.20"))
MONITOR_RATCHET_GIVEBACK_PCT = float(
    os.getenv("PACAPOUNCE_MONITOR_RATCHET_GIVEBACK_PCT", "0.20")
)
MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT = float(
    os.getenv("PACAPOUNCE_MONITOR_HIGH_VOL_RATCHET_GIVEBACK_PCT", "0.10")
)
MONITOR_RATCHET_CONFIRMATIONS = int(
    os.getenv("PACAPOUNCE_MONITOR_RATCHET_CONFIRMATIONS", "2")
)
MONITOR_VOL_WINDOW_SAMPLES = int(os.getenv("PACAPOUNCE_MONITOR_VOL_WINDOW_SAMPLES", "10"))
MONITOR_HIGH_VOL_PCT_MAX_PROFIT = float(
    os.getenv("PACAPOUNCE_MONITOR_HIGH_VOL_PCT_MAX_PROFIT", "0.03")
)

# A profitable exit may earn one new attempt, but only after the market has had
# time to reset and only if the new post-cost EV per defined-risk dollar is
# materially better. Tournament mode keeps the same full-BP sizing objective
# after the re-entry-improvement gate authorizes a genuinely better setup.
REENTRY_COOLDOWN_MIN = int(os.getenv("PACAPOUNCE_REENTRY_COOLDOWN_MIN", "30"))
REENTRY_STABLE_MIN = int(os.getenv("PACAPOUNCE_REENTRY_STABLE_MIN", "10"))
REENTRY_MIN_QUALITY_MULTIPLIER = float(
    os.getenv("PACAPOUNCE_REENTRY_MIN_QUALITY_MULTIPLIER", "1.25")
)
REENTRY_BP_UTILIZATION = float(os.getenv("PACAPOUNCE_REENTRY_BP_UTILIZATION", "1.0"))
if not 0 < REENTRY_BP_UTILIZATION <= 1:
    raise ValueError("PACAPOUNCE_REENTRY_BP_UTILIZATION must be in (0, 1]")

# ── Second Paper strategy: NDX30 mean reversion ─────────────────────────────
# This is deliberately a small, frozen policy rather than another free-form AI
# strategy.  It passed the 2024 SIP OOS staging card (146 trades, PF 1.394,
# Sharpe 1.353, max drawdown 1.82%).  It remains PAPER staging, not a promise of
# production alpha.  Python owns every numeric decision; Poe may only veto a
# candidate for concrete event/news risk and write the human-readable thesis.
STOCK_MR_ENABLED = os.getenv("PACAPOUNCE_STOCK_MR_ENABLED", "true").lower() in {
    "1", "true", "yes", "on",
}
STOCK_MR_UNIVERSE = [
    symbol.strip().upper()
    for symbol in os.getenv(
        "PACAPOUNCE_STOCK_MR_UNIVERSE",
        "AAPL,MSFT,AMZN,GOOGL,GOOG,NVDA,TSLA,META,PEP,AVGO,COST,CSCO,TMUS,"
        "ADBE,TXN,CMCSA,AMD,HON,AMGN,QCOM,NFLX,INTU,SBUX,GILD,ADP,BKNG,ISRG,"
        "MDLZ,PYPL,INTC",
    ).split(",")
    if symbol.strip()
]
STOCK_MR_RSI_MAX = float(os.getenv("PACAPOUNCE_STOCK_MR_RSI_MAX", "10"))
STOCK_MR_EQUITY_RISK_PCT = float(
    os.getenv("PACAPOUNCE_STOCK_MR_EQUITY_RISK_PCT", "0.005")
)
STOCK_MR_MAX_NOTIONAL_PCT = float(
    os.getenv("PACAPOUNCE_STOCK_MR_MAX_NOTIONAL_PCT", "0.20")
)
STOCK_MR_STOP_ATR_MULTIPLE = float(
    os.getenv("PACAPOUNCE_STOCK_MR_STOP_ATR_MULTIPLE", "2.0")
)
STOCK_MR_MAX_POSITIONS = int(os.getenv("PACAPOUNCE_STOCK_MR_MAX_POSITIONS", "3"))
STOCK_MR_MAX_ENTRIES_PER_DAY = int(
    os.getenv("PACAPOUNCE_STOCK_MR_MAX_ENTRIES_PER_DAY", "1")
)
STOCK_MR_MAX_HOLD_SESSIONS = int(
    os.getenv("PACAPOUNCE_STOCK_MR_MAX_HOLD_SESSIONS", "3")
)
STOCK_MR_DECISION_MINUTE = int(
    os.getenv("PACAPOUNCE_STOCK_MR_DECISION_MINUTE", "45")
)
if not 0 < STOCK_MR_EQUITY_RISK_PCT <= 0.02:
    raise ValueError("PACAPOUNCE_STOCK_MR_EQUITY_RISK_PCT must be in (0, 0.02]")
if not 0 < STOCK_MR_MAX_NOTIONAL_PCT <= 1:
    raise ValueError("PACAPOUNCE_STOCK_MR_MAX_NOTIONAL_PCT must be in (0, 1]")
if STOCK_MR_MAX_POSITIONS < 1 or STOCK_MR_MAX_ENTRIES_PER_DAY != 1:
    raise ValueError("stock MR requires >=1 positions and exactly one entry per day")


# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = ROOT / "data"
RUNTIME_ACCOUNT = "".join(
    char if char.isalnum() or char in {"-", "_"} else "_"
    for char in ALPACA_ACCOUNT_ID.strip()
)[:64] or "unconfigured"
RUNTIME_DATA_DIR = DATA_DIR / "accounts" / RUNTIME_ACCOUNT
VERDICT_LOG = RUNTIME_DATA_DIR / "verdicts.jsonl"
SESSION_LOG = RUNTIME_DATA_DIR / "session_log.jsonl"
RISK_STATE_FILE = RUNTIME_DATA_DIR / "risk_state.json"
MCP_CALL_LOG = RUNTIME_DATA_DIR / "mcp_calls.json"
STOCK_MR_STATE_FILE = RUNTIME_DATA_DIR / "stock_mr_state.json"
STOCK_MR_LOG = RUNTIME_DATA_DIR / "stock_mr_decisions.jsonl"
GATE_VERSION = "1.4.0"
CALL_REBOUND_MIN_1D_RETURN = float(os.getenv("PACAPOUNCE_CALL_REBOUND_MIN_1D_RETURN", "-0.01"))
SESSION_POLICY_VERSION = "1.5.0"
