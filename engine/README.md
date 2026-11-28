# engine/ — Holy Grail Unified Engine, D_Sovereign_Core & Legacy MT5 Runners

## 1. Holy Grail Unified 3-Sleeve Engine (Recommended IKIGAI Portfolio)

Engine: [`holy_grail_engine.py`](holy_grail_engine.py) + [`config_holy_grail.json`](config_holy_grail.json) (`HGv3_All38_R0.75_M8_Eq_DD10/15`).
Validated via 16 continuous rolling **8m IS &rarr; 4m OOS** folds (`2021-09` &rarr; `2026-12`, 1,466 OOS trades, `PF 1.27`, `CAGR +25.3%`, `MaxDD 18.2%`, `Calmar 1.39`, `Sharpe 1.25`, `DSR 0.994`, `t_HAC 3.05`), 50,000-path Stationary Block Bootstrap + Crash MC (`DD95 21.9%`, `CrashDD95 25.9%`), and `20 bps` stress (`PF 1.17`, `CAGR +13.8%`).

- **Sleeve A (`4H` 5-Pillar Physics + OrderFlow)**: `A_4H_L`, `A_4H_S` (+ Short Climax Ratchet), `A_RECL`.
- **Sleeve B (`Macro Event Overlay`)**: Pre-event `30m` blackout (`FOMC/NFP/CPI`) + `B_FOMC_15m_ALN` (`t+15m` price+delta continuation).
- **Sleeve C (`Adaptive Volume-Clock`)**: Causal 24-bar/day dollar-volume clock (`C1_VC_PURE_L`, `C3_VC_PURE_S`).
- **Risk & Session Policy**: `0.75%` base risk (`1.0%` alternative preset in `_meta.alternative_1pct_preset`), `T4_Convex` (`2.5 ATR SL / BE@1.5R / 5.0 ATR Trail`), `10%/15%` DD hysteresis, Crypto `24/7`, Commodities (`XAU/XAG/CL/XPT`) **Friday `20:30 UTC` mandatory flatten**.

```bash
python engine/holy_grail_engine.py --selftest
python engine/holy_grail_engine.py --status
python engine/holy_grail_engine.py --dry
python engine/holy_grail_engine.py --live --i-understand-live-risk
```

---

## 2. D_Sovereign_Core & Legacy MT5 Production Runners

Primary sovereign engine: `sovereign_core_engine.py` + `config_sovereign_2500.json`.
(Legacy `unified_alpha_pilot.py` + `config.json` and `m4_live_engine.py` retained for parity/audit compatibility.)

## Launch D_Sovereign_Core (Recommended)

```bash
python engine/sovereign_core_engine.py --selftest
python engine/sovereign_core_engine.py --status
python engine/sovereign_core_engine.py --dry
python engine/sovereign_core_engine.py --live --i-understand-live-risk
```

The live mode manages existing Sovereign positions when its interlock permits it and journals fresh entry ideas as `DESK_PROPOSAL`. New entry submissions require the operator’s desk gateway, current stop-shelf evidence and shared reservation readback. After an interlock failure, the persisted `interlock_rearm_required` latch requires operator reconciliation and the desk re-arm procedure.

Replay the optional ETH Donchian shadow profile entirely offline with `python -m desk.quant_audits.sovereign_engine_step_replay --config engine/config_sovereign_shadow.json`. The research profile uses independent state for dry monitoring and rejects live mode.

### Session Schedule Policy (`session_policy` in `config_sovereign_2500.json`)
- **CRYPTO (`BTCUSD.pi`, `ETHUSD.pi`)**: Runs **24/7** uninterrupted (including weekends).
- **Non-CRYPTO (`USDJPY.pi`, `GAUUSD.pi`, `US2000.p`)**: Halts new entries Friday `19:00 UTC`, automatically **force-closes all open non-crypto positions by Friday `20:30 UTC`**, and resumes Sunday `22:05 UTC`.

---

# Legacy Unified Multi-Strategy MT5 Pilot

Single-file daemon: `unified_alpha_pilot.py` + frozen `config.json`.
Full audit basis: `desk/STRATEGY_SUITE_QUANT_AUDIT.md` (2026-11-24).

| Sleeve | Magic | TF | Status |
| --- | --- | --- | --- |
| htf_gold (EMA233 P4 cluster, gold family ONLY) | 42003 | H4 | live-eligible, one risk unit |
| ivwall (2-SD session wall fade, 13 majors) | 42002 | M15 | micro-risk pilot, exotics excluded |
| vc_gold (XAUUSD squeeze blueprint, fixed 4R) | 42004 | H4 | paper-eligible micro-risk |
| orb_nas (OR30 @ 14:30 UTC) | 42005 | M15 | monitor size only |

## Launch (local machine with MT5 terminal running + Blueberry #5126976 logged in)

```bash
git clone https://github.com/karan1399/Trading-Strategies.git
cd Trading-Strategies
python3 -m pip install MetaTrader5 pandas numpy pyarrow

# 1) no-terminal validation (runs in seconds)
python3 engine/unified_alpha_pilot.py --selftest     # expect SELFTEST: PASS

# 2) paper trade — full signal pipeline + journal, ZERO orders (terminal attached)
python3 engine/unified_alpha_pilot.py --dry

# 3) live micro-risk ($5-6 fixed per trade) — flag is mandatory
python3 engine/unified_alpha_pilot.py --live --i-understand-live-risk

# inspect
python3 engine/unified_alpha_pilot.py --status
tail -f engine/data/unified_journal.csv
```

## What the governor enforces (non-negotiable)

- Every order carries **server-side SL + TP** — zero naked positions.
- Fixed-dollar risk ($5.00–$6.00): lots computed from live `symbol_info`
  (tick_value/tick_size, volume_min/step). If the structural stop at `volume_min`
  risks more than $6.00, the trade is **rejected** — the stop is never compressed.
- Max **2 concurrent positions** portfolio-wide, max **1 per sleeve**, and the gold
  cluster (magics 42003 + 42004) counts as **one** risk slot.
- Signals on **closed bars only**, entry at the next bar's open.
- Breakeven lock at +1R; IV-wall flat before broker day end; ORB flat at 20:55 UTC.
- State: `engine/data/unified_state.json`, journal: `engine/data/unified_journal.csv`.

## IV-wall TP exception (documented deviation)

The governor's TP >= 2.0R rule applies to htf_gold / vc_gold / orb_nas. The IV-wall
sleeve keeps its audited halfway-back target (session open ± 0.5×EM, min_rr 0.3) —
this geometry is what the OOS numbers validate; forcing 2R on it would trade a
different, unvalidated strategy.
