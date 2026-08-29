# PacaPounce

> **Team:** a-meowmeow

> Built for the
> [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).
> See the [judge-facing submission guide](HACKATHON_SUBMISSION.md) for the live
> paper result, criteria mapping, demo script, and social launch package.

**The patient AI trading agent: it hunts, verifies, then pounces.**

Built for the Alpaca AI Trading Agents Hackathon. The LLM is creative but untrusted:
it can suggest any options thesis it likes, but only deterministic, independently
tested policy is allowed to touch the broker.

The implementation retains `veto` as the internal gate-engine package and
broker client-ID prefix so PacaPounce can reconcile earlier orders safely across
process restarts. Public runtime configuration uses `PACAPOUNCE_*` variables.

> **Competition paper account:** `PA3NRNIECO2O` · $100,000 start · options level 3.
> The judged P&L lives on this account; the dashboard banner verifies the live
> account number matches before showing results.

### Judge in 60 seconds

```powershell
cd PacaPounce
..\.venv\Scripts\python.exe run.py --summary                # ledger stats
..\.venv\Scripts\python.exe scripts\build_dashboard.py      # live dashboard snapshot
start dashboard\index.html                                  # open the judge view
```

The dashboard is a single static file (data embedded, works from `file://` or any
static host). Its first screen is deliberately operational: live account metrics,
open Alpaca positions, and a bounded decision log explaining what the agent did
and why. The AI funnel, all 16 gates, P&L evidence, and the complete execution
audit remain available in one expandable deep-dive section.

---

## The problem

AI agents are about to generate options trades at industrial scale, and almost
nothing checks whether those trades are worth taking.

The usual safety layer is a list of operational rules — defined risk only, one
contract, liquid strikes, a max-loss cap, limit orders. Every one of those rules
is correct. None of them asks whether the trade makes money.

Here is a real production strategy, run through a complete operational gate stack:

| Check | Result |
|---|---|
| Defined-risk spread, no naked legs | PASS |
| SPY allowlist | PASS |
| One contract, one position | PASS |
| Fresh quotes, tight bid/ask | PASS |
| Limit order, minimum credit met | PASS |
| Max loss $470 under $500 cap | PASS |
| Duplicate / correlated exposure | PASS |
| Daily breaker, rate limit, cooldown | PASS |
| Deterministic exits, assignment rules | PASS |

Clean sweep. The trade collects $30 to risk $470 — it needs a **94.0%** win rate to
break even, and the market's own delta prices the win rate at **85%**. It is
negative expectancy, and every operational gate waved it through.

That trade is `tests/test_gates.py::test_s13_passes_operational_but_fails_economic`.
It is a strategy the author actually shipped.

## What this agent claims, and what it does not

**It does not claim alpha.** It harvests two documented risk premia with capped
downside, and the gate's job is ensuring it never overpays for that exposure:

- **Variance risk premium.** Index implied vol exceeds subsequent realised vol
  most of the time. Live: SPY ATM IV 14.0% against 11.3% EWMA realised, a ratio
  of 0.80.
- **Equity risk premium.** Equities drift up. A far-OTM put credit spread is a
  long-delta position, so the ERP is part of its expected return.

Both are compensation for bearing risk, not inefficiencies. Saying so is the
honest framing, and it is also the only framing the evidence supports.

The primary options strategy is deliberately limited to two structures that are
fully implemented end to end: bullish/neutral put credit spreads and
bearish/neutral call credit spreads. Put loss integrates the left tail; call
loss integrates the upper tail. Calls also fail a dedicated rebound-risk gate
after an index falls more than 1% in a day. Iron condors are not advertised
until four-leg construction, payoff, sizing, monitoring, and validation exist.

## Second Paper strategy: NDX30 mean reversion

When no option position or opening order has reserved the tournament account,
the same `run.py --loop` process gets one deterministic fallback decision at
15:45 ET on a normal 16:00 session. It scans 30 liquid Nasdaq names and buys at
most one stock whose 15:30 completed-bar price is above a rising SMA200 while
Wilder RSI(2) is below 10. Alphabet share classes are issuer-deduplicated.

- Quantity risks 0.5% of equity at a 2×ATR14 hard stop and is capped at 20% of
  equity notional; no more than three stock positions may coexist.
- Alpaca MCP submits one Paper OTO stock order, so the protective stop becomes
  broker-native after entry. `get_orders` must confirm the client ID and broker
  order ID before SUBMITTED is recorded.
- The 30-second monitor exits deterministically when the 15:30 price recovers
  above EMA5 or after three regular sessions. It cancels and verifies the stop
  before sending the closing order. The LLM never calculates or times an exit.
- Poe sees the top numeric candidate plus timestamped Alpaca `get_news` output.
  It may veto a concrete news/event risk and writes the thesis. `get_news` is
  **not** presented as a verified earnings calendar; this limitation remains
  explicit on the dashboard.

Frozen 2024 SIP OOS staging result: 146 trades, +4.98%, profit factor 1.394,
Sharpe 1.353, 53.4% wins, and 1.82% maximum drawdown with 3 bps modeled on each
stock entry and exit. A separate 2023 screen was positive but below the strict
promotion card, so this is honestly labeled **Paper Staging**, not production
proof or guaranteed alpha.
The compact, machine-readable validation card is
[`data/ndx30_mr_validation.json`](data/ndx30_mr_validation.json); it contains no
historical payload dump and records that the research made zero order calls.

Calendar/diagonal structures were excluded pending a cross-expiry assignment
lifecycle; covered-option combinations require stock inventory; naked and ratio
structures violate defined-risk policy; box spreads add financing, not another
market regime.

## Three edges found, three killed

Before settling on the above, the agent's own chain scan produced three
candidate edges. All three were artifacts of the pricing model, and each died to
a more correct one:

| Candidate | Looked like | Killed by |
|---|---|---|
| Wide far-OTM put spreads | +$16 to +$20 | Flat-vol tails. Market prices the 712 wing at 49% IV; the model used 11%. |
| ATM call spreads | +$23.35 | Zero-drift assumption. Flips to -$4.62 at an 8% ERP. |
| Deep-tail overpricing | 9.0x implied vs historical | 4 overlapping observations in a 6-year bull sample. Unmeasurable. |

Every time the model got more correct, the edge shrank: +$20 -> +$23 -> +$1.43
-> ~0. **A monotone decay under increasing rigor is the signature of an edge
that was never there.**

What survived is smaller and real: the near tail (-0.5% to -2%), where implied
probability exceeds historical frequency by 1.25-1.40x on 115-424 observations.

## The economic gate

```
Tail SHAPE   market's per-strike implied vols     (skew, from the chain)
Vol LEVEL    EWMA realised / ATM implied          (the variance premium)
Drift        equity risk premium, default 8%/yr   (a stated assumption)
Friction     measured from real bid/ask           (not mid)
```

EV is reported at 0%, 4% and 8% drift on every verdict, so the ERP dependence is
visible rather than buried. A trade that only works at 8% is a trade that only
works if the assumption holds.

## Sizing

The hackathon configuration uses `full_buying_power`: after a spread passes the
economic model, quantity is the largest integer count whose entire defined loss
fits inside Alpaca's live `options_buying_power`. This submission intentionally
sets `PACAPOUNCE_OPTIONS_BP_UTILIZATION=1.0`: on the $100,000 competition account, one
approved spread may therefore carry nearly $100,000 of defined maximum loss.
The result records source buying power, usable budget, quantity, total defined
loss, and utilization percentage.

This is tournament sizing, not a prudent production allocation. It maximizes
scored exposure only after all deterministic gates survive, but a single
maximum-loss outcome can still consume almost the entire paper account. Use
fractional utilization or `PACAPOUNCE_SIZING_MODE=kelly` outside the competition. The
dashboard discloses the active 100% allocation and its downside explicitly.

Every sized position reports its full outcome distribution, not just its mean. A
point estimate is close to useless when the spread of outcomes dwarfs it.

## Architecture

```
  MCP session supervisor ..... clock + calendar + orders + positions + account
        |                       sleeps to MCP next_open; rolls across sessions;
        v                       pending locks, entries, Level 3+ eligibility,
                                blocks, and live options buying power
  MCP regime brief .......... live spot + completed daily bars + nearest ATM IV
        |                       1D/5D returns, RV20, IV/RV, quote timestamp;
        v                       cached 5 minutes, missing fields stay missing
  LLM (Poe / gemini-3.7-flash)
        |  intent JSON — never an order, never a strike
        v
  Coherence check ............ can any strike pair satisfy this
        |                       delta target AND this max-loss cap?
        v                       Rejected free, before any chain lookup.
  Contract builder ........... resolves intent against the LIVE chain
        |                       via Alpaca MCP. Real strikes, real
        v                       bid/ask, real Greeks.
  Operational gates .......... 13 controls: defined risk, Alpaca eligibility,
        |                       options-BP collateral, allowlist, size,
        |                       duplicates, freshness, liquidity and limits
        v
  ECONOMIC GATE .............. market-implied EV, net of measured friction
        |
        v
  Executor ................... atomic multi-leg limit order, paper only
        |                       negative limit_price = credit
        v
  Verdict ledger ............. every proposal, approved and rejected,
                               append-only, with the gate version hash

  15:45 fallback lane ......... SIP D1 + completed 15m bars -> SMA/RSI/ATR/EMA
        |                       one top stock candidate; options capital must be clear
        v
  Alpaca news + Poe review .... event-risk veto/thesis only; no fake earnings claim
        |
        v
  Stock OTO + monitor ......... broker stop -> EMA5/3-session deterministic exit
```

The LLM never names a strike. It emits intent; deterministic code resolves it.
That removes hallucinated OCC symbols, impossible strikes and malformed legs as a
category, and it makes proposals comparable to each other. Its compact prompt is
grounded in timestamped Alpaca MCP observations: current spot, completed-bar
returns, realized volatility, nearest-ATM implied volatility, and IV/RV. Alpaca
MCP does not expose a direct VIX or historical IV-rank series here, so neither is
invented or presented to the model.

## Alpaca integration

Everything reaching Alpaca goes through the **official Alpaca MCP server**
(`alpaca-mcp-server`, stdio transport, 72 tools):

| Purpose | MCP tool |
|---|---|
| Underlying quotes | `get_stock_latest_quote` |
| LLM regime grounding | `get_stock_bars`, `get_option_contracts`, `get_option_snapshot` |
| Chain + Greeks + IV | `get_option_chain`, `get_option_snapshot` |
| Session lifecycle | `get_clock`, `get_calendar`, `get_orders` |
| Account and positions | `get_account_info`, `get_all_positions` |
| Fill reconciliation | `get_account_activities` (`activity_types="FILL"`) |
| Multi-leg execution | `place_option_order` (`order_class="mleg"`) |
| Stock MR bars + event context | `get_stock_bars`, `get_news` |
| Protected stock execution | `place_stock_order` (`order_class="oto"`), `cancel_order_by_id` |

There is no direct Alpaca REST fallback in the runnable agent. An incomplete MCP
snapshot fails closed. Paper account only — `ALPACA_PAPER_TRADE=true` is pinned
in `veto/mcp_client.py` and the trading base URL is hard-coded to `paper-api`.

`get_account_info` is also a trade authorization input, not merely a dashboard
statistic. Before Poe is called, and again immediately before order submission,
the agent requires account status `ACTIVE`, no broker/user trading block,
`options_approved_level >= 3`, `options_trading_level >= 3`, and positive
`options_buying_power`. The full deterministic gate is rerun against that final
MCP snapshot so stale eligibility or buying power cannot reach the executor.

## Rejection sampling is overfitting at execution time

If the model may resample until something passes, the gate stops being a filter
and becomes a fitness function — the LLM optimises against the gate's weaknesses
instead of against the market. Two defences:

- **Conditional proposal budget.** One initial idea may receive at most one reasoned
  revision. Economic rejection must diversify DTE, underlying, or strategy;
  broker/session failures close the window immediately.
- **Split modes.** `--measure` runs unlimited proposals and executes **nothing**;
  more samples there is just a bigger sample. `--trade` is budgeted. Headline
  pass rate is reported on first proposals so revision cannot inflate it.

Every rejected proposal is logged in full to the append-only JSONL audit. The
judge-facing dashboard keeps that evidence usable by grouping retry attempts into
decision windows: it always shows approved/submitted results and only the newest
representative no-trade outcomes, with the hidden presentation count disclosed.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add POE_KEY and Alpaca PAPER keys

python run.py --check         # environment + connectivity diagnostics
python run.py --offline --measure 10   # LLM + gates, no Alpaca keys needed
python run.py --propose       # one live cycle, gated, NOT executed
python run.py --trade         # one live cycle, executed if approved
python run.py --loop          # entries + risk monitor + live dashboard, one command
python run.py --summary       # ledger statistics

# Standalone monitor modes are still useful for diagnostics or observation.
python scripts/monitor.py --once          # inspect current paper position once
python scripts/monitor.py                 # observe through the market close
python scripts/monitor.py --execute       # auto-exits without the proposal loop

python scripts/build_dashboard.py          # render dashboard/index.html once
python scripts/build_dashboard.py --watch  # standalone 60-second live rebuild
pytest tests/ -q
```

PacaPounce loads only `PacaPounce/.env`; it never falls back to an `.env` in a
parent directory. This prevents another project from silently changing the
paper account, model, market-data feed, or risk policy.

The monitor reconstructs credit spreads and the second strategy's stock lifecycle
from Alpaca's live positions and polls every 30 seconds. It batches independent MCP requests, measures both
midpoint and immediately executable P&L, and takes profit after 50% of the opening
credit is captured. A restart-safe profit ratchet arms after 20% capture and
closes after two confirmed observations give back 20% of the executable high
water; high P&L volatility tightens that trail to 10% but never closes by itself.
The monitor also applies the repository's 70%-of-max-loss guard on non-expiry
days. Expiry-day stop-loss exits are suppressed; only late pin risk between the
strikes forces a close. A standalone monitor requires `--execute`
before it may mutate; the explicitly mutating `run.py --loop` command enables its
bundled paper monitor. Both paths check the MCP environment and trade URL for
paper mode at startup.

For managed stocks, the same monitor treats the broker OTO stop as the overnight
safety layer. At the daily decision window it uses completed SIP bars to test
EMA5 recovery and the Alpaca calendar to count regular holding sessions. Before
a dynamic sell it cancels the protective stop and confirms that no stop remains,
preventing two live sell orders from racing against one long position.

`run.py --loop` owns the full paper-session lifecycle. It waits for Alpaca to
open, starts the deterministic monitor with paper auto-exits plus the live
dashboard builder, supervises and restarts both processes if needed, and writes
one final dashboard snapshot at each close. It then sleeps in bounded intervals
using Alpaca MCP's `next_open`, including weekends and holidays, and starts both
helpers again at the next session without restarting `run.py`. The static page
reloads the atomically written snapshot every 60 seconds while the market is
open. If the monitor cannot run, new
entries fail closed. Reaching the daily trade cap locks
new proposals but leaves risk monitoring active through the closing bell. In
`full_buying_power` mode the 8%-annual figure remains visible as a benchmark but
does not force a profit exit. Once its first full-capital spread is open, entry
hunting enters a broker-backed wait state before Poe is called; the risk monitor
continues through the closing bell. `Ctrl+C` cleanly stops both processes.

The bundled entry loop is supervised by Alpaca MCP. Before any Poe request,
it reads the broker clock and calendar, open and same-day orders, positions, and
account. It pauses outside the regular session (including short sessions), sleeps
toward MCP's next opening bell, blocks while an opening or closing order is
pending, and counts
filled parent PacaPounce entries from Alpaca instead of process memory. Alpaca FILL
activities independently corroborate the child-leg executions, while the parent
order remains the one logical trade. Structured client IDs
(`veto-open-YYYYMMDD-<decision>-rN`) group order-chase revisions as one
logical trade. A restart therefore cannot reset the daily cap or forget a live
order. The same preflight and all 16 ordered gates run again immediately before
submission and fail closed if MCP is unavailable, the account loses options
eligibility, or remaining options buying power no longer covers the order.
An unfilled opening limit improves by at most one cent per monitor cycle and
never crosses below the live natural credit. Because Alpaca reserves collateral
while the old order is pending, replacement sizing first includes that order's
releasable defined loss, then cancels, refreshes `options_buying_power` through
MCP, and sizes again before submitting. Replacement EV uses the actual limit
price, so midpoint-to-natural friction is not charged twice. An MCP
<code>accepted</code> envelope is not treated as an order: the monitor polls
<code>get_orders(status=all)</code> and records a successful chase only after the
same client ID appears with an Alpaca broker order ID. Missing or terminal
replacements are logged as failures.
The configured submission account must also match MCP's live `account_number`.
Ledger, monitor state, and MCP telemetry are stored below an account-specific
runtime directory, so resetting keys cannot mix a previous account's decisions
or profit-ratchet history into the competition record.

The dashboard's latest-position lifecycle joins the persisted deterministic exit
reason to broker-owned orders and positions through MCP. It shows the trigger,
high-water/floor evidence, parent close fill, gross locked P&L, and whether Alpaca
confirms the entire account is flat; it never labels a local submission as a fill.

After a profitable close, the exited pair remains under MCP quote observation.
The agent waits 30 minutes and requires 10 calm, liquid minutes before it may
ask Poe for one new idea. The re-entry gate then requires post-cost EV per defined-risk
dollar to beat the prior entry by 25%, with no worse delta, strike buffer, or
liquidity and no identical OCC pair. Tournament re-entry uses 100% of live options
buying power, matching the initial sizing objective. Risk exits lock re-entry for
the rest of the session, and this lifecycle
state is persisted across process restarts.

Poe's activity page may show two calls near one timestamp. That is a bounded
decision window: an invalid, unbuildable, or economically vetoed intent may receive
one reasoned revision. An economic revision must materially diversify DTE,
underlying, or strategy; broker/session failures receive no retry. The loop starts another window
only after its next `PACAPOUNCE_SESSION_POLL_INTERVAL_SEC` MCP refresh says a new entry
is still allowed. The risk monitor never creates entries.

The dashboard still converts the 8% annual benchmark to a geometric
252-trading-day target against Alpaca `last_equity`—about $30.54 per day at
$100k. In hackathon mode it is measurement only. Quantity instead uses 100% of
the broker-reported options buying power after every other gate survives. The
agent does not treat Alpaca's equity `multiplier` as options capital: options use
their separate broker field and the maximum loss of the protected spread.

## Does the gate actually work?

A gate that has never been tested against outcomes is an assertion, not a control.
`scripts/validate_gate.py` scores the trades the gate **rejected** as well as the ones
it approved - the counterfactual every naive version skips - and asks whether the
split beats chance.

It is deliberately non-circular: the gate decides using *implied* information
(delta, from implied vol), while outcomes are generated from a separate *realised*
vol linked only through a noisy variance risk premium. The gate never sees the path.

```
12,000 independent trade opportunities, seed 42

approved  2,937   vetoed  9,063   pass rate 24.5%

mean P&L, approved trades    $+8.23
mean P&L, vetoed trades      $-0.47
separation                   $+8.70 per trade
permutation p-value           0.0040   (2,000 shuffles)

Total P&L by policy:
  trade everything            $+19,901
  operational gates only      $ -1,104     <- rejects winners, misses losers
  full gate (with economic)   $+24,175
```

Reproduce: `python scripts/validate_gate.py --trials 12000 --perms 2000`

**The operational-only row is the finding.** A conventional safety stack - defined
risk, size caps, liquidity, limit orders - performs *worse than no filter at all*,
because it rejects trades for being large rather than for being underpaid.

### Effect size matters more than the p-value

| n | separation | p |
|---|---|---|
| 400 | +$25.12 | 0.131 |
| 1,000 | +$13.19 | 0.145 |
| 4,000 | +$8.22 | 0.088 |
| 12,000 | +$8.70 | **0.004** |

The effect is real and **modest**: about +$8.70 per trade, requiring roughly 12,000
observations to establish. That has a direct consequence worth stating plainly - a
week of paper trading yields perhaps ten trades, which is nowhere near enough to
demonstrate an effect this size. Any agent showing a profitable week is showing you
variance. That is the reason to validate offline, and the reason this harness exists.

## Honest limitations

- **Options quotes are the `indicative` feed** unless `PACAPOUNCE_OPTIONS_FEED=opra` and
  an Algo Trader Plus subscription is active. Alpaca describes indicative data as
  derived approximations, not real OPRA quotes. Friction measured on it is an
  estimate, and this is disclosed rather than hidden.
- **The gate rejects negative expectancy, not losing trades.** A positive-EV trade
  can and will lose. The claim is about being correctly paid, not about winning.
- **Short paper-trading windows are dominated by variance.** A week of P&L is not
  evidence of edge, from this agent or any other.
- The offline chain is synthetic and clearly labelled; it never reaches a broker.

## Layout

```
run.py                     CLI: check / propose / trade / measure / loop / summary
veto/config.py             all thresholds, from .env
veto/llm.py                Poe client; reasoning-token aware
veto/regime.py             cached MCP returns, RV20, ATM IV, and IV/RV brief
veto/intent.py             intent schema + pre-chain coherence check
veto/mcp_client.py         Alpaca MCP stdio client
veto/session.py            MCP-owned session lifecycle and restart reconciliation
veto/builder.py            intent -> real contracts from the live chain
veto/gates.py              13 deterministic controls + the economic gate
veto/executor.py           multi-leg paper orders
veto/ledger.py             append-only verdict log
scripts/build_dashboard.py renders/watches the atomic live dashboard snapshot
tests/test_gates.py        gate tests, including the S13 case
tests/test_session.py      close, pending-order, restart-count, and AI-block tests
tests/test_regime.py       MCP feature calculation, selection, and prompt tests
```
