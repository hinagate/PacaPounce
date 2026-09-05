# PacaPounce: post-hackathon, two-lane live-readiness review

Review date: 2026-09-05 JST / 2026-09-04 ET. Reviewed source commit: `e842986527b68f73ee36e467716061fe9a88c0f6`.

## Verdict

**NO-GO for live capital with the current implementation and configuration.** Both lanes remain paper research. There are independently reproducible execution and monitoring failure paths, and neither lane has adequate validation of its currently deployed policy's net expectancy. A profitable account snapshot does not resolve either requirement.

The architecture is a useful hackathon prototype: Python retains contract and sizing authority, gates are auditable, the LLM has bounded authority, and broker identity and paper-mode checks exist. Preserve those boundaries. Do not remove the paper guard or treat live migration as a credentials change.

This review used source inspection, account-scoped broker reads, existing tests, and focused pure-function/in-memory probes. The existing suite passed: **188 tests**. Probes did not submit broker orders. No strategy, risk configuration, trading processes, or broker orders were changed. The authorized final dashboard refresh updated the local frozen public HTML and local runtime HTML; it was not pushed or deployed to a hosted site. This document is a review, not an implementation of its recommendations.

The organizer now identifies the August 28–September 4 event as finished. That establishes event dates, not certification of trading performance or live readiness. [Hackathon recap](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).

## Final snapshot and performance attribution

Broker snapshot: **2026-09-04 22:52:12 ET**; dashboard rendered at **22:52:15 ET**. The configured paper account identity was verified. These figures describe that capture, not a continuously refreshed guarantee.

| Account measure | Captured value |
| --- | ---: |
| Starting capital | $100,000.00 |
| Equity / cash | $106,410.48 |
| Total account P&L versus start | **+$6,410.48 / +6.41%** |
| Daily P&L versus prior close | +$4,443.11 |
| Options buying power | $94,390.92 |
| Open positions / working orders | **0 / 0** |
| FIFO realized P&L from fills, before account adjustments | +$6,447.00 |

The result must be separated by origin. Parent opening orders, their client IDs, normal decision records, and matched broker fills give:

| Origin | Filled opening trades | Gross realized P&L |
| --- | ---: | ---: |
| Normal credit-spread lane | 2 | **-$5,403.00** |
| Normal long-call mean-reversion lane | 4 | **+$10,130.00** |
| Historical test-origin orders | 4 | **+$1,720.00** |
| All origins | 10 | **+$6,447.00** |

Normal spreads: SPY September 3 768/773 calls **+$738**; SPY September 4 770/772 calls **-$6,141**. Normal long calls: GOOG **+$3,690**, AMD **+$3,540**, INTC **+$1,140**, AAPL **+$1,760**.

The four filled test-origin trades produced +$2,100, +$1,170, -$800, and -$750. A fifth test-origin opening order was canceled without filling. Their client IDs use the fabricated `20260826` test date despite September 1 submission. `tests/conftest.py:10` explicitly documents that incident. These were actual orders on the **paper** account, not real-money trades. Current fixtures block broker sessions and isolate runtime paths; this is historical contamination, not a claim that today's test run traded.

Subtracting the test-origin contribution leaves **+$4,727 gross** attributed to normal entries. That is arithmetic attribution, not the return a hypothetical clean account necessarily would have earned: buying-power interactions could change later trades.

The difference between gross fills and account P&L is **$36.52**. Retrieved non-trade activities include **$35.63 in fees** and the original $100,000 funding entry; **$0.89 remains unreconciled** in the retrieved records. Do not label the whole difference as verified fees. Per-lane figures above are deliberately gross; no unsupported fee allocation was invented.

Dashboard improvement: distinguish account return, normal-strategy return, test/manual activity, fees, and unreconciled adjustments. The displayed six normal entries and 33 broker fill records are different counting units. A fill is not necessarily a complete trade. Keep the equity-based total P&L as the account headline and label fill-based totals as gross.

Alpaca documents that paper fills do not model market impact, latency slippage or queue position, and can exceed displayed NBBO size. Consequently, large paper fills do not establish executable live capacity. [Alpaca paper-trading assumptions](https://docs.alpaca.markets/us/docs/paper-trading).

## Findings: shared execution and supervision

Priority convention: **P1** means a blocker before live use; **P2** means an important correctness/research issue. Severity concerns a reachable failure or missing safety property, not proof that it happened in the hackathon.

### E1 — P1: submission success is not consistently broker-confirmed

Locations: `veto/executor.py:37`, `run.py:313`, `veto/mean_reversion.py:1235` and `:1703`.

Initial spread submission unconditionally returns `submitted=True` after the MCP call. A mocked `{"error": "rejected"}` response is recorded as submitted. There is no durable pre-submit record for an ambiguous timeout, and a fresh random client ID is generated for a subsequent attempt.

Long calls do perform order reconciliation, but store their managed record only after submission and successful verification. Acceptance followed by a lost response, reconciliation failure, or process crash can leave an actual position without a durable owner. Spread close lifecycle recording also precedes confirmed completion.

Required change: one execution state machine shared by both lanes; persist intent and stable client ID **before** sending, reserve its risk, distinguish UNKNOWN from REJECTED, and reconcile broker order IDs, cumulative fills and net position before declaring a lifecycle complete. Never blindly retry an uncertain opening order with a new identity.

Acceptance: rejection, timeout-after-acceptance, crash-after-send, delayed visibility and restart tests all recover exactly the intended exposure without duplicate submission or abandoned ownership.

### E2 — P1: spread cancel/replace can exceed intended exposure

Location: `scripts/monitor.py:839`.

The opening-order chaser starts from original `qty`, does not subtract cumulative fills, requests cancellation without waiting for terminal cancellation, and can resubmit the original quantity. A fill racing cancellation can leave old fills plus the replacement. Its refreshed buying-power check does not repeat the complete lane risk gate; available broker buying power is not the same as the configured spread risk budget.

Required change: confirm cancellation, reconcile final fills and positions, replace only remaining quantity, and rerun the complete current-price risk checks under a shared risk reservation. Test partial fills before, during and after cancellation, including a lost cancel response.

### E3 — P1: concurrent processes can lose portfolio-state updates

Locations: `run.py:477` and `:668`; `veto/mean_reversion.py:79`, `:931`, `:1432` and `:1737`; `veto/risk_state.py:59`.

The entry process and monitor both load/modify/replace whole JSON state files. Atomic replacement prevents a torn file, not lost updates: a slow entry scan can overwrite an exit or ratchet update, and a monitor can erase a newly entered record. Risk-state writes also share a fixed temporary filename. There is no account-scoped execution singleton.

Required change: a single account execution owner plus transactional state and an append-only event journal; SQLite with short transactions/version checks is one possible implementation. Add competing-writer, duplicate-process and crash-recovery tests. Corrupt or unavailable state must not silently reset risk history.

### E4 — P1: process liveness is mistaken for protection health

Locations: `run.py:486` and `:690`; `scripts/monitor.py:1310`; `veto/mcp_client.py:56`.

Entry supervision tests whether the monitor process exists. The monitor can repeatedly catch errors and remain alive, or stall in a call, while entries remain permitted. There is no sufficiently strong successful-cycle heartbeat or explicit per-call application deadline. The autonomous loop can resume on later sessions; the event ending is not a built-in shutdown condition.

Required change: timestamped heartbeat proving successful account/order/position reconciliation, fresh data, and coverage of every exposure; fail closed for **new entries** when unhealthy. Add deadlines, escalation alerts and a deliberate recovery policy for existing exposure. A daily loss/drawdown circuit breaker needs a separate, explicit liquidation policy rather than automatically issuing blind market orders during an outage.

### E5 — P1: unavailable broker state can be interpreted as flat

Locations: `veto/mcp_client.py:451`, `scripts/monitor.py:74` and `:980`, `veto/mean_reversion.py:1471`.

The MCP unwrap path ignores `isError`. Plain-text error responses can survive dictionary-only error checking and decode to an empty row list. Long-call reconciliation then treats an absent position as closed. An in-memory error-envelope probe reproduced the error-to-empty conversion. Explicit dictionary errors are handled on the main path; the defect is malformed/text/error-envelope handling, not every possible API error.

Required change: check tool error flags and validate required response schemas and completeness. UNKNOWN portfolio state is not EMPTY portfolio state. Never close ownership records or release risk on an unsuccessful portfolio read.

### E6 — P1: freshness does not survive the whole decision lifecycle

Locations: `veto/builder.py:303`, `veto/gates.py:273`, `run.py:288`, `veto/mean_reversion.py:1152` and `:1231`, `scripts/monitor.py:254`, `veto/mean_reversion.py:1314`.

Entry quote ages are stored as numbers and do not increase during chain processing, news retrieval or LLM review. Rechecking the same numeric age is not refreshing the quote. Monitor quote parsers discard exchange timestamps; positive bid/ask alone can be accepted from a stale response. Focused probes accepted quotes dated 2000 or the previous session. Missing option quotes also suppress the spread underlying-breach action before it is evaluated.

Required change: retain source timestamps, re-read selected-contract quotes and account/clock/exposure immediately before sending, and recalculate sizing and economics. Validate underlying and option observations independently. Specify a degraded-data protection procedure, including bounded quote acquisition, alerts, and a deliberately chosen emergency execution policy.

### E7 — P1: expiry and assignment are not fully managed

Locations: `scripts/monitor.py:371`, `:164`, `:204`, `:993` and `:1049`.

On expiry day, an early return suppresses the normal long-strike breach and loss stop. Pin handling uses a fixed 15:30 cutoff, not the actual session close. Probes produced no action for an expiry-day 100%-loss spread beyond the long strike at 15:45, and for an early-close expiry between strikes at 12:45. Unmatched short legs are reported but not actively recovered; assigned stock is filtered out of the monitored option exposure.

This is more than payoff algebra: American short calls can be assigned before expiry, particularly around ex-dividend dates. Alpaca automatically exercises qualifying ITM contracts, may liquidate positions for exercise buying-power risk, and requires polling for assignment activities. [OIC bear-call-spread risks](https://www.optionseducation.org/strategies/all-strategies/bear-call-spread-credit-call-spread), [Alpaca options lifecycle](https://docs.alpaca.markets/us/docs/options-trading).

Required change: exchange-calendar-relative expiry controls, explicit treatment of assignment/exercise and resulting shares, unmatched-leg escalation, and a documented operator recovery runbook. An initial live design should avoid carrying positions into expiry until that lifecycle is demonstrably supported. Entry blocking on unexpected stock does not manage the stock already held.

## Lane 1: SPY/QQQ credit spreads

The intended sequence is: bounded directional proposal; resolve actual same-expiry option contracts; calculate credit/max loss and model expectancy; apply operational, risk and economic vetoes; submit a multileg order; monitor the position. The separation of proposal from deterministic permission is worth preserving.

Passing 15 of 16 gates does **not** mean a trade is nearly approved. Gates are conjunctive: one failed mandatory condition means no entry. Operational permission and available buying power do not create positive expected return. The displayed 8%-annual target is a benchmark under current full-buying-power mode, not a daily-profit stop or obligation to trade.

### S1 — P1: the smile model does not satisfy its claimed repricing invariant

Location: `veto/skew.py:119`, especially `:139`; entry use at `veto/gates.py:159`.

The code evaluates the flat-volatility Black-Scholes tail formula separately at each strike's implied volatility and integrates it. For a strike-dependent smile, that is not generally the distribution implied by the option price surface: differentiating the price also requires the smile-slope contribution. The comment that ratio=1 reproduces the chain is therefore false in general. This affects both entry EV and model-based hold/exit review.

A pure numerical check built an arbitrage-free, equally weighted mixture of zero-drift lognormal distributions with 10% and 50% volatility, spot 100 and T=30/365, and inverted its prices into a strike smile. With ratio=1, drift=0 and no friction:

| Vertical | Actual model credit/share | Code expected loss/share | Spurious EV/contract |
| --- | ---: | ---: | ---: |
| 95/90 put | 0.818170 | 1.208235 | -$39.01 |
| 105/110 call | 0.734829 | 1.122297 | -$38.75 |

Required change: first implement an arbitrage-consistent surface/distribution with a repricing invariant, then separately specify and validate any physical-measure volatility/drift adjustment. Add surface-shape, probability-monotonicity, bounds and repricing tests. This finding does **not** prove the user's particular QQQ veto should be reversed; corrected fresh inputs would need a new calculation.

### S2 — P1: stale or missing model inputs can retain trading authority

Locations: `veto/builder.py:87` and `:112`; `veto/gates.py:187`; `scripts/monitor.py:718`.

The symbol-keyed EWMA cache has no session/TTL invalidation in a multi-session entry process. Recent-return inputs used by the rebound gate can also remain stale. Missing spot/DTE/realized-vol inputs invoke an `implied_only` fallback rather than an explicit model-readiness veto. Separately, the monitor's missing-smile or nonpositive-DTE path returns credit minus friction without modeled loss: a positive-DTE, missing-smile probe with $0.30 credit returned +$30 before friction.

Required change: version and timestamp inputs; refresh on completed sessions; gate on model readiness; never interpret missing model inputs as favorable EV. Treat expiry payoff separately from missing data, using actual underlying/strike exposure.

### S3 — P2: time conventions and structural validation need tightening

Locations: `veto/builder.py:203`; `veto/gates.py:159`, `:214` and `:243`; `scripts/monitor.py:782` and `:801`.

Calendar DTE is divided by 252 while historical volatility is annualized on trading sessions. Monitor calculations also use host-local `date.today()`; on a Tokyo host that can become the next date during the US session. Use broker/exchange-aware timestamps and an internally consistent variance clock, including holidays and remaining intraday time. Simply substituting 365 everywhere without reconciling volatility units is not a complete fix.

The independent gate checks declared long/short counts and an upper quantity bound, not the full OCC structure or positive integral quantity. Synthetic otherwise-passing candidates with quantity 0 or -1, a mismatched long symbol, or credit greater than width were accepted. Normal construction provides additional constraints, so these are defense-in-depth defects, not claims that every case is normally proposed. Independently validate matching root/right/expiry/multiplier, protective strike direction, finite prices, `0 < credit < width`, and positive integer size.

### S4 — P1 promotion gap: current-policy validation is absent

Locations: `data/gate_validation.json`, `scripts/validate_gate.py:95`, `veto/config.py:524`.

The committed 12,000-trial gate artifact describes version 1.2.0; runtime is 1.4.0. The synthetic generator omits current model inputs and account equity required by the risk budget. A 100-trial seed-42 read-only probe under current configuration produced zero approvals in both policies. Its old synthetic significance claim is not validation of today's trading strategy.

Rebuild tests against a frozen current configuration and validate actual option-level outcomes on unseen data, including stops, chasing, hold-EV exits, reentry and costs. A terminal-payoff EV model alone does not validate a policy that frequently exits early. Test sensitivity to the 8% drift assumption, realized-vol estimation, regime changes and model uncertainty; a net EV threshold of merely greater than zero supplies no estimation-error margin.

Selection improvement after model repair: the builder currently resolves near requested delta/width and the first buildable expiry, not the best admissible opportunity across a declared candidate set. Use bounded deterministic ranking with an abstain outcome. Do not search increasingly many contracts merely to find a model-estimated positive number or weaken economic gates to increase trade count.

## Lane 2: long-call mean reversion

The lane scans a trend-filtered stock universe for short-term oversold signals, chooses a real 14–30 DTE call near 0.70 delta, checks execution/carry/risk constraints, and applies a bounded news/LLM review. Python retains sizing authority. The four normal paper winners are encouraging observations, not sufficient evidence of net option alpha.

### M1 — P1: partial entry fills can become permanently unmanaged

Locations: `veto/mean_reversion.py:1439`, `:1471` and `:1552`.

Reproduced in memory:

`buy 10 -> 1 fills, 9 still working -> stop closes 1 without canceling the remaining buy -> record CLOSED -> remaining 9 fill -> CLOSED record excluded from management`.

Required change: track opening orders, closing orders and net exposure independently. Cancel/reconcile the opening remainder before liquidation, and retain ownership until exposure is zero **and every associated order is terminal**. Test delayed fills and restart at every transition.

### M2 — P1: a canceled time exit can be abandoned for the session

Locations: `veto/mean_reversion.py:1534`, `:1620` and `:1714`.

The initial time/EMA exit sets `last_exit_check_date`. If the close is later canceled, state returns to `open`, but the same-day guard suppresses re-evaluation. A max-hold exit canceled at 15:46 produced no replacement. Existing stop-based chase coverage misses this because a breached stop continues to trigger. Missing signal bars can also prevent checking elapsed holding time.

Required change: a persistent liquidation intent that remains active until broker-confirmed flat; replacement independent of the original signal recurring or a same-day signal-check limit. Time/expiry exits must not depend on optional indicator data.

### M3 — P1: signal completeness does not establish current data

Locations: `veto/mean_reversion.py:578`, `:604` and `:999`.

The intraday selector accepts the latest completed bar from any time today. A 15:46 probe with sufficient daily history but only today's 09:30 bar still produced a passing RSI=5 signal. Counting successfully computed symbols does not prove the current decision window is complete.

Required change: enforce the expected completed-bar boundary for each decision window, with explicit calendar/continuity checks and a safe late-data policy. Combine this with the final pre-submit refresh and timestamp-aware exits described in E6.

### M4 — P1 promotion gap: existing option evidence is negative and does not match deployment

Location: `data/ndx30_option_mr_validation.json:3`.

The validation card explicitly says `PAPER_STAGING_OPTION_EXPRESSION_NOT_VALIDATED` and `DO_NOT_PROMOTE_LONG_CALL_EXPRESSION_ON_ITS_MEASURED_RETURN`. Its reconstruction covers 51 of 108 signal lifecycles (47.22%), using option bars rather than historical executable quotes:

| One-way modeled friction | Executed trades | Reconstructed return |
| --- | ---: | ---: |
| 0% | 47 | -0.021% |
| 1% | 47 | -1.1344% |
| 3% | 46 | -2.4582% |

These incomplete scenarios do not establish inevitable losses, but they do not support positive net option expectancy. Positive stock-signal returns cannot substitute for option-return validation.

| Policy input | Frozen validation | Current local configuration |
| --- | --- | --- |
| Universe | 30 symbols | 12 symbols |
| RSI(2) entry threshold | Below 10 | Below 25 |
| Decision windows | 15:45 ET | Seven windows from 10:00 through 15:45 ET |
| New entries/day | 1 | 2 |
| Holding limit | 3 normal sessions | 5 sessions |
| EMA5 recovery exit | Enabled | Disabled |
| Carry allowance | 0.795% measured move | 3x that allowance, 2.385% |
| Sizing | Small modeled-risk allocation | Tournament |
| Per-position premium cap | 20% | 35% |
| Lane total premium cap | Small reserve | 70% of equity |
| Modeled stop-risk sizing | 0.5% target, bounded discrete exception | Up to 15% of equity per position |

This is materially a new strategy, not just an operational update. Freeze the exact universe, thresholds, windows, contract ranking, sizing and exit rules before option-level walk-forward validation. Broader historical regimes, realistic fills, IV changes, fees, corporate actions and gap losses are required. Report missing-data selection bias and concentration, not only aggregate return.

Additional research/data improvements: the carry approximation in `veto/mean_reversion.py:410` uses square-root extrinsic decay and mixes calendar DTE with holding sessions; it is a rough cost filter, not a proof of positive EV. News headlines are not a reliable earnings calendar. Add deterministic event-date checks, align the LLM's described holding horizon with configuration, and measure displayed depth/capacity. Emergency market conversion after repeated canceled long-call limits needs an explicit slippage-versus-urgency policy.

## Portfolio risk and promotion gates

Current local settings allocate up to **10% of equity in spread defined loss**, **70% in aggregate long-call premium**, and **35% premium in one call position**. The call lane's 15% modeled stop-risk cap is not a guaranteed loss cap: gaps, outages, halts and option repricing can bypass it. Adding the configured lane ceilings can put roughly 80% of equity at economic risk before execution/lifecycle complications; this is a configuration ceiling, not measured current exposure or a claim both budgets will always be filled.

There is no sufficient shared portfolio loss/drawdown circuit breaker, correlated-factor/Greek stress budget, or dependable risk reservation for uncertain orders. Multiple Nasdaq names plus QQQ/SPY are not independent bets. Broker buying power and daily trade-count limits are not substitutes for these controls. Current holdings are flat, so this is prospective risk, not an instruction to liquidate anything.

Promotion must pass separate gates in order:

1. **Execution correctness:** resolve E1–E7 and M1–M3; cover partial fills, cancel races, ambiguous acceptance, duplicate processes, restarts, stale/error data, early closes and assignment. Every broker exposure and pending order must have a durable owner. Existing 188 passing tests remain necessary but insufficient.
2. **Economic correctness and evidence:** fix the spread repricing/model-readiness defects; freeze and hash the exact policy/configuration/data; run reproducible option-level out-of-sample and sensitivity tests with realistic costs and actual exits. A fixed number of profitable days alone is not a pass criterion. Either lane may fail promotion independently.
3. **Portfolio and operational controls:** retire tournament sizing for any proposed live pilot; agree hard dollar/premium limits based on full economic loss, shared stress/correlation budgets and circuit breakers. Demonstrate monitored paper operation and injected outages with complete reconciliation, alerts, a kill-switch runbook and recovery drills.
4. **Environment and authorization:** only then design a supported live environment with separate credentials, explicit account allowlisting, startup verification, data entitlements and minimum privileges. Current code deliberately hardcodes paper mode at `veto/config.py:27`, `veto/mcp_client.py:47` and `scripts/monitor.py:114`; preserve that protection until an explicitly authorized migration.
5. **Conditional supervised pilot:** only after the preceding gates pass and the owner explicitly approves, consider a one-contract, limited-capital pilot for a validated lane, with known full-loss exposure and real fill/reconciliation monitoring. This is a proposed acceptance stage, not approval to trade now. Scale only on measured live execution and risk evidence.

The highest-value next implementation is **shared order ownership/reconciliation and monitor health**, not a more persuasive LLM or looser gates. In parallel research terms, repair and revalidate the spread model; re-establish whether the exact long-call expression has any net edge. The present evidence supports continued engineering and paper research, not autonomous live deployment.

## Review limitations

This is a repository and captured-paper-account review, not a regulatory, security, broker-statement or independent performance audit. Focused probes establish specific failure paths, not exhaustive coverage. No live exchange execution, full load/chaos campaign, or new historical option-data study was performed. Fee reconciliation has the stated $0.89 residual. Snapshot P&L does not establish drawdown, Sharpe ratio, annualized return or future profitability. Current local configuration is reported as captured and may differ from configurations used by earlier trades.
