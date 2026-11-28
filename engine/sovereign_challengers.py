"""Closed-bar signal adapters for Sovereign's modular crypto challengers."""

import numpy as np

from exchange_feed.backtesting.btc_all25_optimizer_v2 import _df_to_bars, _to_chart_df
from exchange_feed.backtesting.multi_asset_optimizer import _fast_s07_masks
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import adx_arr, er_arr
from strategies.s02_donchian_breakout.strategy import DonchianBreakoutStrategy
from strategies.s05_ema_zero_orderflow.strategy import EmaZeroOrderFlowStrategy
from strategies.s11_of_initiative_breakout.strategy import OrderFlowInitiativeBreakoutStrategy
from strategies.s15_rsi2_mean_reversion.strategy import Rsi2MeanReversionStrategy
from strategies.s16_zero_auction_reversal.strategy import ZeroAuctionReversalStrategy


def challenger_signals(frame, pair):
    """Build the same signal arrays for live-proposal and offline replay callers."""
    sid = pair["sid"]
    params = pair["signal_params"]
    close = frame["c"].to_numpy(float)
    high = frame["h"].to_numpy(float)
    low = frame["l"].to_numpy(float)
    ema233 = frame["ema233"].to_numpy(float)
    side = np.zeros(len(frame), dtype=np.int8)
    if sid == "s09_donchian_trend_cta":
        n = int(params["channel"])
        prev_high = frame["h"].shift(1).rolling(n).max().to_numpy(float)
        prev_low = frame["l"].shift(1).rolling(n).min().to_numpy(float)
        clv = frame["clv"].to_numpy(float)
        delta = frame["d_pct"].to_numpy(float)
        buys = frame["buy_imb"].to_numpy(int)
        sells = frame["sell_imb"].to_numpy(int)
        up = (close > prev_high) & (close > ema233) & (clv > .25) & (delta > 0) & (buys >= sells)
        down = (close < prev_low) & (close < ema233) & (clv < -.25) & (delta < 0) & (sells >= buys)
        side[up], side[down] = 1, -1
    elif sid == "s02_donchian_breakout":
        for sig in DonchianBreakoutStrategy().generate_signals(_to_chart_df(frame), params):
            side[sig.index] = sig.side
        valid = (close - ema233) * side > 0.
        if params.get("trend_filter") == "adx_er":
            valid &= (adx_arr(high, low, close, 14) >= 20.) & (er_arr(close, 25) >= .25)
        side[~valid] = 0
    elif sid == "s07_daily_pivot_breakout":
        longs, shorts = _fast_s07_masks(frame, build_fp_context(frame), {"swing_k": int(params["swing_k"])})
        cvd = frame["cvd_z24"].to_numpy(float)
        side[longs & (cvd > .5)] = 1
        side[shorts & (cvd < -.5)] = -1
    elif sid == "s10_ema_pullback":
        ema89 = frame["ema89"].to_numpy(float)
        slope = (frame["ema89"] - frame["ema89"].shift(3)).to_numpy(float)
        atr = frame["atr"].to_numpy(float)
        clv, dp = frame["clv"].to_numpy(float), frame["d_pct"].to_numpy(float)
        delta = float(params["min_delta_pct"])
        up = (ema89 > ema233) & (slope > 0.) & (close > ema233) & (close > ema89)
        down = (ema89 < ema233) & (slope < 0.) & (close < ema233) & (close < ema89)
        side[up & (low <= ema89 + .3 * atr) & (dp > delta) & (clv > .20)] = 1
        side[down & (high >= ema89 - .3 * atr) & (dp < -delta) & (clv < -.20)] = -1
    elif sid == "s15_rsi2_mean_reversion":
        for sig in Rsi2MeanReversionStrategy().generate_signals(_to_chart_df(frame), params):
            side[sig.index] = sig.side
        fp = build_fp_context(frame)
        clv, dp = frame["clv"].to_numpy(float), frame["d_pct"].to_numpy(float)
        cvd = frame["cvd_z24"].to_numpy(float)
        confirms_long = (dp > 1.5) | (cvd > .25) | (clv > .20)
        confirms_short = (dp < -1.5) | (cvd < -.25) | (clv < -.20)
        for flag in ("abs", "exh", "div"):
            confirms_long |= frame[f"cg_{flag}_b"].to_numpy(bool)
            confirms_short |= frame[f"cg_{flag}_s"].to_numpy(bool)
        valid = ((close - ema233) * side > 0.) & (fp["vpin_rank"] < .72)
        side[~(valid & np.where(side > 0, confirms_long, confirms_short))] = 0
    else:
        strategies = {
            "s05_ema_zero_orderflow": EmaZeroOrderFlowStrategy,
            "s11_of_initiative_breakout": OrderFlowInitiativeBreakoutStrategy,
            "s16_zero_auction_reversal": ZeroAuctionReversalStrategy,
        }
        for sig in strategies[sid]().generate_signals(_df_to_bars(frame), params):
            side[sig.index] = sig.side
        if sid == "s16_zero_auction_reversal":
            valid = ((close - ema233) * side > 0.) & (adx_arr(high, low, close, 14) <= 24.)
            side[~valid] = 0
    side[:235] = 0
    return dict(side=side, ts=frame["ts"].to_numpy(np.int64),
                end_ts=frame["end_ts"].to_numpy(np.int64), c=close, atr=frame["atr"].to_numpy(float))
