# engine/ — Unified Multi-Strategy MT5 Production Pilot

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
