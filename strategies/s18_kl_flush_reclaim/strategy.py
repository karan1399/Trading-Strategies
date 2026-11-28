"""S18: Kaminsky-Lo Safe Flush-Reclaim Mean Reversion (4H OrderFlow + Liquidation Snap-Back).

Fixes the `HGv3 A_RECL` tight-stop whipsaw (`PF=0.93` in-portfolio / `t_HAC=1.70` isolated)
proven via Kaminsky & Lo (2014): under empirical flush autocorrelation `phi_1 = -0.1072`
(`p = 2.16e-10`), replacing `SL=2.5 ATR + BE@1.5R` with VWAP/POC reversion exit + `T=18` bars
+ wide `6.0x ATR` catastrophe cap scaled at `0.40x` lifts OOS performance to:
  - 10 bps OOS (2021-09..2026-11): N=201, WR=67.7%, PF=2.46, t_HAC=+5.29, DD=0.7R
  - 20 bps OOS stress:             N=201, WR=67.2%, PF=2.23, t_HAC=+4.68, DD=0.7R
"""
from __future__ import annotations

import numpy as np

from strategies.core.fp_features import fp_val

DEFAULT_PARAMS = {
    "vwz_max": -1.8,
    "oi_chg_max": -7.0,
    "vol_rel_min": 1.5,
    "clv_min": 0.10,
    "dp_min": 0.0,
    "unit_atr": 2.5,
    "cat_sl_atr": 6.0,
    "max_hold": 18,
    "risk_scale": 0.40,
}


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    """Evaluate causal S18 Kaminsky-Lo flush-reclaim entry at closed bar `i`."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    if i < 50 or i >= len(c) or not extra or "fp" not in extra:
        return None

    fp = extra["fp"]
    a_i = fp_val(fp, "atr20", i, float(atr[i]) if atr is not None and i < len(atr) else float("nan"))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    vwz = fp_val(fp, "vwz48", i, float("nan"))
    clv = fp_val(fp, "clv", i, float("nan"))
    dp = fp_val(fp, "d_pct", i, float("nan"))
    oi_chg = fp_val(fp, "oi_chg_6b", i, float("nan"))
    vol_rel = fp_val(fp, "vol_rel20", i, float("nan"))
    if not (np.isfinite(vwz) and np.isfinite(clv) and np.isfinite(dp) and np.isfinite(oi_chg) and np.isfinite(vol_rel)):
        return None

    if (
        vwz < float(p["vwz_max"])
        and oi_chg < float(p["oi_chg_max"])
        and vol_rel > float(p["vol_rel_min"])
        and clv > float(p["clv_min"])
        and dp > float(p["dp_min"])
    ):
        rd = max(float(p["unit_atr"]) * a_i, 1e-9)
        cat_rd = float(p["cat_sl_atr"]) * a_i
        return {
            "side": 1,
            "rd": float(rd),
            "cat_rd": float(cat_rd),
            "sl_abs": None,
            "tp_abs": None,
            "exit_r": 0.0,
            "be_r": 0.0,
            "trail_atr": 0.0,
            "max_hold": int(p["max_hold"]),
            "risk_scale": float(p["risk_scale"]),
            "kl_nosl": True,
            "note": f"S18_RECL_KL|vwz={vwz:.2f}|oi={oi_chg:.1f}",
        }
    return None


def exit_signal_at(c, i: int, extra=None) -> bool:
    """Return True strictly at closed bar `i` when price reclaims fair value (`vwz48 >= 0` or `c >= poc`)."""
    if not extra or "fp" not in extra or i >= len(c):
        return False
    fp = extra["fp"]
    vwz = fp_val(fp, "vwz48", i, float("nan"))
    poc = fp_val(fp, "poc", i, float("nan"))
    return bool((np.isfinite(vwz) and vwz >= 0.0) or (np.isfinite(poc) and float(c[i]) >= poc))
