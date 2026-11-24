#!/usr/bin/env python3
"""
test_ssrn_fx_hedging.py — Empirical Backtesting Engine for SSRN-5047797
"The Best Strategies for FX Hedging" (Castro, Hamill, Harber, Harvey, Van Hemert, 2025).

Tested on historical multi-asset parquet data in historical_parquet/4H:
  - Assets:
      * JP225.p (Nikkei 225 - JPY denominated) hedged via USDJPY.pi
      * GER40.p (DAX 40 - EUR denominated) hedged via EURUSD.pi
      * UK100.p (FTSE 100 - GBP denominated) hedged via GBPUSD.pi
      * AU200.p (ASX 200 - AUD denominated) hedged via AUDUSD.pi
      * Global Diversified Portfolio: 25% JP + 25% DE + 25% UK + 25% AU
  - Hedging Strategies Evaluated:
      1. Unhedged (h = 0)
      2. 100% Fully Hedged (h = 1)
      3. 50% Half Hedged (h = 0.5)
      4. Dynamic Max Carry (h = 0 if foreign yield > US yield else 1)
      5. Minimum Volatility (h* = clip(1 + beta_eq_fx, 0, 1))
      6. Optimal Informed Mean-Variance (h* = clip(1 - mu_fx / (2 * lambda * sigma_fx^2) + beta, 0, 1), lambda=1.333)
      7. 12-Month Momentum / Trend (h = 0 if trailing FX return > 0 else 1)
      8. Blended Strategy (1/3 Optimal + 1/3 Mom + 1/3 Carry)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = REPO_ROOT / "historical_parquet" / "4H"

# -----------------------------------------------------------------------------
# Historical Central Bank Rates (Annualized decimals) across 2023-2026
# -----------------------------------------------------------------------------
def get_rate_series(dates: pd.DatetimeIndex, currency: str) -> np.ndarray:
    """Return historical policy/money-market rate time series for currency."""
    ts = pd.to_datetime(dates, utc=True)
    rates = np.zeros(len(ts))
    for i, t in enumerate(ts):
        year, month = t.year, t.month
        ym = year * 100 + month
        if currency == "USD":
            # Fed Funds target mid / SOFR
            if ym < 202409:
                rates[i] = 0.0533
            elif ym < 202411:
                rates[i] = 0.0483
            elif ym < 202506:
                rates[i] = 0.0458
            else:
                rates[i] = 0.0430
        elif currency == "JPY":
            # BOJ policy rate / TONAR
            if ym < 202403:
                rates[i] = -0.0010
            elif ym < 202407:
                rates[i] = 0.0005
            elif ym < 202501:
                rates[i] = 0.0025
            else:
                rates[i] = 0.0050
        elif currency == "EUR":
            # ECB deposit facility / ESTR
            if ym < 202406:
                rates[i] = 0.0400
            elif ym < 202409:
                rates[i] = 0.0375
            elif ym < 202410:
                rates[i] = 0.0350
            elif ym < 202506:
                rates[i] = 0.0325
            else:
                rates[i] = 0.0300
        elif currency == "GBP":
            # BOE bank rate / SONIA
            if ym < 202408:
                rates[i] = 0.0525
            elif ym < 202411:
                rates[i] = 0.0500
            elif ym < 202506:
                rates[i] = 0.0475
            else:
                rates[i] = 0.0450
        elif currency == "AUD":
            # RBA cash rate
            if ym < 202311:
                rates[i] = 0.0410
            elif ym < 202503:
                rates[i] = 0.0435
            elif ym < 202509:
                rates[i] = 0.0410
            else:
                rates[i] = 0.0385
        else:
            rates[i] = 0.0300
    return rates


# -----------------------------------------------------------------------------
# Data Loader & Resampler
# -----------------------------------------------------------------------------
def load_and_resample(asset_sym: str, fx_sym: str, fx_quote_type: str = "direct") -> pd.DataFrame:
    """
    Load 4H parquet for asset and FX, resample to daily close prices.
    fx_quote_type:
      'direct': Spot is USD per foreign currency (e.g. EURUSD, GBPUSD, AUDUSD).
      'indirect': Spot is Foreign per USD (e.g. USDJPY), so Spot USD/JPY = 1 / rate.
    """
    p_asset = PARQUET_DIR / f"{asset_sym}.parquet"
    p_fx = PARQUET_DIR / f"{fx_sym}.parquet"
    if not p_asset.exists() or not p_fx.exists():
        raise FileNotFoundError(f"Missing parquet: {p_asset} or {p_fx}")

    df_asset = pd.read_parquet(p_asset)
    df_fx = pd.read_parquet(p_fx)

    df_asset["dt"] = pd.to_datetime(df_asset["datetime"], utc=True)
    df_fx["dt"] = pd.to_datetime(df_fx["datetime"], utc=True)

    df_asset = df_asset.set_index("dt").sort_index()
    df_fx = df_fx.set_index("dt").sort_index()

    # Resample to Daily (business days / calendar days) close
    d_asset = df_asset["close"].resample("1D").last().dropna()
    d_fx = df_fx["close"].resample("1D").last().dropna()

    combined = pd.DataFrame({"asset_close": d_asset, "fx_close": d_fx}).dropna()

    if fx_quote_type == "indirect":
        # e.g. USDJPY: Spot (USD per JPY) = 1 / USDJPY
        combined["spot_usd_per_fx"] = 1.0 / combined["fx_close"]
    else:
        # e.g. EURUSD: Spot (USD per EUR) = EURUSD
        combined["spot_usd_per_fx"] = combined["fx_close"]

    return combined


# -----------------------------------------------------------------------------
# Backtest Engine for a Single International Asset
# -----------------------------------------------------------------------------
def run_asset_hedging_backtest(
    country: str,
    asset_sym: str,
    fx_sym: str,
    foreign_ccy: str,
    fx_quote_type: str = "direct",
    lookback_days: int = 60,
    mom_lookback_days: int = 252,
    risk_aversion_lambda: float = 4.0 / 3.0,  # SSRN-5047797 calibrated value ~ 1.333
) -> dict:
    df = load_and_resample(asset_sym, fx_sym, fx_quote_type)
    dates = df.index
    n = len(df)

    # Asset return in local currency
    r_local = df["asset_close"].pct_change().fillna(0.0).values

    # FX spot return (USD per unit of foreign currency)
    s = df["spot_usd_per_fx"].values
    r_spot = np.zeros(n)
    r_spot[1:] = (s[1:] - s[:-1]) / s[:-1]

    # Daily interest rates
    r_home_ann = get_rate_series(dates, "USD")
    r_local_ann = get_rate_series(dates, foreign_ccy)

    r_home_daily = r_home_ann / 365.25
    r_local_daily = r_local_ann / 365.25

    # Currency forward excess return: r_fx_fwd = r_spot + (i_local - i_home)
    r_fx_fwd = r_spot + (r_local_daily - r_home_daily)

    # Hedged return (100% hedged equity): r_hedged = r_local
    r_hedged = r_local

    # Strategies hedge ratio series: h in [0, 1]
    # h = 0 means fully unhedged (100% FX exposure)
    # h = 1 means fully hedged (0% FX exposure)
    h_unhedged = np.zeros(n)
    h_hedged = np.ones(n)
    h_half = np.full(n, 0.5)

    h_carry = np.zeros(n)
    h_minvol = np.zeros(n)
    h_optimal = np.zeros(n)
    h_mom = np.zeros(n)
    h_blend = np.zeros(n)

    # Rolling estimation
    for t in range(n):
        # 1. Max Carry rule (Dynamic):
        # If foreign rate > USD rate -> h = 0 (unhedged to collect yield)
        # If foreign rate <= USD rate -> h = 1 (hedged to avoid paying carry)
        if r_local_ann[t] > r_home_ann[t]:
            h_carry[t] = 0.0
        else:
            h_carry[t] = 1.0

        if t < lookback_days:
            # Warm-up period defaults to 0.5 or carry
            h_minvol[t] = h_carry[t]
            h_optimal[t] = h_carry[t]
            h_mom[t] = h_carry[t]
            h_blend[t] = h_carry[t]
            continue

        # Slice trailing lookback window for beta & volatility
        w_hedged = r_hedged[t - lookback_days : t]
        w_fx = r_fx_fwd[t - lookback_days : t]

        var_fx = np.var(w_fx, ddof=1)
        cov_eq_fx = np.cov(w_hedged, w_fx, ddof=1)[0, 1] if var_fx > 1e-12 else 0.0
        beta_eq_fx = cov_eq_fx / var_fx if var_fx > 1e-12 else 0.0

        # 2. Min Vol Hedge Ratio:
        # h* = clip(1 + beta_eq_fx, 0, 1)
        # Note: In the paper's formula, since r(h) = r_hedged + (1-h)*r_fx,
        # Var(r(h)) = Var(r_hedged) + (1-h)^2 Var(r_fx) + 2(1-h) Cov(r_hedged, r_fx)
        # d/dh Var(r(h)) = -2(1-h) Var(r_fx) - 2 Cov(r_hedged, r_fx) = 0
        # => 1 - h = - Cov(r_hedged, r_fx) / Var(r_fx) = - beta_eq_fx
        # => h* = 1 + beta_eq_fx
        h_mv = 1.0 + beta_eq_fx
        h_minvol[t] = np.clip(h_mv, 0.0, 1.0)

        # 3. Optimal Informed Mean-Variance Hedging:
        # h* = clip(1 - mu_fx / (2 * lambda * var_fx) + beta_eq_fx, 0, 1)
        # mu_fx: expected excess return of FX forward
        # Trend / momentum over window
        w_mom_len = min(t, mom_lookback_days)
        cum_fx_return = np.prod(1.0 + r_fx_fwd[t - w_mom_len : t]) - 1.0
        ann_fx_mom = cum_fx_return * (365.25 / w_mom_len)
        ann_fx_carry = r_local_ann[t] - r_home_ann[t]

        # In the paper, mu_fx is formed from carry and trend:
        # mu_fx_ann = 0.5 * carry + 0.5 * momentum
        mu_fx_ann = 0.5 * ann_fx_carry + 0.5 * ann_fx_mom
        # Convert annual var to annual scale
        var_fx_ann = var_fx * 365.25
        if var_fx_ann > 1e-8:
            mu_term = mu_fx_ann / (2.0 * risk_aversion_lambda * var_fx_ann)
        else:
            mu_term = 0.0
        h_opt = 1.0 - mu_term + beta_eq_fx
        h_optimal[t] = np.clip(h_opt, 0.0, 1.0)

        # 4. Momentum / Trend Strategy:
        # If trailing 12m return > 0 -> h = 0 (unhedged) else h = 1 (hedged)
        if cum_fx_return > 0:
            h_mom[t] = 0.0
        else:
            h_mom[t] = 1.0

        # 5. Blend Strategy:
        # Equal weighted combination of Optimal, Momentum, and Carry
        h_blend[t] = (h_optimal[t] + h_mom[t] + h_carry[t]) / 3.0

    strategies = {
        "Unhedged (h=0)": h_unhedged,
        "Fully Hedged (h=1)": h_hedged,
        "50% Hedged (h=0.5)": h_half,
        "Max Carry": h_carry,
        "Min Volatility": h_minvol,
        "Optimal (Mean-Var)": h_optimal,
        "12M Momentum": h_mom,
        "Blended (Paper Flagship)": h_blend,
    }

    results = {}
    return_series = {}

    for name, h_arr in strategies.items():
        # Daily portfolio return: r(h) = r_hedged + (1 - h) * r_fx_fwd
        # Note: lag hedge decision by 1 day to prevent lookahead bias!
        h_lagged = np.zeros(n)
        h_lagged[1:] = h_arr[:-1]
        h_lagged[0] = h_arr[0]

        r_port = r_hedged + (1.0 - h_lagged) * r_fx_fwd

        # Performance analytics (post warm-up)
        eval_slice = slice(lookback_days, n)
        r_eval = r_port[eval_slice]
        dates_eval = dates[eval_slice]
        n_eval = len(r_eval)
        days = (dates_eval[-1] - dates_eval[0]).days
        years = max(days / 365.25, 0.1)

        cum_return = np.prod(1.0 + r_eval) - 1.0
        cagr = (1.0 + cum_return) ** (1.0 / years) - 1.0
        ann_vol = np.std(r_eval, ddof=1) * np.sqrt(365.25)

        # Average US risk-free rate for Sharpe
        rf_avg = np.mean(r_home_ann[eval_slice])
        sharpe = (cagr - rf_avg) / ann_vol if ann_vol > 1e-8 else 0.0
        sharpe_zero_rf = cagr / ann_vol if ann_vol > 1e-8 else 0.0

        # Downside deviation for Sortino
        neg_returns = r_eval[r_eval < 0]
        downside_vol = (
            np.sqrt(np.mean(neg_returns**2)) * np.sqrt(365.25)
            if len(neg_returns) > 0
            else 1e-8
        )
        sortino = (cagr - rf_avg) / downside_vol if downside_vol > 1e-8 else 0.0

        # Drawdown
        equity_curve = np.cumprod(1.0 + r_eval)
        peak = np.maximum.accumulate(equity_curve)
        dd = (equity_curve - peak) / peak
        max_dd = np.min(dd)
        calmar = cagr / abs(max_dd) if abs(max_dd) > 1e-6 else 0.0

        results[name] = {
            "Total Return (%)": cum_return * 100.0,
            "CAGR (%)": cagr * 100.0,
            "Ann. Volatility (%)": ann_vol * 100.0,
            "Sharpe (rf=SOFR)": sharpe,
            "Sharpe (rf=0)": sharpe_zero_rf,
            "Sortino": sortino,
            "Max Drawdown (%)": max_dd * 100.0,
            "Calmar Ratio": calmar,
            "Avg Hedge Ratio (h)": np.mean(h_lagged[eval_slice]),
        }
        return_series[name] = pd.Series(r_eval, index=dates_eval)

    return {
        "country": country,
        "asset": asset_sym,
        "fx": fx_sym,
        "currency": foreign_ccy,
        "start_date": str(dates[lookback_days].date()),
        "end_date": str(dates[-1].date()),
        "days": (dates[-1] - dates[lookback_days]).days,
        "metrics": results,
        "returns": return_series,
    }


# -----------------------------------------------------------------------------
# Multi-Asset Global Portfolio Backtest
# -----------------------------------------------------------------------------
def run_global_portfolio_backtest(asset_results: list[dict]) -> dict:
    """Aggregate individual country equity returns into an equal-weighted global portfolio."""
    strategy_names = list(asset_results[0]["metrics"].keys())
    common_idx = asset_results[0]["returns"][strategy_names[0]].index
    for ar in asset_results[1:]:
        common_idx = common_idx.intersection(ar["returns"][strategy_names[0]].index)

    global_metrics = {}
    years = (common_idx[-1] - common_idx[0]).days / 365.25

    rf_series = get_rate_series(common_idx, "USD")
    rf_avg = np.mean(rf_series)

    for strat in strategy_names:
        # Equal-weighted combination of the 4 international assets
        strat_returns = pd.DataFrame(
            {ar["country"]: ar["returns"][strat].loc[common_idx] for ar in asset_results}
        ).mean(axis=1)

        cum_return = np.prod(1.0 + strat_returns.values) - 1.0
        cagr = (1.0 + cum_return) ** (1.0 / years) - 1.0
        ann_vol = np.std(strat_returns.values, ddof=1) * np.sqrt(365.25)
        sharpe = (cagr - rf_avg) / ann_vol if ann_vol > 1e-8 else 0.0
        sharpe_zero = cagr / ann_vol if ann_vol > 1e-8 else 0.0

        neg_ret = strat_returns.values[strat_returns.values < 0]
        downside_vol = (
            np.sqrt(np.mean(neg_ret**2)) * np.sqrt(365.25) if len(neg_ret) > 0 else 1e-8
        )
        sortino = (cagr - rf_avg) / downside_vol if downside_vol > 1e-8 else 0.0

        eq = np.cumprod(1.0 + strat_returns.values)
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        max_dd = np.min(dd)
        calmar = cagr / abs(max_dd) if abs(max_dd) > 1e-6 else 0.0

        avg_h = np.mean([ar["metrics"][strat]["Avg Hedge Ratio (h)"] for ar in asset_results])

        global_metrics[strat] = {
            "Total Return (%)": cum_return * 100.0,
            "CAGR (%)": cagr * 100.0,
            "Ann. Volatility (%)": ann_vol * 100.0,
            "Sharpe (rf=SOFR)": sharpe,
            "Sharpe (rf=0)": sharpe_zero,
            "Sortino": sortino,
            "Max Drawdown (%)": max_dd * 100.0,
            "Calmar Ratio": calmar,
            "Avg Hedge Ratio (h)": avg_h,
        }

    return {
        "portfolio": "Global Developed Multi-Asset (25% JP + 25% DE + 25% UK + 25% AU)",
        "start_date": str(common_idx[0].date()),
        "end_date": str(common_idx[-1].date()),
        "days": (common_idx[-1] - common_idx[0]).days,
        "metrics": global_metrics,
    }


# -----------------------------------------------------------------------------
# Main Execution
# -----------------------------------------------------------------------------
def main():
    print("=" * 90)
    print("  SSRN-5047797 'The Best Strategies for FX Hedging' — EMPIRICAL BACKTEST")
    print("  Tested on MT5 Historical Parquet Data (Sep 2023 - Nov 2026)")
    print("=" * 90)

    assets_to_test = [
        ("Japan (Nikkei 225)", "JP225.p", "USDJPY.pi", "JPY", "indirect"),
        ("Germany (DAX 40)", "GER40.p", "EURUSD.pi", "EUR", "direct"),
        ("United Kingdom (FTSE 100)", "UK100.p", "GBPUSD.pi", "GBP", "direct"),
        ("Australia (ASX 200)", "AU200.p", "AUDUSD.pi", "AUD", "direct"),
    ]

    asset_results = []
    for country, asset_sym, fx_sym, ccy, quote_type in assets_to_test:
        print(f"\nProcessing {country}: Asset={asset_sym}, FX={fx_sym} ({ccy})...")
        res = run_asset_hedging_backtest(country, asset_sym, fx_sym, ccy, quote_type)
        asset_results.append(res)

        df_table = pd.DataFrame(res["metrics"]).T
        print(f"\n--- Results for {country} ({res['start_date']} to {res['end_date']}) ---")
        print(df_table.to_string(float_format=lambda x: f"{x:8.2f}"))

    # Global Portfolio
    print("\n" + "=" * 90)
    print("  GLOBAL MULTI-ASSET PORTFOLIO RESULTS (Equal-Weighted 4 Countries)")
    print("=" * 90)
    glob_res = run_global_portfolio_backtest(asset_results)
    df_glob = pd.DataFrame(glob_res["metrics"]).T
    print(f"Period: {glob_res['start_date']} to {glob_res['end_date']} ({glob_res['days']} days)\n")
    print(df_glob.to_string(float_format=lambda x: f"{x:8.2f}"))

    # Save to disk as JSON & Markdown
    out_dir = REPO_ROOT / "desk" / "research"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save clean summary
    summary_export = {
        "paper": "SSRN-5047797 'The Best Strategies for FX Hedging'",
        "authors": "Pedro Castro, Carl Hamill, John Harber, Campbell R. Harvey, Otto Van Hemert (May 2025)",
        "backtest_date": datetime.now(timezone.utc).isoformat(),
        "global_portfolio": glob_res["metrics"],
        "country_results": {ar["country"]: ar["metrics"] for ar in asset_results},
    }

    import json
    with open(out_dir / "ssrn_fx_hedging_results.json", "w", encoding="utf-8") as f:
        json.dump(summary_export, f, indent=2)

    print(f"\n[OK] Backtest complete. Results saved to {out_dir / 'ssrn_fx_hedging_results.json'}")


if __name__ == "__main__":
    main()
