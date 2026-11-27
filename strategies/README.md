# `strategies/` — Unified Strategy Library (`s01` .. `s16` Modular Architecture)

Active profitable strategies live in sequentially numbered folders (`s01_` .. `s16_`), each containing `__init__.py`, `strategy.py`, and individual verified `<ASSET>.json` configurations:

```text
strategies/
├── core/                           # Shared indicators, fp_features, registry, stack
├── tests/                          # Causality & backward-compatibility tests
├── archive/unprofitable/           # Quarantined standalone prop losers (aliases preserved)
├── s01_williams_pullback/          # Williams %R Trend Pullback
├── s02_donchian_breakout/          # Donchian Channel Breakout
├── s03_ema233_imb_rejection/       # EMA233 + Footprint Imbalance Rejection
├── s04_vc_squeeze_breakout/        # TTM BB-in-KC Squeeze Breakout
├── s05_ema_zero_orderflow/         # EMA Touch + CVD + Zero-Print Pullback
├── s06_imbalance_stack/            # Multi-Level Footprint Imbalance Stack
├── s07_daily_pivot_breakout/       # D1 Turning-Point Pivot Breakout
├── s08_vol_target_overlay/         # Causal Inverse-Vol Dollar-Risk Overlay
├── s09_donchian_trend_cta/         # Institutional Donchian Trend CTA
├── s10_ema_pullback/               # Dual-EMA Pullback Trend Continuation
├── s11_of_initiative_breakout/     # OF Initiative Breakout (+ optional D1 EMA50/200 Gate)
├── s12_oi_2h_liq_break/            # 2H OI Liquidation Cluster Breakout
├── s13_oi_2h_reclaim_fade/         # 2H Failed Breakout Reclaim Fade
├── s14_oi_4h_sqz_mom/              # 4H OI Squeeze Momentum
├── s15_rsi2_mean_reversion/        # Connors RSI(2) Trend Mean Reversion
└── s16_zero_auction_reversal/      # Footprint Unfinished/Zero Auction Reversal
```

- All runtime configs live in [`engine/config.json`](../engine/config.json) and [`engine/config_m4_v3b.json`](../engine/config_m4_v3b.json) (no redundant per-strategy `config.json` files).
- Legacy imports (`strategies.sleeves.*`, `strategies.ig_s*`, and unnumbered `strategies.<name>`) resolve automatically via `strategies/__init__.py` and `strategies/core/registry.py`.
