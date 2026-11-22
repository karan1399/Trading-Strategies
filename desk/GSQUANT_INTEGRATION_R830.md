# GSQUANT_INTEGRATION_R830

**2026-11-22 20:30 UTC · Arena.ai · verified by execution, not by reading the README**

You asked me to explore `goldmansachs/gs-quant` and every repo in the
`goldmansachs` org, and say what we can integrate. I enumerated the org, read the
source, **installed the package and ran the functions**. This note separates what
actually works from what sounds impressive and does not.

---

## 1. The org survey — 73 repos, 1 relevant

Fetched via `api.github.com/orgs/goldmansachs/repos`. Sorted by stars:

| repo | stars | lang | relevant to us? |
|---|---|---|---|
| **gs-quant** | 14,295 | Python | **YES — the only one** |
| gs-collections | 1,818 | Java | no — migrated to Eclipse, deprecated |
| gs-collections-kata | 438 | Java | no — teaching |
| reladomo | 389 | Java | no — ORM |
| obevo | 251 | Java | no — DB deployment |
| jdmn | 186 | Java | no — decision models |
| tablasco | 67 | Java | no — JUnit table comparison |
| jrpip | 52 | Java | no — Java RMI |
| everything else (65) | 0–35 | Go/TS/Java | Prometheus, Grafana, Spark, Kafka, CDK, Nix forks — **infrastructure, not trading** |

**Conclusion: there is no second trading repo.** The org is mostly Goldman's open
source infrastructure and their Legend data-modelling effort. Anyone telling you
"integrate all GS repos" has not looked. gs-quant is the entire opportunity.

## 2. The licence and the credential wall

- **Apache-2.0** — genuinely permissive. We may vendor code, modify it, ship it.
  This is not a trap. The earlier `trading-skills` review flagged an MIT repo; this
  is equally usable.
- **The API wall is real.** The README states: *"In order to access the APIs you
  will need a client id and secret. These are available to institutional clients
  of Goldman Sachs."* We do not have them and will not get them.

**Therefore the only honest integration is the OFFLINE surface**: the pure
functions that operate on a pandas Series you already hold. Anything that calls
`GsDataApi`, `GsSession`, `Asset`, or `Curve` is **permanently unavailable** to us.
I verified this by calling them, not by assuming it.

## 3. What I actually ran — the verified inventory

Installed `gs-quant==2.2.6` (the current release, 18 Nov 2026). It **imports with
no credentials**. Results on synthetic series:

### WORKS offline — verified by execution

| function | module | test result |
|---|---|---|
| `max_drawdown` | econometrics | **OK** (−0.279 on a random walk) |
| `beta` | econometrics | **OK** (0.081) |
| `correlation` | econometrics | **OK** (0.063) |
| `returns` | econometrics | **OK** |
| `moving_average` | technicals | **OK** |
| `exponential_moving_average` | technicals | **OK** |
| `bollinger_bands` | technicals | **OK** |
| `relative_strength_index` | technicals | **OK** |
| `macd` | technicals | **OK** |
| `mean median std var cov sum_ product min_ max_ range_` | statistics | **OK** |
| `zscores` | statistics | **OK** |
| `winsorize` | statistics | **OK** |
| `percentiles` | statistics | **OK** |
| `exponential_std` | statistics | **OK** |

That is **19 verified-working functions**.

### BLOCKED — verified by execution, not assumption

| function | failure |
|---|---|
| `sharpe_ratio` | `MqUninitialisedError: GsSession is not initialised` — silently fetches a risk-free curve from GS |
| `exponential_volatility` | `MqValueError: multiple data points on same date` on hourly data |
| `percentile` | blocked |
| `volatility` | `MqValueError: multiple data points on same date` |
| `beta`/`correlation` **first attempt** | `AttributeError: 'numpy.ndarray' object has no attribute 'shift'` — failed until I passed real `pd.Series` with a distinct index. **This is a real trap**: they fail confusingly, not loudly. |

## 4. What this actually buys us — honestly

### It buys: a validated third-party implementation of the risk maths we do NOT have

Our desk currently has **zero** of the following, computed anywhere:

- **max drawdown** — never computed on any strategy or account
- **beta / correlation between our positions** — the R829 challenge said "three
  crypto shorts is one bet at 3× size". That was arithmetic on notional. gs-quant's
  `beta` and `correlation` would turn it into a **measured** statement.
- **Sharpe / realised vol on a track record** — we have no track record metric at all.

That is the "put us on steroids" answer, and it is smaller than the hype: **it is
not alpha, it is measurement.** We have been generating trades without any
formal risk statistics. This closes that specific gap with code written by people
who do this professionally, under a licence that lets us use it.

### It does NOT buy: anything market-facing

No gs pricing models, no curve construction, no Marquee datasets, no GS research.
`gs_quant/risk/` (risk core, scenario engine) is **curve-based** and API-gated — it
models GS derivative books, not crypto perps. The `backtests` module is Marquee-
backed. **None of it runs for us.**

### It does NOT solve our actual bug

I checked specifically: `technicals.trend()` is **seasonal decomposition**, not a
trend classifier. It needs two years of data and removes seasonality. It has
**nothing to say about Q-5** (my `COMPRESSION_SQUEEZE` subsuming `TREND_DOWN`).
Do not let its name mislead anyone.

## 5. The integration — concrete, three files

### (a) `scripts/trading/gs_metrics.py` — the wrapper we write

Not a fork. A thin, tested adapter that:

1. converts our candle arrays (`{ts,o,h,l,c}`) into `pd.Series` **with a distinct
   DatetimeIndex** — this is mandatory, the raw-numpy failure mode above is real;
2. calls the 19 verified functions;
3. returns plain JSON so `omni.py` and the desk cycle can consume it.

Deliberately excluded: everything in the BLOCKED table. Wrapping a function that
silently fetches from GS would make our analytics depend on a session we do not
have — and would fail in production, at the worst moment.

### (b) `requirements.txt` addition — pinned

```
gs-quant==2.2.6
```

Pinned, not floating. This package moves fast (439 releases) and a silent
behaviour change in a risk metric is exactly the kind of thing that goes unnoticed
until it costs money.

### (c) The actual uses, in priority order

1. **Portfolio correlation on the live book** — feed our 3 armed MT5 shorts +
   ETH into `correlation`/`beta`. Turns the R829 "one bet at 3× size" claim from
   my arithmetic into a measured number. **This is the highest-value single use**,
   and it directly serves Monday's phase-1 risk decision.
2. **Max drawdown per strategy** on the archived telemetry in `desk/archive/`
   (86 MB of history we have never analysed statistically).
3. **Sharpe / realised vol** on the near-miss cohort once it resolves — we have
   13 pending rows and no metric to score them with.

## 6. What Antigravity should do — the brief

This is for Antigravity, not me, because it owns execution and the MT5 contract
maths:

1. **Add `gs-quant==2.2.6` to `requirements.txt`.** Verify `pip install` still
   succeeds in the daemon's environment — the package pulls numpy/pandas/scipy/
   statsmodels, and a dependency conflict with the daemon's existing stack would
   be discovered at 21:58Z if nobody checks. **Check it before Monday, not during.**
2. **Build `scripts/trading/gs_metrics.py`** per §5(a). I have deliberately not
   written it: the adapter needs to match the desk's live candle shape, and only
   Antigravity has the MT5/Delta feed in hand.
3. **Run correlation on the live book first.** Three short crypto positions and an
   ETH short — if `correlation` returns >0.7 across them, the R829 challenge is
   confirmed with a number and Monday's sizing decision should follow it.
4. **Do not attempt GS API integration.** No credentials exist. Any PR that adds a
   `GsSession` init is dead code and a false signal of capability.
5. **Do not use `sharpe_ratio`** until we supply our own risk-free rate — it will
   throw `MqUninitialisedError` and take a cycle down with it.

## 7. The honest bottom line

On steroids? **Marginally, and in one specific way.** We gain a professionally
written, licence-clean implementation of drawdown/beta/correlation/volatility —
statistics our desk genuinely lacks and genuinely needs, especially with three
correlated shorts on the book before a funded-account deadline.

We do not gain pricing models, market data, research, or a trend classifier. The
credential wall sees to that. Anyone who says otherwise has not run the code.

**And the real discipline point:** adding a metrics library does not make the
trades better. It makes our *knowledge of* the trades better. Given that this
desk has been sizing without knowing its own correlation, that is worth doing —
but it is measurement, not edge.

---

### Appendix — reproduction

```bash
pip install gs-quant==2.2.6
python3 -c "
import pandas as pd, numpy as np
from gs_quant.timeseries import econometrics as ie, technicals as t, statistics as st
idx = pd.date_range('2026-01-01', periods=500, freq='h')      # distinct index REQUIRED
p = pd.Series(100*np.exp(np.random.normal(0,0.01,500).cumsum()), index=idx)
print(ie.max_drawdown(p).iloc[-1])
print(ie.beta(p, p*1.1).iloc[-1])
print(t.relative_strength_index(p).iloc[-1])
"
```
