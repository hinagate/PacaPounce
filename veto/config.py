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
# Fixed baseline for the paper competition account.  Alpaca exposes current and
# prior-close equity, but not the account's original competition balance, so
# cumulative account P&L needs an explicit, auditable baseline.
STARTING_EQUITY_USD = float(
    os.getenv("PACAPOUNCE_STARTING_EQUITY_USD", "100000")
)
if STARTING_EQUITY_USD <= 0:
    raise ValueError("PACAPOUNCE_STARTING_EQUITY_USD must be positive")
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
# Whether the credit-spread monitor takes profit early at all. The entry gate
# prices a spread on its terminal payoff; the 50% target and the ratchet then
# deliver about a quarter of that on a win while a long-strike breach costs the
# same either way. Simulated on the live SPY 770/772C (3 sessions, 30 s marks,
# realised vol at the gate's own VRP assumption) every early-exit configuration
# was negative expectancy and hold-to-expiry with the breach exit was positive,
# so the profit exits are a switch rather than a constant. Off, the monitor
# keeps the long-strike breach, the defined-loss stop and the expiry pin-risk
# exit: the protection stays, the truncation of the win does not.
MONITOR_PROFIT_EXIT_ENABLED = os.getenv(
    "PACAPOUNCE_MONITOR_PROFIT_EXIT_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}
# Hold-expectancy review. Hold-to-expiry delivers the payoff the entry gate
# priced, but the gate's approval was a statement about the numbers at entry.
# The same model - EWMA realised vol for the level, the live chain's skew for
# the tail shape, the equity risk premium as drift - is re-run on the held
# position every few minutes. The debit to close now is the credit the
# position still earns by holding; when the model's expected terminal loss
# exceeds it, holding has negative expectancy against closing, and after the
# configured consecutive reviews agree the position is closed with a limit.
# This is the gate's own arithmetic applied continuously, not a new opinion,
# and it never pre-empts the defined-risk exits. Off by default; the
# competition runs it on.
MONITOR_HOLD_EV_EXIT_ENABLED = os.getenv(
    "PACAPOUNCE_MONITOR_HOLD_EV_EXIT_ENABLED", "false"
).lower() in {"1", "true", "yes", "on"}
MONITOR_HOLD_EV_AFTER_ET = os.getenv("PACAPOUNCE_MONITOR_HOLD_EV_AFTER_ET", "09:45").strip()
MONITOR_HOLD_EV_INTERVAL_MIN = int(
    os.getenv("PACAPOUNCE_MONITOR_HOLD_EV_INTERVAL_MIN", "5")
)
MONITOR_HOLD_EV_CONFIRMATIONS = int(
    os.getenv("PACAPOUNCE_MONITOR_HOLD_EV_CONFIRMATIONS", "2")
)
if MONITOR_HOLD_EV_INTERVAL_MIN < 1 or MONITOR_HOLD_EV_CONFIRMATIONS < 1:
    raise ValueError(
        "PACAPOUNCE_MONITOR_HOLD_EV_INTERVAL_MIN and "
        "PACAPOUNCE_MONITOR_HOLD_EV_CONFIRMATIONS must be at least one"
    )
# Budget resize. A held spread must fit the lane's current equity share, not
# only the share that applied when it was opened: when SPREAD_EQUITY_PCT is
# tightened, the excess contracts are closed with an atomic limit and the rest
# keep the thesis at the size the policy now allows. The ceiling is equity x
# share, deliberately not live options buying power - a held position's
# collateral is already posted, so remaining BP says nothing about it.
MONITOR_BUDGET_RESIZE_ENABLED = os.getenv(
    "PACAPOUNCE_MONITOR_BUDGET_RESIZE_ENABLED", "false"
).lower() in {"1", "true", "yes", "on"}

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
# How many same-day re-entries one profitable exit may earn. Each still has to
# clear the cooldown, the market-reset window, and every gate including the
# economic one; this only stops a single profitable close from ending the
# session's entry hunting when capital is free and the setup is still good.
REENTRY_MAX_PER_DAY = int(os.getenv("PACAPOUNCE_REENTRY_MAX_PER_DAY", "1"))
if REENTRY_MAX_PER_DAY < 1:
    raise ValueError("PACAPOUNCE_REENTRY_MAX_PER_DAY must be at least 1")

# ── Second options strategy: NDX30 long-call mean reversion ─────────────────
# The underlying signal passed the 2024 SIP OOS staging card.  Competition
# execution is options-only: Python resolves one liquid 14-30 DTE long call,
# sizes its premium and monitors its deterministic exit.  The stock result is a
# signal proxy, not an options P&L claim.
OPTION_MR_ENABLED = os.getenv("PACAPOUNCE_OPTION_MR_ENABLED", "true").lower() in {
    "1", "true", "yes", "on",
}
OPTION_MR_UNIVERSE = [
    symbol.strip().upper()
    for symbol in os.getenv(
        "PACAPOUNCE_OPTION_MR_UNIVERSE",
        "AAPL,MSFT,AMZN,GOOGL,GOOG,NVDA,TSLA,META,PEP,AVGO,COST,CSCO,TMUS,"
        "ADBE,TXN,CMCSA,AMD,HON,AMGN,QCOM,NFLX,INTU,SBUX,GILD,ADP,BKNG,ISRG,"
        "MDLZ,PYPL,INTC",
    ).split(",")
    if symbol.strip()
]
OPTION_MR_RSI_MAX = float(os.getenv("PACAPOUNCE_OPTION_MR_RSI_MAX", "10"))
OPTION_MR_EQUITY_RISK_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_EQUITY_RISK_PCT", "0.005")
)
OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT", "0.02")
)
# Premium ceiling for ONE long-call position, as a share of equity.
OPTION_MR_MAX_PREMIUM_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_MAX_PREMIUM_PCT", "0.20")
)
OPTION_MR_STOP_ATR_MULTIPLE = float(
    os.getenv("PACAPOUNCE_OPTION_MR_STOP_ATR_MULTIPLE", "2.0")
)
OPTION_MR_DTE_MIN = int(os.getenv("PACAPOUNCE_OPTION_MR_DTE_MIN", "14"))
OPTION_MR_DTE_MAX = int(os.getenv("PACAPOUNCE_OPTION_MR_DTE_MAX", "30"))
OPTION_MR_DELTA_TARGET = float(os.getenv("PACAPOUNCE_OPTION_MR_DELTA_TARGET", "0.70"))
OPTION_MR_DELTA_MIN = float(os.getenv("PACAPOUNCE_OPTION_MR_DELTA_MIN", "0.55"))
OPTION_MR_DELTA_MAX = float(os.getenv("PACAPOUNCE_OPTION_MR_DELTA_MAX", "0.85"))
OPTION_MR_MAX_SPREAD_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_MAX_SPREAD_PCT", "0.06")
)
# Mean underlying move per signal trade, measured on the frozen 2026 YTD scan
# (108 trades, mean +0.795%, median +0.789%). This is what a long call has to
# be bought cheaply enough to monetise: it is the measured edge, not a target.
OPTION_MR_SIGNAL_EDGE_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_SIGNAL_EDGE_PCT", "0.00795")
)
if not 0 < OPTION_MR_SIGNAL_EDGE_PCT < 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_SIGNAL_EDGE_PCT must be in (0, 1)")
# How many times the measured edge a contract's carry may cost before it is
# rejected. At 1.0 the lane only buys calls the signal can actually pay for -
# the expected-value reading. Tournament mode loosens this deliberately: the
# objective there is upper-tail, not expected value, so the gate's job narrows
# to throwing out the genuinely unpayable contracts rather than proving edge.
# Selection still ranks by cheapest carry, so a looser ceiling never makes the
# chosen contract worse - it only enlarges the pool when the tight one is empty.
OPTION_MR_CARRY_EDGE_MULTIPLE = float(
    os.getenv("PACAPOUNCE_OPTION_MR_CARRY_EDGE_MULTIPLE", "1.0")
)
if OPTION_MR_CARRY_EDGE_MULTIPLE < 1.0:
    raise ValueError("PACAPOUNCE_OPTION_MR_CARRY_EDGE_MULTIPLE must be at least 1.0")
OPTION_MR_CARRY_CEILING_PCT = OPTION_MR_SIGNAL_EDGE_PCT * OPTION_MR_CARRY_EDGE_MULTIPLE
OPTION_MR_MAX_POSITIONS = int(os.getenv("PACAPOUNCE_OPTION_MR_MAX_POSITIONS", "3"))
OPTION_MR_MAX_ENTRIES_PER_DAY = int(
    os.getenv("PACAPOUNCE_OPTION_MR_MAX_ENTRIES_PER_DAY", "1")
)
OPTION_MR_MAX_HOLD_SESSIONS = int(
    os.getenv("PACAPOUNCE_OPTION_MR_MAX_HOLD_SESSIONS", "3")
)
# The EMA5 recovery exit is the strategy's profit-taking rule: once the bounce
# arrives, the mean-reversion thesis is spent and it closes. Under the
# upper-tail objective that rule is the binding cap on the payoff the lane
# exists to buy - it typically closes on session two for a small gain and gives
# up the rest of the window. Disabling it keeps the 2xATR stop and the
# holding-session limit, so downside protection is unchanged; only the
# profit-taking is removed.
OPTION_MR_PROFIT_EXIT_ENABLED = os.getenv(
    "PACAPOUNCE_OPTION_MR_PROFIT_EXIT_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}

# Profit ratchet for the long-call lane. Being wrong about direction is a
# probability outcome and the 2xATR stop already bounds it. Being RIGHT and
# ending flat is not a probability outcome, it is a missing control: the lane
# earned the move it was bought for and then handed it back. The ratchet exists
# for that case only.
#
# Once a position has armed, its executable high-water mark is trailed by
# GIVEBACK_PCT of the gain. The trailing floor is clamped at breakeven, and once
# armed a confirmed mark at or below it closes the position whatever its sign:
# a gap through the floor is a breach, not an exemption from one. Elevated P&L
# volatility tightens the trail rather than closing by itself, and a close
# needs CONFIRMATIONS consecutive breaches so one bad quote cannot exit.
OPTION_MR_RATCHET_ENABLED = os.getenv(
    "PACAPOUNCE_OPTION_MR_RATCHET_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}
# Arm on an ATR-normalised move of the UNDERLYING, not on a share of the
# premium. "This position has been right" is a statement about the stock, and a
# long call's P&L moves by its leverage - delta x spot / premium - which ranged
# 8.4x to 29.9x across live positions. A single 15% of premium therefore asked
# GOOG for 0.50% and INTC for 1.79%, i.e. 0.28 to 0.58 ATR: the same rule
# meaning different things. Measured over 400 sessions on the 14-name universe,
# a favourable 5-session excursion peaks near 2x ATR regardless of the name, so
# arming at 0.35 ATR protects from roughly the first sixth of a typical move.
# Symmetric with the 2x ATR stop, in the same units.
OPTION_MR_RATCHET_ARM_ATR = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_ARM_ATR", "0.35")
)
# Arming is a statement about the stock, but the executable P&L it was read
# from is a statement about the option's bid. On an illiquid contract the bid
# lags the stock: AMD 415C on 2026-09-02 traded three times all morning, the
# stock moved +0.47 ATR, and the bid-based high water reached 83% of the arm
# threshold - so the trail never existed and +$1,780 went back to +$20. The
# arm test therefore also reads the underlying directly: once the stock has
# been ARM_ATR above the signal price, the position has been right, and it
# arms provided the executable high water is at least this share of the
# P&L threshold. A wide quote may hide up to half the move, not all of it -
# below that, the contract cannot realise its gains and a trail on them would
# be a trail on nothing.
OPTION_MR_RATCHET_ARM_QUOTE_ALLOWANCE = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_ARM_QUOTE_ALLOWANCE", "0.5")
)
if not 0 < OPTION_MR_RATCHET_ARM_QUOTE_ALLOWANCE <= 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_RATCHET_ARM_QUOTE_ALLOWANCE must be in (0, 1]")
if OPTION_MR_RATCHET_ARM_ATR <= 0:
    raise ValueError("PACAPOUNCE_OPTION_MR_RATCHET_ARM_ATR must be positive")
# Fallback when a position has no usable ATR or delta - an adopted position, or
# a chain that returned no greeks. Never the primary path.
OPTION_MR_RATCHET_ARM_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_ARM_PCT", "0.15")
)
OPTION_MR_RATCHET_GIVEBACK_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_GIVEBACK_PCT", "0.40")
)
OPTION_MR_RATCHET_HIGH_VOL_GIVEBACK_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_HIGH_VOL_GIVEBACK_PCT", "0.25")
)
OPTION_MR_RATCHET_CONFIRMATIONS = int(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_CONFIRMATIONS", "2")
)
OPTION_MR_RATCHET_VOL_SAMPLES = int(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_VOL_SAMPLES", "10")
)
OPTION_MR_RATCHET_HIGH_VOL_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_HIGH_VOL_PCT", "0.03")
)
# Volatility is measured in the arm threshold's units: the dispersion of the
# last VOL_SAMPLES marks over the P&L of a one-ATR move in the underlying. As a
# share of premium the flag depended on leverage - in the same INTC minutes the
# 85C read 0.033 and the 80C 0.020 - so it tightened one trail and not the
# other. At 30 s sampling a normal tape sits near 0.02 of an ATR move with a
# 95th percentile near 0.04, which is the default. The premium share above is
# the fallback for a position with no usable ATR or delta.
OPTION_MR_RATCHET_HIGH_VOL_ATR = float(
    os.getenv("PACAPOUNCE_OPTION_MR_RATCHET_HIGH_VOL_ATR", "0.04")
)
if OPTION_MR_RATCHET_HIGH_VOL_ATR <= 0:
    raise ValueError("PACAPOUNCE_OPTION_MR_RATCHET_HIGH_VOL_ATR must be positive")
# A close is a day limit at the live bid. Marketable when sent, it can still
# miss when the bid moves in the second between quote and order - and the stop
# fires on exactly the tape where that happens. Left alone the order sat until
# the close and the position was unprotected for the rest of the day. Unfilled
# for this long it is cancelled and re-sent at the fresh bid; after this many
# chases it goes at market, because a stop that is not filled protects nothing.
OPTION_MR_EXIT_CHASE_AFTER_SEC = int(
    os.getenv("PACAPOUNCE_OPTION_MR_EXIT_CHASE_AFTER_SEC", "20")
)
OPTION_MR_EXIT_MARKET_AFTER_CHASES = int(
    os.getenv("PACAPOUNCE_OPTION_MR_EXIT_MARKET_AFTER_CHASES", "2")
)
if OPTION_MR_EXIT_CHASE_AFTER_SEC < 0 or OPTION_MR_EXIT_MARKET_AFTER_CHASES < 1:
    raise ValueError(
        "PACAPOUNCE_OPTION_MR_EXIT_CHASE_AFTER_SEC must be non-negative and "
        "PACAPOUNCE_OPTION_MR_EXIT_MARKET_AFTER_CHASES at least one"
    )
# The lane holds one position per issuer (GOOG and GOOGL are one issuer). A
# position adopted from outside the lane can break that; when it does, the
# adopted duplicate is closed after the opening rotation, because the
# concentration was never the lane's choice and doubling it is not a thesis.
OPTION_MR_ENFORCE_ISSUER_LIMIT = os.getenv(
    "PACAPOUNCE_OPTION_MR_ENFORCE_ISSUER_LIMIT", "true"
).lower() in {"1", "true", "yes", "on"}
if OPTION_MR_RATCHET_ARM_PCT <= 0:
    raise ValueError("PACAPOUNCE_OPTION_MR_RATCHET_ARM_PCT must be positive")
for _name, _value in (
    ("GIVEBACK", OPTION_MR_RATCHET_GIVEBACK_PCT),
    ("HIGH_VOL_GIVEBACK", OPTION_MR_RATCHET_HIGH_VOL_GIVEBACK_PCT),
):
    if not 0 < _value < 1:
        raise ValueError(
            f"PACAPOUNCE_OPTION_MR_RATCHET_{_name}_PCT must be in (0, 1); a value "
            "of 1 or more would let a protected position fall back into a loss"
        )
if OPTION_MR_RATCHET_HIGH_VOL_GIVEBACK_PCT > OPTION_MR_RATCHET_GIVEBACK_PCT:
    raise ValueError("the high-volatility giveback must tighten the trail, not loosen it")
if OPTION_MR_RATCHET_CONFIRMATIONS < 1 or OPTION_MR_RATCHET_VOL_SAMPLES < 2:
    raise ValueError("ratchet confirmations >= 1 and volatility samples >= 2")
OPTION_MR_DECISION_MINUTE = int(
    os.getenv("PACAPOUNCE_OPTION_MR_DECISION_MINUTE", "45")
)


def _decision_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse "HH:MM,HH:MM" into sorted, validated regular-session windows."""
    parsed = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hour, minute = (int(part) for part in chunk.split(":", 1))
        except ValueError as exc:
            raise ValueError(f"invalid option-MR decision window {chunk!r}") from exc
        if not (0 <= minute < 60) or not (9, 30) <= (hour, minute) <= (15, 45):
            raise ValueError(
                f"option-MR decision window {chunk!r} must be between "
                "09:30 and 15:45 ET, leaving room for the ten-minute window"
            )
        parsed.add((hour, minute))
    if not parsed:
        raise ValueError("at least one option-MR decision window is required")
    return tuple(sorted(parsed))


# Each window is ten minutes long. A single 15:45 scan sees only the completed
# 15:30 bar, so a name that is oversold in the morning and recovers by the close
# is never seen, and a session without a 15:30 signal deploys nothing at all.
# Measured over 120 sessions, 27.5% of sessions produce no candidate and misses
# cluster hard: P(miss | yesterday missed) is 53% against 17% otherwise, with a
# nine-session drought in sample. More windows is the direct answer to that.
OPTION_MR_DECISION_WINDOWS = _decision_windows(
    os.getenv(
        "PACAPOUNCE_OPTION_MR_DECISION_WINDOWS",
        f"15:{OPTION_MR_DECISION_MINUTE:02d}",
    )
)
# The monitor's once-daily exit check runs in the last window of the session.
OPTION_MR_EXIT_CHECK_WINDOW = OPTION_MR_DECISION_WINDOWS[-1]
# ── Two-lane capital budget ─────────────────────────────────────────────────
# Each lane gets a share of EQUITY, not of whatever buying power happens to be
# left. Sizing off remaining BP made the second lane's allocation depend on the
# order the two lanes happened to fill in, and starved whichever went second.
# Live options buying power still bounds both: these are ceilings, not grants.
SPREAD_EQUITY_PCT = float(os.getenv("PACAPOUNCE_SPREAD_EQUITY_PCT", "0.95"))
if not 0 < SPREAD_EQUITY_PCT <= 1:
    raise ValueError("PACAPOUNCE_SPREAD_EQUITY_PCT must be in (0, 1]")
# Total premium the long-call lane may hold open across all its positions.
OPTION_MR_TOTAL_PREMIUM_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_TOTAL_PREMIUM_PCT", "0.05")
)
if not 0 < OPTION_MR_TOTAL_PREMIUM_PCT <= 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_TOTAL_PREMIUM_PCT must be in (0, 1]")

# Long-call sizing objective.
#   risk_budget - size from the 2xATR modeled stop (the validated risk model)
#   tournament  - size from the premium budget directly, because a leaderboard
#                 rewards the upper tail rather than expected value. This is an
#                 explicit objective change and is disclosed as one; it does not
#                 pretend a 17%-of-equity position is a 0.5% risk budget.
OPTION_MR_SIZING_MODE = os.getenv(
    "PACAPOUNCE_OPTION_MR_SIZING_MODE", "risk_budget"
).strip().lower()
if OPTION_MR_SIZING_MODE not in {"risk_budget", "tournament"}:
    raise ValueError(
        "PACAPOUNCE_OPTION_MR_SIZING_MODE must be risk_budget or tournament"
    )
OPTION_MR_TOURNAMENT = OPTION_MR_SIZING_MODE == "tournament"
if not 0 < OPTION_MR_EQUITY_RISK_PCT <= 0.02:
    raise ValueError("PACAPOUNCE_OPTION_MR_EQUITY_RISK_PCT must be in (0, 0.02]")
# In tournament mode the premium budget buys the upper tail, but what a
# position puts at its 2xATR stop is delta x 2 x ATR x 100 per contract: 54%
# of premium at delta 0.8 and about all of it at delta 0.6. Sized on premium
# alone, two positions of equal cost risked 18% and 37% of equity. This caps
# the modeled stop loss of one position so the risk taken does not depend on
# which delta the chain happened to offer. The 0.5% risk budget above stays
# the risk_budget-mode input and is far inside this cap.
OPTION_MR_MAX_STOP_RISK_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_MAX_STOP_RISK_PCT", "0.15")
)
if not 0 < OPTION_MR_MAX_STOP_RISK_PCT <= 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_MAX_STOP_RISK_PCT must be in (0, 1]")
if not OPTION_MR_EQUITY_RISK_PCT <= OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT <= 0.02:
    raise ValueError(
        "PACAPOUNCE_OPTION_MR_ONE_CONTRACT_RISK_CAP_PCT must be between the "
        "target risk and 0.02"
    )
if not 0 < OPTION_MR_MAX_PREMIUM_PCT <= 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_MAX_PREMIUM_PCT must be in (0, 1]")
if not 0 < OPTION_MR_DTE_MIN <= OPTION_MR_DTE_MAX:
    raise ValueError("option MR DTE range is invalid")
if not 0 < OPTION_MR_DELTA_MIN <= OPTION_MR_DELTA_TARGET <= OPTION_MR_DELTA_MAX < 1:
    raise ValueError("option MR delta range/target is invalid")
if not 0 < OPTION_MR_MAX_SPREAD_PCT <= 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_MAX_SPREAD_PCT must be in (0, 1]")
# The relative spread above is a share of the option's price; what the exit
# side needs is a bid that keeps up with the stock, and that is a share of an
# ATR move. AMD260925C00415000 passed the 6% test at 5.1% on 2026-09-02 and
# cost $235/contract to cross - 15% of the P&L a one-ATR move produces
# (19.17 x 0.79 x 100 = $1,523) - so the bid lagged the stock and the trail
# never armed. INTC's $20 was 5% of its $388. Crossing cost may take at most
# this share of a one-ATR move, in the same units as the arm and the stop.
OPTION_MR_MAX_CROSSING_ATR_PCT = float(
    os.getenv("PACAPOUNCE_OPTION_MR_MAX_CROSSING_ATR_PCT", "0.10")
)
if not 0 < OPTION_MR_MAX_CROSSING_ATR_PCT <= 1:
    raise ValueError("PACAPOUNCE_OPTION_MR_MAX_CROSSING_ATR_PCT must be in (0, 1]")
if OPTION_MR_MAX_POSITIONS < 1 or OPTION_MR_MAX_ENTRIES_PER_DAY < 1:
    raise ValueError("option MR requires at least one position and one entry per day")
if OPTION_MR_MAX_ENTRIES_PER_DAY > OPTION_MR_MAX_POSITIONS:
    raise ValueError(
        "PACAPOUNCE_OPTION_MR_MAX_ENTRIES_PER_DAY cannot exceed MAX_POSITIONS"
    )


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
OPTION_MR_STATE_FILE = RUNTIME_DATA_DIR / "option_mr_state.json"
OPTION_MR_LOG = RUNTIME_DATA_DIR / "option_mr_decisions.jsonl"
GATE_VERSION = "1.4.0"
CALL_REBOUND_MIN_1D_RETURN = float(os.getenv("PACAPOUNCE_CALL_REBOUND_MIN_1D_RETURN", "-0.01"))
SESSION_POLICY_VERSION = "1.5.0"
