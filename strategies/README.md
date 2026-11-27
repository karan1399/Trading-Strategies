# `strategies/` — Unified Strategy Library (All Price-Action, Footprint & Overlay Strategies)

All **25 strategies** live together inside this single `strategies/` folder, regardless of style:

```text
strategies/
├── core/                         # Shared infrastructure (indicators, fp_features, registry, stack)
├── tests/                        # Causality & backward-compatibility tests
├── s01_williams_pullback/        # Live MT5 · Trend Pullback — Williams %R
├── s02_rsi2_meanrev/             # Live MT5 · Mean Reversion — RSI(2)
├── s03_htf_ema_rejection/        # Live MT5 · Mean Reversion — EMA-233 Rejection
├── s04_vc_squeeze_breakout/      # Live MT5 · Breakout — Bollinger/Keltner Squeeze
├── s05_zscore_breakout/          # Live MT5 · Breakout — Z-Score Volatility
├── s06_iv_wall_fade/             # Live MT5 · Intraday Fade — Session 2-SD Wall
├── s07_daily_pivot_breakout/     # Live MT5 · Breakout — Daily Pivot Level
├── s08_vol_target_overlay/       # Live MT5 · Risk Overlay — Volatility Targeting
├── s09_donchian_trend_cta/       # Live MT5 · Breakout — Donchian Channel
├── s10_ema_pullback/             # Live MT5 · Trend Pullback — Dual EMA (89/233)
├── s11_or30_london_breakout/     # Live MT5 · Intraday Breakout — London Open Range
├── cg_signal_strategy.py         # Footprint Lab · Coinglass Engine (Abs/Trap/Div/Exh)
├── delta_divergence.py           # Footprint Lab · Delta Divergence Fade
├── ema233_imb_rejection.py       # Footprint Lab · S03-FP EMA233 + ImbStack >= 2
├── ema_zero_orderflow.py         # Footprint Lab · EMA89/233 + Zero Auction Pullback
├── imbalance_stack.py            # Footprint Lab · Imbalance Stack Continuation
├── of_initiative_breakout.py     # Footprint Lab · OF Initiative Breakout + EMA233
├── rsi2_of_reversion.py          # Footprint Lab · S02-FP RSI(2) + OF + VPIN
├── vwap_of_reclaim.py            # Footprint Lab · VWAP 2σ OF Reclaim
├── zero_auction_reversal.py      # Footprint Lab · Finished Auction Zero (0|X & X|0)
├── bollinger_zscore.py           # Charts Lab · Bollinger / Z-Score Breakout
├── donchian_breakout.py          # Charts Lab · Donchian Channel Breakout
├── ema_pullback.py               # Charts Lab · Dual EMA Pullback
├── rsi2_mean_reversion.py        # Charts Lab · RSI(2) Mean Reversion
└── vwap_rejection.py             # Charts Lab · VWAP Rejection
```

Legacy imports (`strategies.sleeves.*`, `strategies.overlays.*`, `strategies.ig_s*`) remain compatible via `strategies/__init__.py` and `strategies/core/registry.py`.
