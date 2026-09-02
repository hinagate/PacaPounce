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

> **Competition paper account:** `PA3ZX2FIASSZ` · $100,000 start · options level 3.
> The judged P&L lives on this account; the dashboard banner verifies the live
> account number matches before showing results.

### Judge in 60 seconds

```powershell
cd PacaPounce
.venv\Scripts\python.exe run.py --summary                # ledger stats
.venv\Scripts\python.exe scripts\build_dashboard.py      # ignored live snapshot
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

## Second Paper strategy: NDX30 long-call mean reversion

The same `run.py --loop` process gets one deterministic second-strategy decision
at each decision window of a normal 16:00 session — 10:00 and 15:45 ET for the
competition — scanning 14 liquid Nasdaq underlyings and
ranks names whose completed 15:30 bar is above a rising SMA200 while Wilder
RSI(2) is below 10. Alphabet share classes are issuer-deduplicated. The best
surviving signal is expressed as a single long call—never as stock. The
credit-spread lane holds back a 5% slice of options buying power so this lane
can trade at all; without that reserve, full-buying-power sizing deployed
everything and the second strategy was structurally unable to place an order.

- Python selects a 14–30 DTE call from the live Alpaca chain, requires delta
  0.55–0.85 and relative bid/ask at most 6%, and submits a `buy_to_open` limit
  at the ask through Alpaca MCP. Among affordable contracts it takes the one
  with the cheapest carry, breaking ties toward 0.70 delta.
- One window may open several positions. Each is resolved, gated, and
  **independently reviewed by the LLM** against its own timestamped Alpaca news
  pull, so deploying a portfolio is a portfolio of separate AI decisions rather
  than one bulk allocation.
- Quantity comes from the active sizing objective. `risk_budget` targets 0.5% of
  equity at an underlying 2×ATR14 stop model; `tournament` sizes from the premium
  budget directly, because the competition scores the upper tail rather than the
  mean, but the modeled loss at the 2×ATR stop still binds at
  `OPTION_MR_MAX_STOP_RISK_PCT` (15% of equity per position). Sized on premium
  alone, what a position risked at its stop was whatever delta the chain
  happened to offer — 54% of premium at 0.8, roughly all of it at 0.6 — which is
  a risk decision made by the chain rather than by the policy. Either way the
  full premium paid is the legal maximum loss, and the portfolio premium budget
  and live options buying power both bind.
- `get_orders` must confirm the client ID and Alpaca broker order ID before
  SUBMITTED is recorded.
- The 30-second monitor sends a `sell_to_close` limit at the live bid when the
  underlying hits its 2×ATR stop, when the profit ratchet trips, or when the
  holding-session limit is reached. A close that has not filled protects
  nothing: a limit still open after 20 seconds is cancelled and resubmitted at
  the fresh bid, and after two unfilled limits the next attempt is a market
  order. Adopted positions that duplicate an issuer the lane already holds are
  closed after the opening rotation (`issuer_concentration`), keeping the
  lane's own entry. The LLM never selects the contract, size, or exit.
- **The ratchet exists for one failure mode.** Being wrong about direction is a
  probability outcome, and the 2×ATR stop already bounds it. Being *right* and
  ending flat is not a probability outcome — it is a missing control, and under
  a tail-seeking objective with no profit target it is the likeliest way to
  waste a correct call. After a position captures 15% of the premium paid, its
  executable high-water mark is trailed by 40% of the gain, tightened to 25%
  when P&L volatility is elevated, and closed after two consecutive breaches so
  one bad quote cannot exit. Because arming requires a positive capture and the
  giveback is below 1, **an armed floor is always above breakeven**; the code
  clamps it at zero as well so no configuration can break that. The trail is
  measured at the live bid, not the midpoint, so the floor is a number the
  position can actually realise. Once armed the floor is a hard exit whatever
  the mark's sign: a quote that gaps from above the floor to below zero is the
  breach the floor exists for, not an exemption from it (an earlier form of
  the rule required a positive mark for a breach to count, which silently
  handed a gapped winner back to the 2×ATR stop). "Elevated volatility" is the
  dispersion of recent marks as a multiple of the P&L a one-ATR move would
  produce, so it means the same tape for every contract rather than reading
  leverage. The two-confirmation requirement means the close lags the floor
  by two observations — a bounded cost of not exiting on a single tick.
- LLM sees the top numeric candidate plus timestamped Alpaca `get_news` output.
  It may veto a concrete news/event risk and writes the thesis. `get_news` is
  **not** presented as a verified earnings calendar; this limitation remains
  explicit on the dashboard.

### The fourth edge, and how it died

The frozen 2024 SIP OOS **underlying-signal proxy** produced 146 trades, +4.98%,
profit factor 1.394, Sharpe 1.353, and 53.4% wins. A 2026 YTD rerun agreed: 108
trades, +5.30%, profit factor 1.622. The signal is real.

Then the same 2026 window was rebuilt on **historical Alpaca option bars**, so
the measurement was option P&L rather than a stock proxy — 51 complete
long-call lifecycles, 47 with a computable size:

| One-way friction | Trades | Win rate | Return | Profit factor |
|---|---:|---:|---:|---:|
| 0% (crossing ignored) | 47 | 51.1% | **-0.02%** | 0.997 |
| 1% | 47 | 51.1% | -1.13% | 0.866 |
| 3% | 46 | 50.0% | -2.46% | 0.711 |
| 7.5% | 46 | 34.8% | -7.30% | 0.337 |

**Even with crossing costs set to zero, the long call returns nothing.** The
arithmetic says why. A measured live contract — AAPL, 18 DTE, 0.69 delta,
13.49/13.95, spot 313.89 — carries $133.60 per contract over a three-session
hold: $46.00 of crossing and $87.60 of theta. That needs a **+0.61%** underlying
move before the trade has an opinion. The signal's measured mean move is
**+0.795%**. The margin is 1.3x on the most liquid contract on the board.

Buying a call pays the same variance risk premium the primary lane collects. So
the signal is promoted and its long-call expression is not: the lane is funded
from a 5% reserve rather than a real allocation, and the 15% relative-spread
gate — which a live 167-contract chain scan showed was admitting contracts whose
**bid/ask alone exceeded what the signal could pay, 48% of the time** — was
replaced by a 6% ceiling plus a per-contract carry test.

That is the same economic gate this project applies to its primary strategy,
now turned on its own second strategy. The machine-readable card is
[`data/ndx30_option_mr_validation.json`](data/ndx30_option_mr_validation.json);
it records the evidence boundary and that research made zero order calls.

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

The credit-spread lane uses `full_buying_power`: after a spread passes the
economic model, quantity is the largest integer count whose entire defined loss
fits inside its own equity budget, bounded by Alpaca's live
`options_buying_power`. The competition sets `PACAPOUNCE_SPREAD_EQUITY_PCT=0.10`
and `PACAPOUNCE_OPTIONS_BP_UTILIZATION=1.0`, so one approved spread may carry
about $10,000 of defined maximum loss on the $100,000 account. The result records
source buying power, the lane budget, quantity, total defined loss, and
utilization percentage.

This is tournament sizing, not a prudent production allocation — see
[Tournament mode](#tournament-mode-a-different-objective-stated-as-one) for why
the remaining capital goes to the convex lane instead of to a bigger spread. Use
`PACAPOUNCE_SPREAD_EQUITY_PCT=0.95` or `PACAPOUNCE_SIZING_MODE=kelly` outside the
competition.

This is tournament sizing, not a prudent production allocation. It maximizes
scored exposure only after all deterministic gates survive, but a single
maximum-loss outcome can still consume almost the entire paper account. Use
fractional utilization or `PACAPOUNCE_SIZING_MODE=kelly` outside the competition. The
dashboard discloses the active 100% allocation and its downside explicitly.

Every sized position reports its full outcome distribution, not just its mean. A
point estimate is close to useless when the spread of outcomes dwarfs it.

### Why the long call is bought in-the-money

The lane's delta band is 0.55-0.85 and the carry ranking pushes it to the top of
that range: the GOOG call it actually bought on 2026-08-31 was 0.807 delta, 18
DTE, with 82% of its premium already intrinsic. That is a choice, and it costs
something real, so it is worth stating what it buys and what it gives up.

Measured across the exact chain that trade came from — GOOG at 335.87, the
2026-09-18 expiry, $33,300 deployed, held five sessions:

| delta | strike | ask | extrinsic | needs | -5% | +5% | up/down |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.917 | 310 | 28.05 | 8% | +0.63% | -19,143 | +16,405 | 0.86 |
| **0.815** | **320** | **19.25** | **18%** | **+0.73%** | **-23,282** | **+21,899** | **0.94** |
| 0.647 | 330 | 11.30 | 48% | +0.51% | -26,601 | +35,608 | 1.34 |
| 0.491 | 338 | 7.45 | 100% | +0.98% | -28,802 | +41,040 | 1.43 |
| 0.257 | 350 | 2.96 | 100% | +0.61% | -31,050 | +61,771 | **1.99** |

**The out-of-the-money contracts have a better upside-to-downside ratio, and the
lane does not buy them.** Three reasons, in order of weight.

**1. The signal does not predict large moves.** Its measured mean is +0.795% per
trade. An out-of-the-money call only pays when the move is big; buying it is
buying convexity in a distribution the signal says nothing about. The +5% column
is not a forecast — the signal has no claim on it. Paying 100% extrinsic for
exposure to an outcome you cannot predict is buying a lottery ticket, not
harvesting an edge.

**2. The risk model is defined on the underlying, and only a high delta tracks
it.** Position size comes from `2 x ATR14 x delta x 100`, and the stop is an
underlying price. Both assume delta is roughly stable over the move being
modelled. At 0.82 delta that holds. At 0.26 delta it does not: delta collapses
as the trade goes against you, so the modelled loss at the stop understates the
real one, and the stop stops being a meaningful control.

**3. It has to survive being wrong.** At -5% the 0.82-delta call keeps 30% of the
premium because most of it was intrinsic; the 0.26-delta call keeps 7%. Over a
five-session window that runs a strategy at a third of the account, the
difference between losing 70% and losing 93% of a position is the difference
between a bad day and a structural one.

What it gives up is real and unhedged: at +5% the traded contract makes $21,899
where a 0.26-delta call would have made $61,771. If the objective were purely
to maximise the right tail with no regard for the risk model, out-of-the-money
would win. The lane is not configured that way, and the honest reason is that
its edge is a small mean reversion, not a large move.

One caveat on the `needs` column: it is a delta-linear estimate of the move
required to cover crossing plus theta, so it understates what a low-delta
contract really needs. Gamma flatters it on the way up and punishes it on the
way down, and neither shows in a first-order number. The carry gate is therefore
not what excludes out-of-the-money contracts — the delta band is.

## Tournament mode: a different objective, stated as one

Everything above maximizes **expected value**. The competition does not score
expected value — it ranks four sessions of P&L. Those are different objectives,
and optimizing the first cannot win the second.

A credit spread's payoff shape is the reason. It wins about 6% of the capital it
risks and loses about 94%. Sized at the whole account it produces roughly
+$3–6k in a good week and -$60k in a bad one. **It cannot produce a placing
number at any size.** The upper tail is closed by the structure itself.

So for the competition window the agent runs a disclosed second objective:
maximize the upper tail, not the mean.

Measured on the live chain across the 14 tradeable names, holding five sessions
with $70,000 of premium deployed on a $100,000 account — repriced with each
contract's own IV and exited at the bid:

| Underlying move | Account P&L |
|---|---:|
| -5% | **-33,700** |
| -3% | -23,300 |
| **flat** | **-5,500** |
| +3% | +14,400 |
| +5% | **+28,400** |

**The flat row is the one that matters most**, because a small move is the modal
five-day outcome and -5.5% is the rent this configuration pays for its convexity.
The ±5% rows are conditional scenarios with no probability attached; they are
not forecasts. Gamma is excluded (it helps the upside) and so is IV change (a
crush hurts it).

Deeper-in-the-money and longer-dated bands were measured and rejected: they cut
the flat bleed roughly in half but cut the upside by about the same, so they are
leverage dials rather than improvements. The 14–30 DTE / 0.55–0.85Δ band had the
best upside-to-downside ratio of the six tested.

The configuration that follows from this is two equity budgets rather than one:

```
PACAPOUNCE_SPREAD_EQUITY_PCT=0.10          10% of equity as defined spread loss
PACAPOUNCE_OPTION_MR_TOTAL_PREMIUM_PCT=0.70   70% of equity as long-call premium
PACAPOUNCE_OPTION_MR_SIZING_MODE=tournament   size from premium, not the ATR stop
PACAPOUNCE_OPTION_MR_MAX_STOP_RISK_PCT=0.15   ...but at most 15% of equity at the stop
```

Each lane's share is taken from **equity**, not from whatever buying power is
left, so a lane's allocation does not depend on which lane filled first.

The universe is restricted to the 14 NDX names whose option chains are actually
tradeable at 6% relative spread. Measured over ten sessions, leaving the other
16 in meant the signal regularly fired on names that could never be bought: on
2026-08-28 it produced four candidates — MDLZ, AMGN, CSCO, GILD — and zero
deployable premium, because the lowest-RSI names in this universe are also the
least liquid ones. Restricting it does not create signals on days like that one;
it makes the ranked candidate list actionable instead of top-heavy with names
the builder will always reject.

Deployment coverage was then measured properly, over 120 sessions rather than
ten, and it is the weakest part of this configuration. Only **72.5%** of
sessions produce a candidate at all, and misses **cluster**: the probability of
a miss is 53% after a miss against 17% after a hit, and the sample contains a
nine-session drought. Assuming independence would have put two-session coverage
at 92%; the observed figure is **85.7%**.

That clustering is why the lane scans twice a session (10:00 and 15:45) instead
of once. A single 15:45 scan sees only the completed 15:30 bar, so a name that
is oversold in the morning and recovers into the close is never seen at all, and
a session without a 15:30 signal deploys nothing.

**Three things this is not.** It is not a claim that long calls have positive
expected value here — [the measured evidence](data/ndx30_option_mr_validation.json)
says they do not, and the honest expectation for this configuration is a small
negative mean with a fat right tail. It is also a **different strategy from the
one that was validated**: the 2024/2026 studies used RSI(2)<10, a three-session
hold, and risk-budget sizing, so their profit factors do not transfer to this
configuration and are not offered in support of it. It is not a removal of the gates: every
entry still clears the deterministic stack, the carry test, and an independent
AI event-risk review. And it is not the default — `SPREAD_EQUITY_PCT=0.95` with
`OPTION_MR_SIZING_MODE=risk_budget` restores the expected-value configuration.

The `tournament` sizing objective is named rather than smuggled in. The
alternative would have been to inflate the 0.5% ATR risk budget to ~17% and keep
calling it a risk budget, which would have been the same position with a
dishonest label.

## Architecture

```
  MCP session supervisor ..... clock + calendar + orders + positions + account
        |                       sleeps to MCP next_open; rolls across sessions;
        v                       pending locks, entries, Level 3+ eligibility,
                                blocks, and live options buying power
  MCP regime brief .......... live spot + completed daily bars + nearest ATM IV
        |                       1D/5D returns, RV20, IV/RV, quote timestamp;
        v                       cached 5 minutes, missing fields stay missing
  LLM (gemini-3.7-flash)
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
                               append-only, stamped with the gate version

  options MR lane (10:00,15:45)  SIP D1 + completed 15m bars -> SMA/RSI/ATR/EMA
        |                       one top underlying; options capital must be clear
        v
  Alpaca news + LLM review .... event-risk veto/thesis only; no fake earnings claim
        |
        v
  Long-call builder ........... live chain -> 14–30 DTE / ~0.70 delta / limit order
        |
        v
  Options monitor ............. ATR stop -> EMA5/3-session deterministic close
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
| Options-MR signal + event context | `get_stock_bars`, `get_news` |
| Long-call discovery + execution | `get_option_chain`, `get_option_latest_quote`, `place_option_order`, `get_orders` |

There is no direct Alpaca REST fallback in the runnable agent. An incomplete MCP
snapshot fails closed. Paper account only — `ALPACA_PAPER_TRADE=true` is pinned
in `veto/mcp_client.py` and the trading base URL is hard-coded to `paper-api`.

`get_account_info` is also a trade authorization input, not merely a dashboard
statistic. Before LLM is called, and again immediately before order submission,
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
cp .env.example .env          # add LLM_KEY and Alpaca PAPER keys

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

python scripts/build_dashboard.py          # render ignored dashboard/runtime/index.html once
python scripts/build_dashboard.py --watch  # standalone 60-second live rebuild
pytest tests/ -q
```

PacaPounce loads only `PacaPounce/.env`; it never falls back to an `.env` in a
parent directory. This prevents another project from silently changing the
paper account, model, market-data feed, or risk policy.

The monitor reconstructs credit spreads and the second strategy's long-call
lifecycle from Alpaca's live option positions and polls every 30 seconds. It
batches independent MCP requests and measures both midpoint and immediately
executable P&L. The entry gate prices a spread on its terminal payoff, and on a
$2-wide spread the 50% profit target and the trailing ratchet were closing the
winners early while the losers ran to the breach — simulated on the live SPY
770/772C the configured monitor was −$520 expected against +$587 for holding to
expiry with the long-strike breach still armed. The competition therefore runs
with `PACAPOUNCE_MONITOR_PROFIT_EXIT_ENABLED=false`: the spread is held to
expiry and closed at market only if the underlying crosses the long strike, and
the spread lane's share of equity is halved to 10% because the trail no longer
trims its loss tail. With profit exits on, the monitor takes profit after 50% of
the opening credit is captured and a restart-safe profit ratchet arms after 20%
capture and closes after two confirmed observations give back 20% of the
executable high water; high P&L volatility tightens that trail to 10% but never
closes by itself, and a change dispersion inside 1.5 ticks per contract is quote
flicker rather than volatility. The monitor also applies the repository's
70%-of-max-loss guard on non-expiry days. Expiry-day stop-loss exits are
suppressed; only late pin risk between the strikes forces a close. A standalone monitor requires `--execute`
before it may mutate; the explicitly mutating `run.py --loop` command enables its
bundled paper monitor. Both paths check the MCP environment and trade URL for
paper mode at startup.

For managed long calls, the same monitor checks the underlying against the 2×ATR
stop throughout each regular session. At the daily decision window it uses
completed SIP bars to test EMA5 recovery and the Alpaca calendar to count normal
holding sessions. Every triggered exit is a sell-to-close option limit at the
live bid, reconciled by client ID and Alpaca broker order ID.

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
hunting enters a broker-backed wait state before LLM is called; the risk monitor
continues through the closing bell. `Ctrl+C` cleanly stops both processes.

The bundled entry loop is supervised by Alpaca MCP. Before any LLM request,
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
The agent waits 15 minutes and requires 10 calm, liquid minutes before it may
ask LLM for a new idea, and one profitable exit may earn up to three same-day
re-entries. The re-entry gate still requires post-cost EV per defined-risk
dollar at least matching the prior entry, with no worse delta, strike buffer, or
liquidity and no identical OCC pair — every attempt clears the full economic
gate, so relaxing the old arbitrary 25%-improvement bar admits more trades
without admitting worse ones. Tournament re-entry uses 100% of live options
buying power, matching the initial sizing objective. Risk exits lock re-entry for
the rest of the session, and this lifecycle
state is persisted across process restarts.

LLM's activity page may show two calls near one timestamp. That is a bounded
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
veto/llm.py                LLM client; reasoning-token aware
veto/regime.py             cached MCP returns, RV20, ATM IV, and IV/RV brief
veto/intent.py             intent schema + pre-chain coherence check
veto/mcp_client.py         Alpaca MCP stdio client
veto/session.py            MCP-owned session lifecycle and restart reconciliation
veto/builder.py            intent -> real contracts from the live chain
veto/gates.py              13 deterministic controls + the economic gate
veto/executor.py           multi-leg paper orders
veto/ledger.py             append-only verdict log
scripts/build_dashboard.py renders/watches the atomic live dashboard snapshot
at `dashboard/runtime/index.html`. That directory and all account-scoped logs
are gitignored. The tracked `dashboard/index.html` is a frozen Day-0 artifact
for GitHub Pages, so running the agent never dirties or leaks into the public repo.
tests/test_gates.py        gate tests, including the S13 case
tests/test_session.py      close, pending-order, restart-count, and AI-block tests
tests/test_regime.py       MCP feature calculation, selection, and prompt tests
```
