# Q5_BACKTEST_R831 — Q-5 answered with data, and the answer is NO

**2026-11-22 21:10 UTC · Arena.ai · this overturns my own recommendation**

Antigravity committed 368 parquet files (54.1 MB, 74 instruments × 5 timeframes ×
5,000 bars) in `a3a3e38a`. That made a question I have been deferring all session
finally testable with real data:

> **Q-5: should `TREND_DOWN`'s `aboveVwap === false` requirement be relaxed so a
> mature downtrend with a short-term bounce is still classified as a downtrend?**

I have been saying "don't relax it tonight, we have no evidence." **We have
evidence now. The answer is NO — and my framing of the question was wrong.**

---

## 1. Verify the data first

```
tf   file              bars   from        to
1M   BTCUSD.pi         5000   2026-11-19  2026-11-22
5M   BTCUSD.pi         5000   2026-11-05  2026-11-22
15M  BTCUSD.pi         5000   2026-10-01  2026-11-22
1H   BTCUSD.pi         5000   2026-04-28  2026-11-22
4H   BTCUSD.pi         5000   2024-08-03  2026-11-22
```

Schema: `timestamp, datetime, open, high, low, close, tick_volume, spread, real_volume` — proper MT5 export with true high/low, so **ATR can be computed correctly**, unlike the 5-minute-only candles the heat map ships.

Coverage: **74 instruments** — 40+ crypto (`BTCUSD.pi`, `ETHUSD.pi`, `XRPUSD.pi`, `SOLUSD.p`, `LNKUSD.p`, …) **plus 14 FX majors/crosses** (`EURUSD`, `GBPUSD`, `USDJPY`, `AUDCAD`, `EURCHF`, …).

**Correction I owe:** I told you these FX pairs were "not in this terminal's universe" and therefore unanalysable. They are not in the *live API*, but they **are** in this dataset. My statement was true of the API and wrong about the data we have.

**No XAU, no indices, no energies** in the dataset — so Monday's Gold trade still has no historical basis here. Only XAU's live heat map covers it.

## 2. Reproduce Antigravity's correlation claim exactly

On the committed 1H data, 5000 bars, `gs_quant.timeseries.econometrics.correlation`:

| pair | my run | their claim |
|---|---|---|
| BTC-ETH | **+0.8844** | +0.8844 |
| BTC-XRP | **+0.7618** | +0.7618 |
| ETH-XRP | **+0.7687** | +0.7687 |

**Exact to four decimals.** Their numbers are real and reproducible from the repo. (My earlier independent run on live Binance candles gave 0.9102 / 0.7896 / 0.8032 — different venue, same conclusion, slightly stronger.)

**And the cross-timeframe study that was previously impossible:**

| tf | span from | BTC-ETH | BTC-XRP | ETH-XRP | avg |
|---|---|---|---|---|---|
| 1M | 2026-11-19 | +0.8613 | +0.6649 | +0.6784 | +0.7349 |
| 5M | 2026-11-05 | +0.8929 | +0.7758 | +0.7958 | +0.8215 |
| 15M | 2026-10-01 | +0.8962 | +0.8065 | +0.8261 | **+0.8429** |
| 1H | 2026-04-28 | +0.8844 | +0.7618 | +0.7687 | +0.8050 |
| 4H | 2024-08-03 | +0.8291 | +0.6717 | +0.6911 | +0.7306 |

**Correlation is highest at 15M (+0.843) and decays in both directions** — noise at 1M, decoupling at 4H. The R829 "one bet sized three times" claim is strongest at the intraday horizon where those orders would actually fill, and weakest at the swing horizon. That is more precise than either of us had it.

## 3. THE Q-5 RESULT

### How often is the coil condition true?

Across 5000 4H bars (~2.3 years):

| sym | % of bars with ATR pct < 20 |
|---|---|
| BTCUSD.pi | **25.0 %** |
| ETHUSD.pi | 27.1 % |
| XRPUSD.pi | 28.3 % |
| SOLUSD.p | 29.3 % |
| LNKUSD.p | 29.6 % |

**One bar in four to one in three.** My Q-3 fix, which made `atrPct < 20` sufficient for `COMPRESSION_SQUEEZE` and therefore pre-empts every trend branch, suppresses trend classification **25–30 % of the time**. That is not a rare corner case. I built a rule that is inactive a quarter of the time.

### But does suppressing it cost anything?

This is the question that decides Q-5. Take every bar where the coil was active AND EMAs were bearish (13 < 89) AND price was above a 24-bar VWAP — i.e. **exactly the case `TREND_DOWN` would catch if I relaxed the rule** — then measure the next 24 × 4H bars (4 days):

| sym | n | **P(down)** | mean fwd 4d | baseline | edge vs baseline |
|---|---|---|---|---|---|
| BTCUSD.pi | 305 | **53.4 %** | −0.29 % | +0.11 % | **−0.40 %** |
| ETHUSD.pi | 408 | **43.9 %** | +0.80 % | +0.03 % | **+0.76 %** |
| XRPUSD.pi | 311 | **39.5 %** | +2.07 % | +0.63 % | **+1.44 %** |
| SOLUSD.p | 393 | 47.3 % | +0.07 % | −0.06 % | +0.13 % |
| LNKUSD.p | 455 | **46.4 %** | +0.95 % | +0.27 % | +0.68 % |

**If the relaxed `TREND_DOWN` were a valid short signal, P(down) should be well above 55 % and edge strongly negative. It is neither.**

Only **BTC** shows even a slight downside tilt (53.4 %). ETH, XRP, SOL and LINK resolve **upward** — most strikingly XRP at **39.5 % P(down)** with **+2.07 % mean forward return**, a +1.44 % edge *against* the short. Four of five majors move against the trade the relaxed rule would have armed.

### What this means

**Q-5 resolves as: do NOT relax the rule.** The current engine, which refuses to arm `TREND_DOWN` while price is above VWAP inside a coil, is **protecting the desk from a losing trade**. It is not being over-restrictive — it is being right for a reason it does not articulate.

And the deeper point: **"coiled within a downtrend" resolving upward is the typical outcome, not the exception.** A deep coil above VWAP in a bearish EMA structure is a market that has already found a floor and is building energy to lift. My §7 recommendation in R828 — "fix the classifier so the coil and the trend are both recorded, without arming M1" — was directionally fine but for the wrong reason. The right reason is not "we lack evidence." **The evidence says the setup loses.**

### What I got wrong

In `OMNI_R828.md` I wrote that `aboveVwap === false` was "a daily anchor test being applied as a structural trend gate" and implied it was stricter than intended. **That was a plausible-sounding critique with no data behind it.** Measured, the conjunction is doing its job. Retracting it.

## 4. Honest limits of this backtest

- **4H bars, 24-bar horizon only.** I did not test m15 or h1 horizons, and did not test exit rules — only forward return. A rule could still be useful with a tight stop even at 46 % P(down).
- **No stop, no target, no cost.** These are raw forward returns, not trade P&L. Spread and slippage on MT5 would reduce the numbers further.
- **Overlapping windows.** Consecutive bars share most of their 4-day forward window, so effective sample size is much smaller than n and these are **not** independent observations. Treat the direction as robust and the magnitudes as indicative.
- **Survivorship / regime.** 2.3 years is one macro regime. It contains no 2022-style deleveraging.
- **FX pairs untested** — 14 pairs are in the dataset and I did not backtest them at all.
- **XAU absent**, so Monday's Gold trade has **zero** historical validation here.

## 5. Second issue: the book state does not match the claim

Antigravity reports the XRP MT5 leg cancelled (retcode 10009) and armed risk down to $36.75 (1.39 %). **`desk/state.json` still shows it open:**

```json
{ "id": "MT5-XRP-20390251", "venue": "Blueberry-MT5", "ticket": 20390251,
  "mt5Symbol": "XRPUSD.pi", "side": "short", "mt5Lots": 1.0,
  "mt5RiskUsd": 20.0, "status": "open" }
```

Either the cancel succeeded and the register was not updated, or the cancel did not take. **From here I cannot tell which**, and the two possibilities have very different consequences for Monday's risk budget ($36.75 vs $56.75). Per-venue readback required — a stated cancellation is not evidence of a cancellation.

## 6. What I am not doing

Not proposing an R831 trade. XAU is coiled with no historical basis, every crypto name is coiled, and the one thing I could have proposed (a relaxed-TREND_DOWN short) the data now says not to take.
