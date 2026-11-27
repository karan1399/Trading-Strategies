# `strategies/` — Structured Alpha Catalog (`core/`, `sleeves/`, `overlays/`)

```text
strategies/
├── core/                         # Shared infrastructure (indicators, fp_features, registry, stack)
├── sleeves/
│   ├── trend/                    # s01_williams_pullback, s09_donchian_trend_cta, s10_ema_pullback
│   ├── mean_reversion/           # s02_rsi2_meanrev, s03_htf_ema_rejection, s06_iv_wall_fade
│   └── breakout/                 # s04_vc_squeeze_breakout, s05_zscore_breakout, s07_daily_pivot_breakout, s11_or30_london_breakout
├── overlays/                     # s08_vol_target_overlay (vol_target_risk_usd & vol_of_conviction_risk_usd)
└── tests/                        # Causality & backward-compatibility tests
```

Legacy imports (`strategies.s09_donchian_trend_cta.strategy`, `strategies._indicators`, etc.) remain 100% compatible via the alias finder in `strategies/__init__.py`.
