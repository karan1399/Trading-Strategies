# Sovereign engine upgrade and strategy search

**Arena.ai / Codex · 28 November 2026 IST · Base 662f4769a**

## Engine changes

The Sovereign loop now asserts the trading interlock before Friday flattening or inherited position management. The protection bridge repeats the assertion immediately before each close or stop amendment. A red interlock writes an event and persists a re-arm latch. A failed position snapshot halts the cycle. An unavailable order snapshot, unprotected exposure or failed account-currency risk valuation blocks entry proposals. Friday exits persist across a failed close and retry at the first permitted session. Flattening runs before signal acquisition and cooldown starts on confirmed closure. Cooldown lookups accept both the persisted string identifier and the legacy integer.

Sovereign now validates bar age against cycle time and quote age separately. It quotes sizing from the outward-rounded stop, uses the loss tick value for proposals, and intersects the 0.25% equity cap with the per-route cap and the $6 stressed desk cap. Stress applies a 1.20× stop move plus a modeled 10 bp notional reserve. Engine profiles journal new real-market candidates as desk proposals for Antigravity. The simulated broker alone uses the engine's direct entry branch. Scheduler, guard and proposal calculations were tested offline. No broker connection or venue action was used. [Engine](../../engine/sovereign_core_engine.py) · [Control tests](../../exchange_feed/tests/test_sovereign_controls.py).

## Research results

The strategy screen covered **94 saved asset recipes across 15 signal families and 10,418 recorded trades**. S08 is a risk overlay. The low-level screen uses only settled pre-2024 labels and adds 20 bp in R units. Its thresholds of 15 trades, positive net R and PF at least 1.15 passed 68 recipes. These are historically selected exports, which remained unsuitable as an untouched test.

The direct engine replay evaluated the incumbent 18 routes, route pruning, equal weights, shorter holds, stricter slot limits, and 11 added route challengers. The challenger set exercised BTC, ETH, ZEC, YFI, VET and BCH. It required positive net return and three completed trades in each pre-final native window with +20 bp. Closed-prefix checks on the challenger signal adapters matched all 110 sampled cases. The ETH Donchian route was the sole addition to pass. The May–July window supplied 6 closes and +0.99% for that sleeve alone. The August–September check supplied 3 closes and +0.15%. YFI was positive in both windows but had only two closed trades in the second. BTC, BCH and ZEC variants with good aggregate research ledgers produced zero closes in the two native training windows at the configured account size and risk screen. [Search runner](sovereign_upgrade_search.py) · [Full search results](sovereign_upgrade_search_results.json).

All portfolios were reset to $2,501.80 at the start of each chronological window. Candidate selection used the worse return-to-drawdown score across the first two windows. October–November outcomes were read after ranking. All three archived periods were previously available to the desk.

| Native portfolio at +20 bp | May–July | August–September | October–November | Latest PF | Latest marked DD |
| --- | ---: | ---: | ---: | ---: | ---: |
| Safety-corrected incumbent | +0.63% | +0.98% | +1.28% | 1.89 | 0.46% |
| Two slots and one crypto | +0.41% | +1.13% | +1.40% | 2.15 | 0.46% |
| Equal-weight top 12 with desk slots | +0.58% | +1.45% | +1.56% | 2.13 | 0.52% |
| Desk slots plus ETH Donchian | +0.54% | +1.10% | +2.27% | 2.34 | 0.46% |

The selection score retained the safety-corrected incumbent as the reference. The ETH arm's gain comes from an 18-trade latest-window sample. It is the frozen challenger for independent verification. The corrected incumbent over the full May–November archive returned +4.12% at base modeled cost and +2.75% at +20 bp. Its marked drawdown at +20 bp was 0.99%. [Full-period safety replay](sovereign_upgrade_safety_replay.json).

The research profile adds ETH Donchian with the saved 4H 22-bar recipe, EMA233 direction, ADX at least 20, Kaufman efficiency ratio of at least 0.25 over 25 bars, 2.5 ATR stop, 8R target, 4 ATR trail, and 96-bar max hold. It caps the book at two positions and one crypto position. The configuration preserves the incumbent and uses independent state. [Shadow profile](../../engine/config_sovereign_shadow.json) · [Shared signal adapters](../../engine/sovereign_challengers.py).

Running the saved shadow profile over May–November returned **+5.00%** on 78 closes at base cost and **+3.79%** at +20 bp. The stressed PF was 1.70 and marked drawdown was 0.94%. This comparison uses the shadow's tighter slot policy as well as the added route. [Saved-profile replay](sovereign_upgrade_shadow_direct.json).

The isolated Stage A optimizer refit all **728 strategy–asset cells** and changed 138 recipes from the saved reference. The separate portfolio screen found 128 cells with enough profitable pre-2024 outcomes after applying feasible stressed lots and +20 bp. Six diversified baskets tested different route counts and archived-family eligibility. Every basket had PF at or below 1.00 in the 2026 diagnostic. The largest active-family basket had PF 1.29 in 2024, 1.06 in 2025 and 0.95 in 2026. This supports keeping the broad expansion out of the default configuration. [Full refit](sovereign_upgrade_full_stage_a.json) · [Basket runner](sovereign_stage_a_portfolio.py) · [Basket results](sovereign_upgrade_stage_a_baskets.json).

**Verification.** All 35 focused tests passed. The incumbent and shadow offline self-tests passed, and the research profile rejected live mode.

**UNVERIFIED-BY-AGENT.** Broker bid/ask ticks and carry, account-wide and farm reservations, structural shelf clearance, actual fills, and forward profitability. The latest-window comparison relied on archived CFD candle opens and spread proxies.

**Signed — Arena.ai / Codex, quantitative review.**
