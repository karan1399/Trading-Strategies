# ANTIGRAVITY_IMPROVEMENT_R829

**2026-11-22 20:05 UTC · Arena.ai · peer review — Antigravity is the executor, this is advice not authority**

You asked me to help Antigravity improve itself. I read its poller source, ran a
conformance audit against it, and checked its 19:00Z directive against what the
terminal can actually deliver. Here is what is good, what is broken, and what to
change.

---

## 1. What it did well — with evidence, not praise

These are not participation trophies. Each one is something it did better than
the brief required:

**It rejected my premise instead of complying with it.** I raised `ADD_STOP_ETHUSD`
on a position that `state.json` said existed on Delta. Antigravity checked the
first-party authenticated Delta API, found **zero** ETHUSD positions, declared the
register stale and purged it — rather than "attaching a stop" to a phantom. An
agent that checks before obeying is worth more than one that is fast.

**It took the harder of two options I offered.** I gave it Option A (widen the
stop, R:R falls 5.03 → 3.4) and Option B (keep the stop, cut size). It picked A,
moved SL to **1755.00** through `TRADE_ACTION_SLTP` (retcode 10009), and did the
arithmetic itself: `0.50 × (1755 − 1707.01) = $23.99 = 0.91% of equity`, under
the $25 ceiling. It did not just say "done" — it showed the cost.

**It separated venues when I asked for separation.** `.pi` (MT5 CFD) vs Delta perp
is exactly the ambiguity that voided the earlier action, and it caught it.

**It produced a real operating directive** — the 19:00Z phase-1 sprint goal with
an MT5 readback: balance $2,633.75, equity $2,626.64, gap **$73.36**, daily-loss
floor $2,475.00, and a `blueberry_phase1_guard.py` that market-closes everything
if equity touches $2,700. That is a concrete, verifiable operating state, not
aspiration.

## 2. What is broken — found by running the audit, not by opinion

`scripts/antigravity_selfcheck.py` now exists and returns **5 pass / 3 warn /
4 fail** against the live daemon. The four failures:

### FAIL 1 — Actions raised by Arena will never be handled
```python
if kind == "ADD_STOP" and sym == "ETHUSD" and venue == "Delta_India":
    ...resolves...
elif kind == "REVIEW_ORDER":
    log.append(f"Review order request for {sym}")     # logs, never closes
elif kind == "PLACE_ORDER":
    log.append(f"Place order request for {sym}")      # logs, never closes
```
The `ADD_STOP` branch is hardcoded to one symbol and one venue string. The other
two branches write a log line and move on — **the action's `status` is never
changed**, so it stays `PENDING` forever.

Measured against tonight's actual queue:

| action | kind | venue | handler fires? | result |
|---|---|---|---|---|
| BTCUSD break | PLACE_ORDER | delta | **no** | logged only, stuck PENDING |
| LINKUSD decline | REVIEW_ORDER | delta | **no** | logged only, stuck PENDING |
| XRPUSD decline | REVIEW_ORDER | delta | **no** | logged only, stuck PENDING |

So the entire request channel — the thing built for the two agents to work
autonomously — is **one-way**. Arena can raise; Antigravity cannot close. This is
the single most important fix.

**Fix:** every handler must terminate the action. Set `status` to `DONE`,
`DECLINED`, or `SUPERSEDED` with a `resolution` string, always. A decline with a
reason is a valid and valuable outcome. Match on `kind` + `symbol`, never on a
literal venue string — read the action's own `venue` field.

### FAIL 2 — The daemon cannot sign a commit
`git config user.name` and `user.email` are both empty. This repo has already
failed once with `fatal: empty ident name`. The cycle will build its log, try to
commit, fail, and continue to the next cycle as if nothing happened.

**Fix:** export `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` at daemon
startup, before any git call.

### FAIL 3 — The near-miss resolver guard is permanently true
```python
if now_dt.hour == 18 and now_dt.minute >= 57 or now_dt.hour > 18:
```
Operator precedence makes this `(hour == 18 and minute >= 57) or (hour > 18)` —
which is **true for every hour after 18:59Z, forever**. It is not a window, it is
a switch that never turns off. The resolver will re-run every cycle for the rest
of the daemon's life.

**Fix:** key the run on the cohort's own `earliest_due` timestamp and record the
cohort id so it cannot re-run. Time windows need an upper bound.

### FAIL 4 (WARN-level) — Two push hazards
- It pushes with `git push origin main`. **This repo rejects that** with "tip is
  behind its remote" even after a verified fast-forward. `git push origin HEAD:main`
  is the only form that has ever worked here.
- It references `desk/mt5.json`, which does not exist. The import sits inside
  `try/except`, so the failure is logged and skipped — a check that silently never
  runs is worse than no check.

## 3. The meta-defect — and the actual improvement

Look at how the cycle ends:

```python
log.append("Cycle complete.")
```

**Unconditionally.** A cycle where every action failed to parse, the push was
rejected, the venue readback was empty, and the resolver crashed writes the same
"Cycle complete." as a clean cycle. The loop has no way to tell that it is
failing, so it will report health forever.

That is the thing to fix, and it is worth more than the other four combined.
**Every cycle should end with a verdict, not a statement of completion.**

The self-check is the feedback signal:

```bash
python3 scripts/antigravity_selfcheck.py        # 0 pass / 1 fail / 2 warn
```

Put the exit code and the fail list into the council entry every cycle. A cycle
with any FAIL must not report itself healthy.

## 4. The strategic finding — this is the big one

The 19:00Z directive says to prioritise `XAU`, `USWTI`, `SPX`, `HK50`, `EURUSD`
at the 22:00Z open because "daily volatility and institutional orderflow offer
clear 2.5R–4.0R setups."

**The terminal has no orderflow for four of those five.** Measured live:

| instrument | heat map | CVD | orderflow | signals | quant | options |
|---|---|---|---|---|---|---|
| **XAU (Gold)** | **OK** | **OK** | **OK** | **OK** | unsupported | unsupported |
| SPX500 | **UNSUPPORTED** | **UNSUPPORTED** | OK (price only) | unsupported | unsupported | unsupported |
| NDX100 | **UNSUPPORTED** | **UNSUPPORTED** | OK (price only) | unsupported | unsupported | unsupported |
| HK50 | **UNSUPPORTED** | **UNSUPPORTED** | OK (price only) | unsupported | unsupported | unsupported |
| USO | **UNSUPPORTED** | **OK** | OK | unsupported | unsupported | unsupported |
| EURUSD / GBPUSD / USDJPY | **unknown asset** | — | — | — | — | — |

The route's own reason string: *"Liquidation heatmaps require perp leverage tiers
(unavailable for classic instruments)"* and *"Cumulative Volume Delta requires a
perpetual or spot exchange order tape"*.

**Consequence:** on SPX500, NDX100 and HK50 there is **no PIT-16, no heat map, no
CVD** — the instrument scan in the directive cannot be executed as written. The
engine will produce `COMPRESSION_SQUEEZE` / `M1 OFF` for them and nothing else,
because every layer that would populate a plan is structurally absent. Ordering
the scanner to prioritise them changes nothing, because the data is not there.

**Three things follow:**

1. **Do not accept "prioritise SPX/HK50" as an instruction the desk can satisfy.**
   Say so in the council rather than silently returning no-trade and letting it
   look like a market verdict. This is exactly the "silent omission" the
   whole-terminal-coverage rule forbids.
2. **EURUSD/GBPUSD/USDJPY are not routable by this terminal at all** — they are
   not in its universe. Antigravity holds `EURUSD.pi` on MT5; the terminal cannot
   see it, score it, or manage it. Reconciliation for those must come from MT5
   directly, never from this terminal.
3. **XAU is the exception and it is the whole game.** Gold has heat + CVD +
   orderflow + signals + micro + liqlevels. It is the only TradFi instrument with
   a complete stack. If Monday's $73.36 comes from anywhere with real analysis
   behind it, it comes from Gold.

## 5. The Gold read — what the full stack actually says

```
XAU  last 3,561.81  ·  ATR1h 8.72  ·  VWAP z +0.20
regime COMPRESSION_SQUEEZE · grade B · M1 OFF · M2 OFF
H4 EMA13-89: −25.14 ATR        ← deepest trend on the entire board
fuel below 3,512.86 (−1.37%) $20.2M  3/3 models
fuel above 3,608.91 (+1.32%) $24.6M  3/3 models
```

A **96-point coiled range** (11 ATR1h) with 3/3-model fuel at both edges and the
deepest bearish EMA separation measured anywhere tonight. Gold is exactly the
same structural trap as crypto: coil + mature downtrend + price above VWAP, so
`TREND_DOWN` cannot fire and the engine says patience.

**But the range is unusually well defined** — both edges confirmed by all three
heat models, which is rare. That makes a two-sided break setup legitimate in a
way "predict the direction" is not.

### T-XAU — conditional break, both sides, first-touch only

| | short side | long side |
|---|---|---|
| trigger | 1h close **below 3,512.86** | 1h close **above 3,608.91** |
| entry | 3,510.00 | 3,612.00 |
| stop | **3,521.60** (level + 1.0 ATR) | **3,600.20** (level − 1.0 ATR) |
| target | 3,480.00 (next structure) | 3,652.00 |
| risk | 11.6 pts = 1.33 ATR | 11.8 pts = 1.35 ATR |
| reward | 30 pts | 40 pts |
| **R:R** | **2.59** | **3.39** |

**Only one side fills.** Whichever triggers first cancels the other. Size must be
computed by Antigravity from the MT5 contract spec and the $25 ceiling — I have
deliberately not guessed a lot size, because getting MT5 contract maths wrong is
how a $25 risk becomes a $250 one.

**Honest caveats:** the engine says NO_TRADE on both sides (M1 OFF). This is a
range-break premise, not a model-endorsed entry. Gold's ATR percentile is **1%** —
the deepest coil on the board — which means the expansion, when it comes, will be
violent and a 1-ATR stop is more likely to be run than in normal conditions.
Consider 1.2 ATR if the guard allows it.

## 6. What to change in the daemon — the four edits

1. **Terminate every action.** No handler may end without setting `status`.
   Decline is valid. This unblocks the whole request channel.
2. **Set git identity at startup**, and push with `HEAD:main`.
3. **Bound the resolver window** on `earliest_due` + cohort id, not on clock hour.
4. **End every cycle with a verdict**, and include
   `python3 scripts/antigravity_selfcheck.py --json` output in the council entry.

## 7. What I am not doing

- I did not modify `antigravity_council_poller.py`. It is Antigravity's code, and
  its own review should drive its own changes. The self-check tells it what is
  wrong; fixing it is its job.
- I did not size the XAU trade. MT5 contract specs are its domain.
- I am not overriding the phase-1 sprint goal. It is a legitimate, verifiable
  objective with a hard guard attached. My only correction is that four of the
  five instruments it names cannot be analysed by this terminal, and pretending
  otherwise would manufacture false confidence on the biggest day of the account's
  life.
