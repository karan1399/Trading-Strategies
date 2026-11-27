"""Strictly Causal Multi-Model ML Meta-Labeling & Sequence Research Engine for Order-Flow Trading.

Zero-lookahead design:
  - Features at bar `i` depend strictly on closed bars `<= end_ts[i]` (before `ts_in = m1[k_in]`).
  - Expanding walk-forward folds (test years 2023, 2024, 2025, 2026) enforce a 7-day purge gap:
    training uses ONLY trades whose exit completed at least 7 days before `fold_start_ts`.
  - Compares 6 diverse model families against the un-filtered baseline:
    1) ElasticNet / Regularized Logistic Regression (Linear anchor)
    2) LightGBM (Leaf-wise histogram GBDT)
    3) XGBoost (Level-wise regularized GBDT)
    4) CatBoost (Symmetric oblivious trees)
    5) PyTorch Hybrid Conv1D+GRU Sequence + Tabular ResMLP (`FootprintSeqResNet`)
    6) Soft-Voting Multi-Architecture Meta-Ensemble (filtering + continuous confidence sizing)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from footprint_Chart.backtesting.multi_asset_optimizer import compute_hac_stats, evaluate_symbol_all_sleeves
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.data_pipeline.mt5_universe import CORE_12_ASSETS, load_mt5_contract_specs
from strategies.core.fp_features import build_fp_context

SEQ_LEN = 16
PURGE_SEC = 7 * 86400


def _precompute_tf_feature_arrays(df: pd.DataFrame, fp: dict[str, np.ndarray], btc_ref: dict[str, np.ndarray] | None) -> dict[str, np.ndarray]:
    """Compute strictly causal bar-level microstructure, momentum, volatility, and cross-asset arrays."""
    n = len(df)
    ts = df["ts"].to_numpy(dtype=np.int64)
    end_ts = df["end_ts"].to_numpy(dtype=np.int64)
    o = fp["o"]
    h = fp["h"]
    l = fp["l"]
    c = fp["c"]
    atr = df["atr"].to_numpy(dtype=np.float64) if "atr" in df else fp["atr20"]
    v = df["v"].to_numpy(dtype=np.float64) if "v" in df else np.ones(n, dtype=np.float64)
    d_pct = fp["d_pct"]
    clv = fp["clv"]
    bi = fp["buy_imb"].astype(np.float64)
    si = fp["sell_imb"].astype(np.float64)
    poc = fp["poc"]
    ztop = fp["zero_top"].astype(np.float64)
    zbot = fp["zero_bot"].astype(np.float64)
    vpin = fp["vpin_rank"]
    vpm = fp["vpm_rk"]
    cvd_z = df["cvd_z24"].to_numpy(dtype=np.float64) if "cvd_z24" in df else np.zeros(n)
    vwz = fp["vwz48"]
    avg_sz_rk = df["avg_sz_rk"].to_numpy(dtype=np.float64) if "avg_sz_rk" in df else np.full(n, 0.5)
    e89 = fp["ema89"]
    e233 = fp["ema233"]
    rsi2 = fp["rsi2"]
    wr14 = fp["wr14"]
    adx14 = fp["adx14"]
    vr4 = fp["vr4"]
    fund_bp = df["fund_bp"].to_numpy(dtype=np.float64) if "fund_bp" in df else np.zeros(n)
    dur_m = df["dur_m"].to_numpy(dtype=np.float64) if "dur_m" in df else np.full(n, 240.0)

    s_dp = pd.Series(d_pct)
    s_atr = pd.Series(atr)
    s_c = pd.Series(c)
    s_v = pd.Series(v)
    dp_lag1 = s_dp.shift(1).to_numpy(dtype=np.float64)
    dp_mean3 = s_dp.rolling(3).mean().to_numpy(dtype=np.float64)
    dp_accel = (s_dp - s_dp.shift(1)).to_numpy(dtype=np.float64)
    ret1 = (s_c - s_c.shift(1)).to_numpy(dtype=np.float64)
    ret3 = (s_c - s_c.shift(3)).to_numpy(dtype=np.float64)
    ret12 = (s_c - s_c.shift(12)).to_numpy(dtype=np.float64)
    natr = (s_atr / s_c.replace(0.0, np.nan)).to_numpy(dtype=np.float64)
    atr_rk = pd.Series(natr).rolling(100).rank(pct=True).to_numpy(dtype=np.float64)
    atr_exp = (s_atr / s_atr.rolling(20).mean().replace(0.0, np.nan)).to_numpy(dtype=np.float64)
    vol_rel20 = (s_v / s_v.rolling(20).mean().replace(0.0, np.nan)).to_numpy(dtype=np.float64)
    e89_slope3 = (pd.Series(e89) - pd.Series(e89).shift(3)).to_numpy(dtype=np.float64)

    cg_b = (
        fp["cg_abs_b"].astype(np.float64)
        + fp["cg_div_b"].astype(np.float64)
        + fp["cg_exh_b"].astype(np.float64)
        + fp["cg_trap_b"].astype(np.float64)
        + fp["cg_sdiv_b"].astype(np.float64)
    )
    cg_s = (
        fp["cg_abs_s"].astype(np.float64)
        + fp["cg_div_s"].astype(np.float64)
        + fp["cg_exh_s"].astype(np.float64)
        + fp["cg_trap_s"].astype(np.float64)
        + fp["cg_sdiv_s"].astype(np.float64)
    )

    # Cross-asset causal alignment against BTCUSDT (strictly latest closed bar <= end_ts[i])
    btc_dist233 = np.zeros(n, dtype=np.float64)
    btc_ret6 = np.zeros(n, dtype=np.float64)
    btc_vpin = np.full(n, 0.5, dtype=np.float64)
    if btc_ref is not None and len(btc_ref["end_ts"]) > 0:
        b_ets = btc_ref["end_ts"]
        idx_b = np.searchsorted(b_ets, end_ts, side="right") - 1
        valid = idx_b >= 0
        iv = np.maximum(0, idx_b)
        btc_dist233 = np.where(valid, btc_ref["dist233"][iv], 0.0)
        btc_ret6 = np.where(valid, btc_ref["ret6"][iv], 0.0)
        btc_vpin = np.where(valid, btc_ref["vpin"][iv], 0.5)

    return {
        "ts": ts, "end_ts": end_ts, "o": o, "h": h, "l": l, "c": c,
        "atr": atr, "d_pct": d_pct, "clv": clv, "bi": bi, "si": si,
        "poc": poc, "ztop": ztop, "zbot": zbot, "vpin": vpin, "vpm": vpm,
        "cvd_z": cvd_z, "vwz": vwz, "avg_sz_rk": avg_sz_rk,
        "e89": e89, "e233": e233, "rsi2": rsi2, "wr14": wr14, "adx14": adx14, "vr4": vr4,
        "fund_bp": fund_bp, "dur_m": dur_m,
        "dp_lag1": dp_lag1, "dp_mean3": dp_mean3, "dp_accel": dp_accel,
        "ret1": ret1, "ret3": ret3, "ret12": ret12,
        "natr": natr, "atr_rk": atr_rk, "atr_exp": atr_exp,
        "vol_rel20": vol_rel20, "e89_slope3": e89_slope3,
        "cg_b": cg_b, "cg_s": cg_s,
        "btc_dist233": btc_dist233, "btc_ret6": btc_ret6, "btc_vpin": btc_vpin,
    }


def _safe_num(val: float, default: float = 0.0) -> float:
    return float(val) if np.isfinite(val) else default


def build_ml_dataset(
    per_symbol: dict[str, dict[str, Any]],
    override_trades_by_sym: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[pd.DataFrame, np.ndarray, pd.DataFrame, list[str]]:
    """Construct zero-lookahead tabular + trailing 16-bar sequence dataset across all simulated trades."""
    # Pre-build BTC causal macro reference on 4H
    btc_ref = None
    if "BTCUSDT" in per_symbol:
        bg4 = per_symbol["BTCUSDT"]["g4"]
        bfp = build_fp_context(bg4)
        batr = np.maximum(bg4["atr"].to_numpy(dtype=np.float64), 1e-6)
        bc = bfp["c"]
        be233 = bfp["ema233"]
        bret6 = ((pd.Series(bc) - pd.Series(bc).shift(6)) / pd.Series(batr)).fillna(0.0).to_numpy(dtype=np.float64)
        btc_ref = {
            "end_ts": bg4["end_ts"].to_numpy(dtype=np.int64),
            "dist233": np.where(np.isfinite(be233), (bc - be233) / batr, 0.0),
            "ret6": bret6,
            "vpin": np.where(np.isfinite(bfp["vpin_rank"]), bfp["vpin_rank"], 0.5),
        }

    # Precompute causal bar arrays per (symbol, tf)
    feat_cache: dict[tuple[str, int], dict[str, np.ndarray]] = {}
    for sym, res in per_symbol.items():
        g4 = res["g4"]
        feat_cache[(sym, 4)] = _precompute_tf_feature_arrays(g4, build_fp_context(g4), btc_ref)
        # Re-load or use 2H when present in cache via build_fp_context if needed
    # Also load 2H frames quickly from cache where available
    for sym in per_symbol:
        g2_p = DATA_DIR / "cache" / f"{sym}_2h.parquet"
        if g2_p.is_file():
            import pyarrow.parquet as pq
            g2 = pq.read_table(g2_p).to_pandas()
            feat_cache[(sym, 2)] = _precompute_tf_feature_arrays(g2, build_fp_context(g2), btc_ref)

    def _sym_trades(s_k: str, r_k: dict[str, Any]) -> list[dict[str, Any]]:
        return override_trades_by_sym[s_k] if (override_trades_by_sym and s_k in override_trades_by_sym) else r_k["all_trades"]

    sleeve_names = sorted({t["sleeve"] for s_k, res in per_symbol.items() for t in _sym_trades(s_k, res)})
    slv_to_id = {s: idx for idx, s in enumerate(sleeve_names)}

    rows: list[dict[str, Any]] = []
    meta_rows: list[dict[str, Any]] = []
    seqs: list[np.ndarray] = []

    for sym, res in per_symbol.items():
        spec = res.get("spec", {})
        sp_bps = float(spec.get("sp_bps", 12.0))
        is_metal = 1.0 if spec.get("cat") == "METAL" else 0.0
        for t in _sym_trades(sym, res):
            slv = str(t["sleeve"])
            tf_k = 2 if "(2h)" in slv else 4
            fa = feat_cache.get((sym, tf_k)) or feat_cache.get((sym, 4))
            if fa is None:
                continue
            i = int(t["bar_i"])
            side = 1.0 if t["side"] == "BUY" else -1.0
            ext = _extract_fa_row_and_seq(fa, i, side, slv, tf_k, sp_bps, is_metal, slv_to_id)
            if ext is None:
                continue
            row, seq_m = ext
            rows.append(row)
            seqs.append(seq_m)

            meta_rows.append({
                "symbol": sym,
                "sleeve": slv,
                "ts_in": int(t["ts_in"]),
                "ts_out": int(t["ts_out"]),
                "year": int(t["year"]),
                "side": t["side"],
                "regime": t["regime"],
                "r": float(t["r"]),
                "win": 1 if float(t["r"]) > 0.0 else 0,
            })

    x_df = pd.DataFrame(rows)
    x_seq = np.stack(seqs, axis=0) if seqs else np.empty((0, SEQ_LEN, 8), dtype=np.float32)
    meta_df = pd.DataFrame(meta_rows)
    return x_df, x_seq, meta_df, list(x_df.columns)


def _train_predict_pytorch_seq_res(
    x_tab_tr: np.ndarray,
    x_seq_tr: np.ndarray,
    y_tr: np.ndarray,
    x_tab_te: np.ndarray,
    x_seq_te: np.ndarray,
    epochs: int = 18,
    seed: int = 2026,
) -> np.ndarray:
    """Train hybrid Conv1D+GRU Sequence + Tabular Residual MLP strictly inside fold and predict probabilities."""
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    torch.manual_seed(seed)
    np.random.seed(seed)

    imp = SimpleImputer(strategy="median")
    scl = StandardScaler()
    xt_tr = np.clip(scl.fit_transform(imp.fit_transform(x_tab_tr)), -6.0, 6.0).astype(np.float32)
    xt_te = np.clip(scl.transform(imp.transform(x_tab_te)), -6.0, 6.0).astype(np.float32)

    class FootprintSeqResNet(nn.Module):
        def __init__(self, n_tab: int, n_seq_ch: int = 8) -> None:
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv1d(n_seq_ch, 24, kernel_size=3, padding=1),
                nn.GELU(),
                nn.Dropout(0.15),
            )
            self.gru = nn.GRU(24, 24, batch_first=True)
            self.tab_net = nn.Sequential(
                nn.Linear(n_tab, 48),
                nn.LayerNorm(48),
                nn.GELU(),
                nn.Dropout(0.25),
                nn.Linear(48, 32),
                nn.LayerNorm(32),
                nn.GELU(),
            )
            self.head = nn.Sequential(
                nn.Dropout(0.20),
                nn.Linear(24 + 32, 16),
                nn.GELU(),
                nn.Linear(16, 1),
            )

        def forward(self, tab: torch.Tensor, seq: torch.Tensor) -> torch.Tensor:
            # seq: (B, L, C) -> (B, C, L)
            s = self.conv(seq.transpose(1, 2)).transpose(1, 2)
            _, h_n = self.gru(s)
            seq_emb = h_n[-1]
            tab_emb = self.tab_net(tab)
            return self.head(torch.cat([seq_emb, tab_emb], dim=1)).squeeze(-1)

    model = FootprintSeqResNet(xt_tr.shape[1], x_seq_tr.shape[2])
    ds = TensorDataset(
        torch.from_numpy(xt_tr),
        torch.from_numpy(x_seq_tr.astype(np.float32, copy=False)),
        torch.from_numpy(y_tr.astype(np.float32)),
    )
    dl = DataLoader(ds, batch_size=256, shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=1.5e-3, weight_decay=2e-2)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    loss_fn = nn.BCEWithLogitsLoss()

    model.train()
    for _ in range(epochs):
        for bt, bs, by in dl:
            opt.zero_grad()
            logits = model(bt, bs)
            loss = loss_fn(logits, by)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        sched.step()

    model.eval()
    with torch.no_grad():
        logits_te = model(
            torch.from_numpy(xt_te),
            torch.from_numpy(x_seq_te.astype(np.float32, copy=False)),
        )
        probs = torch.sigmoid(logits_te).numpy().astype(np.float64)
    return probs


def _eval_oos_trading_metrics(r_vals: np.ndarray, weights: np.ndarray | None = None, risk_pct: float = 1.0) -> dict[str, Any]:
    """Compute realistic compounded OOS equity metrics for unweighted or confidence-scaled trades."""
    n = len(r_vals)
    if n < 5:
        return {"trades": n, "wr": 0.0, "pf": 0.0, "sum_r": 0.0, "mean_r": 0.0, "dd_pct": 0.0, "ret_pct": 0.0, "calmar": 0.0, "sharpe": 0.0, "t_hac": 0.0}
    w = np.ones(n, dtype=float) if weights is None else np.asarray(weights, dtype=float)
    eff = r_vals * w
    eq, pk, mdd = 1.0, 1.0, 0.0
    rp = risk_pct / 100.0
    for val in eff:
        eq *= max(0.01, 1.0 + float(val) * rp)
        pk = max(pk, eq)
        mdd = max(mdd, (pk - eq) / pk * 100.0)
    g = float(eff[eff > 0].sum())
    l = float(abs(eff[eff < 0].sum()))
    pf = (g / l) if l > 0 else 9.99
    ret_pct = (eq - 1.0) * 100.0
    # 4-year OOS span normalization (2023..2026)
    cagr = ((eq ** (1.0 / 3.9)) - 1.0) * 100.0 if eq > 0 else -100.0
    calmar = cagr / mdd if mdd > 0 else 0.0
    sharpe = float(eff.mean() / (eff.std() + 1e-9) * math.sqrt(n / 3.9))
    hac = compute_hac_stats(eff)
    return {
        "trades": int(n),
        "wr": round(float((eff > 0).mean() * 100.0), 1),
        "pf": round(pf, 2),
        "sum_r": round(float(eff.sum()), 2),
        "mean_r": round(float(eff.mean()), 4),
        "dd_pct": round(mdd, 1),
        "ret_pct": round(ret_pct, 1),
        "cagr_pct": round(cagr, 1),
        "calmar": round(calmar, 2),
        "sharpe": round(sharpe, 2),
        "t_hac": hac["t_hac"],
    }


def run_ml_meta_exploration(
    symbols: list[str] | None = None,
    cost_mode: str = "mt5",
    max_concurrent: int = 5,
    test_years: tuple[int, ...] = (2023, 2024, 2025, 2026),
) -> dict[str, Any]:
    """Run full strictly-causal purged walk-forward comparison across 6 ML architectures on MT5-cost trades."""
    import catboost as cb
    import lightgbm as lgb
    import xgboost as xgb

    specs = load_mt5_contract_specs(DATA_DIR)
    avail = [a["binance"] for a in CORE_12_ASSETS if (DATA_DIR / f"{a['binance']}_agg_footprint.parquet").is_file()]
    if symbols:
        want = {s.upper() for s in symbols}
        avail = [s for s in avail if s in want]

    per_symbol = {s: evaluate_symbol_all_sleeves(s, DATA_DIR, cost_mode, specs) for s in avail}
    x_df, x_seq, meta_df, feat_names = build_ml_dataset(per_symbol)
    x_np = x_df.to_numpy(dtype=np.float64)
    y_win = meta_df["win"].to_numpy(dtype=np.int64)
    ts_in = meta_df["ts_in"].to_numpy(dtype=np.int64)
    ts_out = meta_df["ts_out"].to_numpy(dtype=np.int64)

    model_names = ["LogReg_L2", "LightGBM", "XGBoost", "CatBoost", "PyTorch_SeqResNet"]
    oof_probs: dict[str, np.ndarray] = {m: np.full(len(meta_df), np.nan, dtype=np.float64) for m in model_names}
    lgb_imp_accum = np.zeros(len(feat_names), dtype=np.float64)
    folds_done = 0

    for yr in test_years:
        cut_ts = int(datetime(yr, 1, 1, tzinfo=timezone.utc).timestamp())
        next_ts = int(datetime(yr + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        # Strict causal embargo purge gap: train trades must exit at least 7d before fold starts
        tr_mask = ts_out < (cut_ts - PURGE_SEC)
        te_mask = (ts_in >= cut_ts) & (ts_in < next_ts)
        if tr_mask.sum() < 200 or te_mask.sum() < 30:
            continue

        x_tr, y_tr = x_np[tr_mask], y_win[tr_mask]
        x_te = x_np[te_mask]

        # 1) Regularized Linear Logistic Baseline
        lr = Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("scl", StandardScaler()),
            ("clf", LogisticRegression(C=0.05, max_iter=500, random_state=2026)),
        ])
        lr.fit(x_tr, y_tr)
        oof_probs["LogReg_L2"][te_mask] = lr.predict_proba(x_te)[:, 1]

        # 2) LightGBM Regularized
        gbm = lgb.LGBMClassifier(
            n_estimators=220, learning_rate=0.03, max_depth=4, num_leaves=12,
            min_child_samples=45, subsample=0.75, subsample_freq=1, colsample_bytree=0.70,
            reg_alpha=1.0, reg_lambda=5.0, random_state=2026, n_jobs=4, verbose=-1,
        )
        gbm.fit(x_tr, y_tr)
        oof_probs["LightGBM"][te_mask] = gbm.predict_proba(x_te)[:, 1]
        lgb_imp_accum += gbm.feature_importances_.astype(np.float64)

        # 3) XGBoost Regularized
        xm = xgb.XGBClassifier(
            n_estimators=220, learning_rate=0.03, max_depth=3, min_child_weight=25,
            subsample=0.75, colsample_bytree=0.70, reg_alpha=1.5, reg_lambda=8.0,
            eval_metric="logloss", tree_method="hist", random_state=2026, n_jobs=4,
        )
        xm.fit(x_tr, y_tr)
        oof_probs["XGBoost"][te_mask] = xm.predict_proba(x_te)[:, 1]

        # 4) CatBoost Symmetric Oblivious Trees
        cm = cb.CatBoostClassifier(
            iterations=320, learning_rate=0.03, depth=4, l2_leaf_reg=12.0,
            random_seed=2026, thread_count=4, verbose=0,
        )
        cm.fit(x_tr, y_tr)
        oof_probs["CatBoost"][te_mask] = cm.predict_proba(x_te)[:, 1]

        # 5) PyTorch Conv1D+GRU Sequence + Tabular ResMLP
        oof_probs["PyTorch_SeqResNet"][te_mask] = _train_predict_pytorch_seq_res(
            x_tr, x_seq[tr_mask], y_tr, x_te, x_seq[te_mask], epochs=18, seed=2026 + yr,
        )
        folds_done += 1

    valid_oof = np.isfinite(oof_probs["LightGBM"])
    # 6) Blended Meta-Ensemble (equal soft probability blend over diverse model families)
    ens_p = (
        0.15 * oof_probs["LogReg_L2"]
        + 0.25 * oof_probs["LightGBM"]
        + 0.20 * oof_probs["XGBoost"]
        + 0.25 * oof_probs["CatBoost"]
        + 0.15 * oof_probs["PyTorch_SeqResNet"]
    )
    oof_probs["MetaEnsemble_5Model"] = ens_p

    # Chronological concurrency simulator across strictly unseen OOS test window (`valid_oof`)
    sub_meta = meta_df.loc[valid_oof].copy()
    sub_idx = np.flatnonzero(valid_oof)
    order = np.argsort(sub_meta["ts_in"].to_numpy())
    ordered_global_idx = sub_idx[order]

    def _run_concurrent_oos(probs: np.ndarray | None, prob_floor: float | None = None, scale_kelly: bool = False) -> dict[str, Any]:
        active: list[tuple[int, str, str]] = []
        picked_r: list[float] = []
        picked_w: list[float] = []
        picked_by_yr: dict[int, list[float]] = {}
        base_p = float(y_win[valid_oof].mean()) if valid_oof.any() else 0.42
        for g_idx in ordered_global_idx:
            t_in = int(ts_in[g_idx])
            t_out = int(ts_out[g_idx])
            sym_t = str(meta_df.iloc[g_idx]["symbol"])
            side_t = str(meta_df.iloc[g_idx]["side"])
            active = [a for a in active if a[0] > t_in]
            same_asset = sum(1 for a in active if a[1] == sym_t)
            same_side = sum(1 for a in active if a[2] == side_t)
            max_a = 3 if sym_t == "BTCUSDT" else 1
            if len(active) >= max_concurrent or same_asset >= max_a or same_side >= 3:
                continue

            if probs is not None:
                p = float(probs[g_idx])
                if prob_floor is not None and p < prob_floor:
                    continue
                if scale_kelly:
                    # Continuous confidence risk scaler bounded [0.40, 1.40], centered near baseline WR
                    w = float(np.clip(1.0 + (p - base_p) * 3.0, 0.40, 1.40))
                else:
                    w = 1.0
            else:
                w = 1.0

            rv = float(meta_df.iloc[g_idx]["r"])
            yr_t = int(meta_df.iloc[g_idx]["year"])
            active.append((t_out, sym_t, side_t))
            picked_r.append(rv)
            picked_w.append(w)
            picked_by_yr.setdefault(yr_t, []).append(rv * w)

        m = _eval_oos_trading_metrics(np.array(picked_r, dtype=float), np.array(picked_w, dtype=float), risk_pct=1.25)
        m["by_year_r"] = {yr_k: round(float(sum(vs)), 2) for yr_k, vs in sorted(picked_by_yr.items())}
        return m

    y_oos = y_win[valid_oof]
    r_oos = meta_df.loc[valid_oof, "r"].to_numpy(dtype=float)

    classification_eval: list[dict[str, Any]] = []
    for m_name in model_names + ["MetaEnsemble_5Model"]:
        p_m = oof_probs[m_name][valid_oof]
        auc = float(roc_auc_score(y_oos, p_m))
        brier = float(brier_score_loss(y_oos, p_m))
        ll = float(log_loss(y_oos, p_m))
        # Top-quartile vs Bottom-quartile R spread (economic edge calibration)
        q75 = float(np.percentile(p_m, 75))
        q25 = float(np.percentile(p_m, 25))
        top_q_mean_r = float(r_oos[p_m >= q75].mean())
        bot_q_mean_r = float(r_oos[p_m <= q25].mean())
        classification_eval.append({
            "model": m_name,
            "roc_auc": round(auc, 4),
            "brier": round(brier, 4),
            "logloss": round(ll, 4),
            "top_q25_mean_r": round(top_q_mean_r, 4),
            "bot_q25_mean_r": round(bot_q_mean_r, 4),
            "top_bot_spread_r": round(top_q_mean_r - bot_q_mean_r, 4),
        })

    # Base OOS reference + strictly causal threshold sweeps (no future quantile peeking!)
    base_wr = float(y_oos.mean())
    policy_eval: list[dict[str, Any]] = [
        {"policy": "0) Unfiltered All Sleeves Baseline (OOS 23-26)", **_run_concurrent_oos(None)},
    ]
    # Causal probability floor calibrated at fixed domain thresholds (zero future-fold peeking)
    for m_name in ["LogReg_L2", "LightGBM", "XGBoost", "CatBoost", "PyTorch_SeqResNet", "MetaEnsemble_5Model"]:
        pm = oof_probs[m_name]
        policy_eval.append({
            "policy": f"{m_name} | Causal Gate (p>=0.40)",
            **_run_concurrent_oos(pm, prob_floor=0.40, scale_kelly=False),
        })
    for p_thr in (0.38, 0.40, 0.42, 0.45):
        policy_eval.append({
            "policy": f"MetaEnsemble_5Model | Gate p>={p_thr:.2f} + KellyScale",
            **_run_concurrent_oos(ens_p, prob_floor=p_thr, scale_kelly=True),
        })
    policy_eval.append({
        "policy": "MetaEnsemble_5Model | Full KellyScale (0 veto)",
        **_run_concurrent_oos(ens_p, prob_floor=None, scale_kelly=True),
    })

    top_features = sorted(
        [(feat_names[k], round(float(lgb_imp_accum[k] / max(1, folds_done)), 1)) for k in range(len(feat_names))],
        key=lambda x: x[1],
        reverse=True,
    )[:15]

    return {
        "symbols": avail,
        "total_candidate_trades": int(len(meta_df)),
        "strict_oos_trades": int(valid_oof.sum()),
        "oos_base_win_rate_pct": round(base_wr * 100.0, 2),
        "folds": folds_done,
        "classification_eval": classification_eval,
        "policy_eval": policy_eval,
        "top_features_lgbm": top_features,
    }


def _make_pytorch_seq_er_net(n_tab: int, n_seq_ch: int = 8):
    """Instantiate hybrid Conv1D+GRU + Tabular ResMLP SmoothL1 Expected-R architecture."""
    import torch
    import torch.nn as nn

    class FootprintSeqERNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv1d(n_seq_ch, 24, kernel_size=3, padding=1),
                nn.GELU(),
                nn.Dropout(0.15),
            )
            self.gru = nn.GRU(24, 24, batch_first=True)
            self.tab_net = nn.Sequential(
                nn.Linear(n_tab, 48),
                nn.LayerNorm(48),
                nn.GELU(),
                nn.Dropout(0.25),
                nn.Linear(48, 32),
                nn.LayerNorm(32),
                nn.GELU(),
            )
            self.head = nn.Sequential(
                nn.Dropout(0.20),
                nn.Linear(24 + 32, 16),
                nn.GELU(),
                nn.Linear(16, 1),
            )

        def forward(self, tab: torch.Tensor, seq: torch.Tensor) -> torch.Tensor:
            s = self.conv(seq.transpose(1, 2)).transpose(1, 2)
            _, h_n = self.gru(s)
            return self.head(torch.cat([h_n[-1], self.tab_net(tab)], dim=1)).squeeze(-1)

    return FootprintSeqERNet()


def _train_pytorch_seq_er_model(
    x_tab_tr: np.ndarray,
    x_seq_tr: np.ndarray,
    y_er_tr: np.ndarray,
    sample_weight: np.ndarray | None = None,
    epochs: int = 20,
    seed: int = 2026,
) -> tuple[Any, SimpleImputer, StandardScaler]:
    """Train hybrid Conv1D+GRU Sequence + Tabular ResMLP predicting Expected R with optional sample weights."""
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    torch.manual_seed(seed)
    np.random.seed(seed)

    imp = SimpleImputer(strategy="median")
    scl = StandardScaler()
    xt_tr = np.clip(scl.fit_transform(imp.fit_transform(x_tab_tr)), -6.0, 6.0).astype(np.float32)
    sw = np.ones(len(y_er_tr), dtype=np.float32) if sample_weight is None else np.asarray(sample_weight, dtype=np.float32)

    model = _make_pytorch_seq_er_net(xt_tr.shape[1], x_seq_tr.shape[2])
    ds = TensorDataset(
        torch.from_numpy(xt_tr),
        torch.from_numpy(x_seq_tr.astype(np.float32, copy=False)),
        torch.from_numpy(y_er_tr.astype(np.float32)),
        torch.from_numpy(sw),
    )
    dl = DataLoader(ds, batch_size=256, shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=1.5e-3, weight_decay=2.5e-2)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    loss_fn = nn.SmoothL1Loss(beta=1.0, reduction="none")

    model.train()
    for _ in range(epochs):
        for bt, bs, by, bw in dl:
            opt.zero_grad()
            pred = model(bt, bs)
            loss = (loss_fn(pred, by) * bw).mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        sched.step()
    model.eval()
    return model, imp, scl


def _predict_pytorch_seq_er_model(
    model: Any,
    imp: SimpleImputer,
    scl: StandardScaler,
    x_tab_te: np.ndarray,
    x_seq_te: np.ndarray,
) -> np.ndarray:
    import torch

    xt_te = np.clip(scl.transform(imp.transform(x_tab_te)), -6.0, 6.0).astype(np.float32)
    model.eval()
    with torch.no_grad():
        preds = model(
            torch.from_numpy(xt_te),
            torch.from_numpy(x_seq_te.astype(np.float32, copy=False)),
        ).numpy().astype(np.float64)
    return preds


def _train_predict_pytorch_seq_er(
    x_tab_tr: np.ndarray,
    x_seq_tr: np.ndarray,
    y_er_tr: np.ndarray,
    x_tab_te: np.ndarray,
    x_seq_te: np.ndarray,
    epochs: int = 20,
    seed: int = 2026,
    sample_weight: np.ndarray | None = None,
) -> np.ndarray:
    """Train & predict hybrid Conv1D+GRU Sequence + Tabular ResMLP SmoothL1 Expected-R regressor."""
    model, imp, scl = _train_pytorch_seq_er_model(
        x_tab_tr, x_seq_tr, y_er_tr, sample_weight=sample_weight, epochs=epochs, seed=seed,
    )
    return _predict_pytorch_seq_er_model(model, imp, scl, x_tab_te, x_seq_te)


def _compute_decay_weights(
    ts_ref: int,
    ts_train: np.ndarray,
    half_life_years: float | None = 1.5,
    decay_floor: float = 0.20,
) -> np.ndarray:
    """Causal exponential recency weights floored at decay_floor (1.0 if disabled)."""
    if half_life_years is None or half_life_years <= 0:
        return np.ones(len(ts_train), dtype=np.float64)
    age_yrs = np.maximum(0.0, (float(ts_ref) - ts_train.astype(np.float64)) / (365.25 * 86400.0))
    return np.maximum(float(decay_floor), np.exp(-math.log(2.0) * age_yrs / float(half_life_years)))


def _fit_er_ensemble(
    x_tr: np.ndarray,
    x_seq_tr: np.ndarray,
    y_tr: np.ndarray,
    sample_weight: np.ndarray | None = None,
    seed: int = 2026,
) -> dict[str, Any]:
    """Fit all 5 Expected-R Huber/SmoothL1 models (shared between OOS CV and live persistence)."""
    import catboost as cb
    import lightgbm as lgb
    from sklearn.linear_model import HuberRegressor
    import xgboost as xgb

    sw = None if sample_weight is None else np.asarray(sample_weight, dtype=np.float64)

    lin = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("scl", StandardScaler()),
        ("reg", HuberRegressor(alpha=25.0, epsilon=1.35, max_iter=500)),
    ])
    lin.fit(x_tr, y_tr, **({"reg__sample_weight": sw} if sw is not None else {}))

    gbm = lgb.LGBMRegressor(
        objective="huber", alpha=1.25, n_estimators=220, learning_rate=0.025,
        max_depth=4, num_leaves=12, min_child_samples=45,
        subsample=0.75, subsample_freq=1, colsample_bytree=0.70,
        reg_alpha=1.5, reg_lambda=8.0, random_state=seed, n_jobs=4, verbose=-1,
    )
    gbm.fit(x_tr, y_tr, sample_weight=sw)

    xm = xgb.XGBRegressor(
        objective="reg:pseudohubererror", huber_slope=1.25, n_estimators=220, learning_rate=0.025,
        max_depth=3, min_child_weight=25, subsample=0.75, colsample_bytree=0.70,
        reg_alpha=2.0, reg_lambda=10.0, tree_method="hist", random_state=seed, n_jobs=4,
    )
    xm.fit(x_tr, y_tr, sample_weight=sw)

    cm = cb.CatBoostRegressor(
        loss_function="Huber:delta=1.25", iterations=320, learning_rate=0.025,
        depth=4, l2_leaf_reg=14.0, random_seed=seed, thread_count=4, verbose=0,
    )
    cm.fit(x_tr, y_tr, sample_weight=sw)

    pt_model, pt_imp, pt_scl = _train_pytorch_seq_er_model(
        x_tr, x_seq_tr, y_tr, sample_weight=sw, epochs=20, seed=seed,
    )
    return {
        "HuberLinear_ER": lin,
        "LightGBM_Huber_ER": gbm,
        "XGBoost_Huber_ER": xm,
        "CatBoost_Huber_ER": cm,
        "pt_state_dict": {k: v.cpu() for k, v in pt_model.state_dict().items()},
        "pt_imp": pt_imp,
        "pt_scl": pt_scl,
        "pt_n_tab": int(x_tr.shape[1]),
        "pt_n_seq_ch": int(x_seq_tr.shape[2]),
        "pt_model_live": pt_model,
    }


FINAL_BUNDLE_PATH = DATA_DIR / "final_live_ml_bundle.pkl"
FINAL_BLUEPRINT_PATH = DATA_DIR / "final_live_blueprint.json"
TIGHT_SPREAD_UNIVERSE = ["BTCUSDT", "ETHUSDT", "ZECUSDT", "BCHUSDT", "XAUUSDT", "XAGUSDT"]
BLEND_TD_WEIGHTS = {
    "HuberLinear_ER": 0.10,
    "LightGBM_Huber_ER": 0.25,
    "XGBoost_Huber_ER": 0.25,
    "CatBoost_Huber_ER": 0.30,
    "PyTorch_SeqResNet_ER": 0.10,
}


def run_ml_expected_r_exploration(
    symbols: list[str] | None = None,
    cost_mode: str = "mt5",
    max_concurrent: int = 5,
    test_years: tuple[int, ...] = (2023, 2024, 2025, 2026),
    label: str = "All Available Assets",
    half_life_years: float | None = None,
    decay_floor: float = 0.20,
    per_symbol_precomputed: dict[str, dict[str, Any]] | None = None,
    override_trades_by_sym: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compare Expected-R Regression/Sequence ML models under 7d-purged expanding walk-forward."""
    import warnings
    from scipy.stats import spearmanr

    warnings.filterwarnings("ignore", message="X does not have valid feature names")

    specs = load_mt5_contract_specs(DATA_DIR)
    avail = [a["binance"] for a in CORE_12_ASSETS if (DATA_DIR / f"{a['binance']}_agg_footprint.parquet").is_file()]
    if symbols:
        want = {s.upper() for s in symbols}
        avail = [s for s in avail if s in want]

    per_symbol = per_symbol_precomputed or {s: evaluate_symbol_all_sleeves(s, DATA_DIR, cost_mode, specs) for s in avail}
    x_df, x_seq, meta_df, feat_names = build_ml_dataset(per_symbol, override_trades_by_sym=override_trades_by_sym)
    x_np = x_df.to_numpy(dtype=np.float64)
    r_raw = meta_df["r"].to_numpy(dtype=np.float64)
    # Winsorized expectancy target preserves right-tail trend winners without gradient explosion
    y_er = np.clip(r_raw, -1.5, 4.0)
    ts_in = meta_df["ts_in"].to_numpy(dtype=np.int64)
    ts_out = meta_df["ts_out"].to_numpy(dtype=np.int64)

    m_names = ["HuberLinear_ER", "LightGBM_Huber_ER", "XGBoost_Huber_ER", "CatBoost_Huber_ER", "PyTorch_SeqResNet_ER"]
    oof_er: dict[str, np.ndarray] = {m: np.full(len(meta_df), np.nan, dtype=np.float64) for m in m_names}
    lgb_imp_accum = np.zeros(len(feat_names), dtype=np.float64)
    folds_done = 0

    for yr in test_years:
        cut_ts = int(datetime(yr, 1, 1, tzinfo=timezone.utc).timestamp())
        next_ts = int(datetime(yr + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        tr_mask = ts_out < (cut_ts - PURGE_SEC)
        te_mask = (ts_in >= cut_ts) & (ts_in < next_ts)
        if tr_mask.sum() < 150 or te_mask.sum() < 25:
            continue

        x_tr, y_tr = x_np[tr_mask], y_er[tr_mask]
        x_te = x_np[te_mask]
        sw = _compute_decay_weights(cut_ts, ts_out[tr_mask], half_life_years=half_life_years, decay_floor=decay_floor)
        fitted = _fit_er_ensemble(x_tr, x_seq[tr_mask], y_tr, sample_weight=sw, seed=2026 + yr)

        for mk in ("HuberLinear_ER", "LightGBM_Huber_ER", "XGBoost_Huber_ER", "CatBoost_Huber_ER"):
            oof_er[mk][te_mask] = fitted[mk].predict(x_te)
        lgb_imp_accum += fitted["LightGBM_Huber_ER"].feature_importances_.astype(np.float64)
        oof_er["PyTorch_SeqResNet_ER"][te_mask] = _predict_pytorch_seq_er_model(
            fitted["pt_model_live"], fitted["pt_imp"], fitted["pt_scl"], x_te, x_seq[te_mask],
        )
        folds_done += 1

    valid_oos = np.isfinite(oof_er["LightGBM_Huber_ER"])
    w_map = BLEND_TD_WEIGHTS if (half_life_years is not None and half_life_years > 0) else {
        "HuberLinear_ER": 0.15,
        "LightGBM_Huber_ER": 0.25,
        "XGBoost_Huber_ER": 0.20,
        "CatBoost_Huber_ER": 0.25,
        "PyTorch_SeqResNet_ER": 0.15,
    }
    ens_er = sum(float(w_map[k]) * oof_er[k] for k in m_names)
    oof_er["MetaEnsemble_ER"] = ens_er

    r_oos = r_raw[valid_oos]
    rank_eval: list[dict[str, Any]] = []
    for m_name in m_names + ["MetaEnsemble_ER"]:
        p_m = oof_er[m_name][valid_oos]
        rho, rho_p = spearmanr(p_m, r_oos)
        q75 = float(np.percentile(p_m, 75))
        q25 = float(np.percentile(p_m, 25))
        top_r = float(r_oos[p_m >= q75].mean())
        bot_r = float(r_oos[p_m <= q25].mean())
        rank_eval.append({
            "model": m_name,
            "spearman_r": round(float(rho), 4),
            "spearman_p": round(float(rho_p), 5),
            "top_q25_mean_r": round(top_r, 4),
            "bot_q25_mean_r": round(bot_r, 4),
            "top_bot_spread_r": round(top_r - bot_r, 4),
        })

    # Concurrent causal portfolio simulation (first-come & score-prioritized on bar clashes)
    sub_idx = np.flatnonzero(valid_oos)
    order = np.lexsort((-np.nan_to_num(ens_er[sub_idx], nan=0.0), ts_in[sub_idx]))
    ordered_idx = sub_idx[order]

    def _sim_er_policy(preds: np.ndarray | None, er_floor: float | None = None, er_scale: bool = False) -> dict[str, Any]:
        active: list[tuple[int, str, str]] = []
        picked_r, picked_w = [], []
        by_yr: dict[int, list[float]] = {}
        for g_idx in ordered_idx:
            t_in = int(ts_in[g_idx])
            t_out = int(ts_out[g_idx])
            sym_t = str(meta_df.iloc[g_idx]["symbol"])
            side_t = str(meta_df.iloc[g_idx]["side"])
            active = [a for a in active if a[0] > t_in]
            same_asset = sum(1 for a in active if a[1] == sym_t)
            same_side = sum(1 for a in active if a[2] == side_t)
            max_a = 3 if sym_t == "BTCUSDT" else 1
            if len(active) >= max_concurrent or same_asset >= max_a or same_side >= 3:
                continue
            w = 1.0
            if preds is not None:
                pr = float(preds[g_idx])
                if er_floor is not None and pr < er_floor:
                    continue
                if er_scale:
                    # Continuous causal risk scaling bounded [0.40, 1.40] around neutral ER
                    w = float(np.clip(1.0 + pr * 1.5, 0.40, 1.40))
            rv = float(r_raw[g_idx])
            yr_t = int(meta_df.iloc[g_idx]["year"])
            active.append((t_out, sym_t, side_t))
            picked_r.append(rv)
            picked_w.append(w)
            by_yr.setdefault(yr_t, []).append(rv * w)
        m = _eval_oos_trading_metrics(np.array(picked_r, dtype=float), np.array(picked_w, dtype=float), risk_pct=1.25)
        m["by_year_r"] = {yk: round(float(sum(vs)), 2) for yk, vs in sorted(by_yr.items())}
        return m

    policies: list[dict[str, Any]] = [
        {"policy": "0) Baseline (All Sleeves, Score Tie-Break Only)", **_sim_er_policy(None)},
    ]
    for m_name in m_names + ["MetaEnsemble_ER"]:
        policies.append({
            "policy": f"{m_name} | Gate E[R] >= 0.00",
            **_sim_er_policy(oof_er[m_name], er_floor=0.0, er_scale=False),
        })
    for thr in (-0.05, 0.00, 0.03, 0.06, 0.10):
        policies.append({
            "policy": f"MetaEnsemble_ER | E[R]>={thr:+.2f} + KellyScale",
            **_sim_er_policy(ens_er, er_floor=thr, er_scale=True),
        })
    policies.append({
        "policy": "MetaEnsemble_ER | Full ExpectancyScale (0 veto)",
        **_sim_er_policy(ens_er, er_floor=None, er_scale=True),
    })

    top_feats = sorted(
        [(feat_names[k], round(float(lgb_imp_accum[k] / max(1, folds_done)), 1)) for k in range(len(feat_names))],
        key=lambda x: x[1],
        reverse=True,
    )[:15]

    return {
        "label": label,
        "symbols": avail,
        "half_life_years": half_life_years,
        "decay_floor": decay_floor,
        "strict_oos_trades": int(valid_oos.sum()),
        "oos_base_mean_r": round(float(r_oos.mean()), 4),
        "rank_eval": rank_eval,
        "policy_eval": policies,
        "top_features": top_feats,
    }


_BUNDLE_CACHE: dict[str, Any] = {"mtime": None, "bundle": None}


def _load_bundle_cached(bundle_path: Path = FINAL_BUNDLE_PATH) -> dict[str, Any] | None:
    import pickle
    from pathlib import Path as _Path
    p = _Path(bundle_path)
    if not p.is_file():
        return None
    mt = p.stat().st_mtime
    if _BUNDLE_CACHE["bundle"] is not None and _BUNDLE_CACHE["mtime"] == mt:
        return _BUNDLE_CACHE["bundle"]
    with open(p, "rb") as f:
        bundle = pickle.load(f)
    # Reconstruct PyTorch module once into memory cache
    if "pt_state_dict" in bundle and "pt_model_live" not in bundle:
        pt_m = _make_pytorch_seq_er_net(int(bundle["pt_n_tab"]), int(bundle.get("pt_n_seq_ch", 8)))
        pt_m.load_state_dict(bundle["pt_state_dict"])
        pt_m.eval()
        bundle["pt_model_live"] = pt_m
    _BUNDLE_CACHE["mtime"] = mt
    _BUNDLE_CACHE["bundle"] = bundle
    return bundle


def score_live_signal(
    feat_row: dict[str, Any] | np.ndarray,
    seq_arr: np.ndarray | None = None,
    bundle_path: Path = FINAL_BUNDLE_PATH,
    min_expected_r: float = -0.10,
) -> dict[str, Any]:
    """Score a live closed-bar trade candidate using the persisted Final Time-Decay Expected-R Ensemble."""
    import warnings

    warnings.filterwarnings("ignore", message="X does not have valid feature names")
    bundle = _load_bundle_cached(bundle_path)
    if bundle is None:
        return {
            "ok": False,
            "expected_r": 0.0,
            "approve_regime": True,
            "high_conviction": False,
            "risk_mult": 1.0,
            "tier": "NO_BUNDLE",
            "per_model": {},
        }

    feat_names: list[str] = bundle["feat_names"]
    slv_to_id: dict[str, int] = bundle.get("slv_to_id", {})
    if isinstance(feat_row, dict):
        row = dict(feat_row)
        if "sleeve_id" not in row and "sleeve" in row:
            row["sleeve_id"] = float(slv_to_id.get(str(row["sleeve"]), 0))
        x_tab = np.array([[_safe_num(row.get(k, 0.0), 0.0) for k in feat_names]], dtype=np.float64)
    else:
        x_tab = np.asarray(feat_row, dtype=np.float64).reshape(1, -1)

    if seq_arr is None:
        # Construct neutral causal fallback sequence when caller provides tabular snapshot only
        x_seq = np.zeros((1, SEQ_LEN, int(bundle.get("pt_n_seq_ch", 8))), dtype=np.float32)
        x_seq[:, :, 5] = float(x_tab[0, feat_names.index("vpin_rank")] if "vpin_rank" in feat_names else 0.5)
        x_seq[:, :, 6] = float(x_tab[0, feat_names.index("vpm_rk")] if "vpm_rk" in feat_names else 0.5)
    else:
        x_seq = np.asarray(seq_arr, dtype=np.float32).reshape(1, SEQ_LEN, -1)

    per_model: dict[str, float] = {}
    for mk in ("HuberLinear_ER", "LightGBM_Huber_ER", "XGBoost_Huber_ER", "CatBoost_Huber_ER"):
        if mk in bundle:
            per_model[mk] = float(bundle[mk].predict(x_tab)[0])
    if "pt_model_live" in bundle and "pt_imp" in bundle and "pt_scl" in bundle:
        per_model["PyTorch_SeqResNet_ER"] = float(
            _predict_pytorch_seq_er_model(bundle["pt_model_live"], bundle["pt_imp"], bundle["pt_scl"], x_tab, x_seq)[0]
        )

    weights = bundle.get("weights", BLEND_TD_WEIGHTS)
    w_sum = sum(float(weights.get(k, 0.0)) for k in per_model)
    expected_r = (
        sum(float(weights.get(k, 0.0)) * v for k, v in per_model.items()) / w_sum
        if w_sum > 0 else float(np.mean(list(per_model.values())))
    )
    risk_mult = float(np.clip(1.0 + expected_r * 1.5, 0.50, 1.35))
    approve_regime = bool(expected_r >= float(min_expected_r))
    high_conv = bool(expected_r >= 0.0)
    tier = "HIGH_CONVICTION" if expected_r >= 0.05 else ("APPROVED" if approve_regime else "VETO_REGIME")
    return {
        "ok": True,
        "expected_r": round(expected_r, 4),
        "per_model": {k: round(v, 4) for k, v in per_model.items()},
        "approve_regime": approve_regime,
        "high_conviction": high_conv,
        "risk_mult": round(risk_mult, 3),
        "tier": tier,
    }


def _extract_fa_row_and_seq(
    fa: dict[str, np.ndarray],
    i: int,
    side: float,
    slv: str,
    tf_k: int,
    sp_bps: float,
    is_metal: float,
    slv_to_id: dict[str, int],
) -> tuple[dict[str, float], np.ndarray] | None:
    """Shared causal single-bar feature & (SEQ_LEN, 8) extractor for zero train/serve skew."""
    if i < SEQ_LEN or i >= len(fa["c"]):
        return None
    atr_i = _safe_num(fa["atr"][i], 0.0)
    c_i = _safe_num(fa["c"][i], 0.0)
    if atr_i <= 1e-12 or c_i <= 1e-12:
        return None
    o_i, h_i, l_i = float(fa["o"][i]), float(fa["h"][i]), float(fa["l"][i])
    rng_i = max(h_i - l_i, 1e-12)
    bi_i, si_i = float(fa["bi"][i]), float(fa["si"][i])
    wick_adv = ((h_i - max(o_i, c_i)) / rng_i) if side > 0 else ((min(o_i, c_i) - l_i) / rng_i)
    cg_align = (float(fa["cg_b"][i]) - float(fa["cg_s"][i])) * side
    zero_align = float(fa["zbot"][i]) if side > 0 else float(fa["ztop"][i])
    zero_opp = float(fa["ztop"][i]) if side > 0 else float(fa["zbot"][i])
    poc_dist = _safe_num((c_i - float(fa["poc"][i])) / atr_i * side, 0.0)
    natr_bps = max(1.0, atr_i / c_i * 10000.0)

    ts_bar = int(fa["end_ts"][i])
    dt = datetime.fromtimestamp(ts_bar, tz=timezone.utc)
    hr_rad = 2.0 * math.pi * (dt.hour + dt.minute / 60.0) / 24.0
    dow_rad = 2.0 * math.pi * dt.weekday() / 7.0
    slv_lower = slv.lower()
    is_mr = 1.0 if ("rsi2" in slv_lower or "fade" in slv_lower or "sweep" in slv_lower or "imb2" in slv_lower) else 0.0

    row = {
        "signed_d_pct": side * _safe_num(fa["d_pct"][i]),
        "signed_clv": side * _safe_num(fa["clv"][i]),
        "signed_imb_diff": side * (bi_i - si_i),
        "imb_ratio": side * (bi_i - si_i) / (bi_i + si_i + 1.0),
        "zero_align": zero_align,
        "zero_oppose": zero_opp,
        "poc_dist_atr": poc_dist,
        "vpin_rank": _safe_num(fa["vpin"][i], 0.5),
        "vpm_rk": _safe_num(fa["vpm"][i], 0.5),
        "signed_cvd_z24": side * _safe_num(fa["cvd_z"][i]),
        "signed_vwz48": side * _safe_num(fa["vwz"][i]),
        "avg_sz_rk": _safe_num(fa["avg_sz_rk"][i], 0.5),
        "cg_align": cg_align,
        "signed_dp_lag1": side * _safe_num(fa["dp_lag1"][i]),
        "signed_dp_mean3": side * _safe_num(fa["dp_mean3"][i]),
        "signed_dp_accel": side * _safe_num(fa["dp_accel"][i]),
        "signed_ret1_atr": side * _safe_num(fa["ret1"][i] / atr_i),
        "signed_ret3_atr": side * _safe_num(fa["ret3"][i] / atr_i),
        "signed_ret12_atr": side * _safe_num(fa["ret12"][i] / atr_i),
        "atr_pct_rk": _safe_num(fa["atr_rk"][i], 0.5),
        "atr_exp_ratio": _safe_num(fa["atr_exp"][i], 1.0),
        "bar_range_atr": rng_i / atr_i,
        "wick_adv_pct": wick_adv,
        "vol_rel20": _safe_num(fa["vol_rel20"][i], 1.0),
        "vr4": _safe_num(fa["vr4"][i], 1.0),
        "signed_dist_e89": side * _safe_num((c_i - float(fa["e89"][i])) / atr_i),
        "signed_dist_e233": side * _safe_num((c_i - float(fa["e233"][i])) / atr_i),
        "signed_ema_spread": side * _safe_num((float(fa["e89"][i]) - float(fa["e233"][i])) / atr_i),
        "signed_e89_slope3": side * _safe_num(fa["e89_slope3"][i] / atr_i),
        "signed_rsi2": (50.0 - _safe_num(fa["rsi2"][i], 50.0)) * side,
        "signed_wr14": side * (_safe_num(fa["wr14"][i], -50.0) + 50.0),
        "adx14": _safe_num(fa["adx14"][i], 20.0),
        "signed_fund_bp": side * _safe_num(fa["fund_bp"][i]),
        "sp_bps": sp_bps,
        "sp_to_atr_bps": sp_bps / natr_bps,
        "signed_btc_dist233": side * _safe_num(fa["btc_dist233"][i]),
        "signed_btc_ret6": side * _safe_num(fa["btc_ret6"][i]),
        "btc_vpin": _safe_num(fa["btc_vpin"][i], 0.5),
        "hr_sin": math.sin(hr_rad),
        "hr_cos": math.cos(hr_rad),
        "dow_sin": math.sin(dow_rad),
        "dow_cos": math.cos(dow_rad),
        "side": side,
        "is_2h": 1.0 if tf_k == 2 else 0.0,
        "is_mr_sleeve": is_mr,
        "is_metal": is_metal,
        "sleeve_id": float(slv_to_id.get(slv, 0)),
    }
    s0, s1 = i - SEQ_LEN + 1, i + 1
    sq_ret = np.nan_to_num(fa["ret1"][s0:s1] / atr_i * side, nan=0.0)
    sq_rng = np.nan_to_num((fa["h"][s0:s1] - fa["l"][s0:s1]) / atr_i, nan=0.0)
    sq_dp = np.nan_to_num(fa["d_pct"][s0:s1] / 100.0 * side, nan=0.0)
    sq_clv = np.nan_to_num(fa["clv"][s0:s1] * side, nan=0.0)
    sq_e89 = np.nan_to_num((fa["c"][s0:s1] - fa["e89"][s0:s1]) / atr_i * side, nan=0.0)
    sq_vpin = np.nan_to_num(fa["vpin"][s0:s1], nan=0.5)
    sq_vpm = np.nan_to_num(fa["vpm"][s0:s1], nan=0.5)
    sq_imb = np.nan_to_num((fa["bi"][s0:s1] - fa["si"][s0:s1]) / 10.0 * side, nan=0.0)
    seq_m = np.clip(np.stack([sq_ret, sq_rng, sq_dp, sq_clv, sq_e89, sq_vpin, sq_vpm, sq_imb], axis=1).astype(np.float32), -10.0, 10.0)
    return row, seq_m


def train_and_save_final_live_ml(
    data_dir: Path = DATA_DIR,
    cost_mode: str = "mt5",
    half_life_years: float = 1.5,
    decay_floor: float = 0.20,
    save_bundle: bool = True,
) -> dict[str, Any]:
    """Train & persist the Final Unified Tight-Spread Portfolio + 1.5y Time-Decay Expected-R ML Bundle."""
    import json
    import pickle
    import time
    import warnings
    from footprint_Chart.backtesting.multi_asset_optimizer import (
        MULTI_REPORT_PATH,
        optimize_per_asset_strict_is_oos,
        run_full_multi_asset_optimization,
    )

    warnings.filterwarnings("ignore", message="X does not have valid feature names")

    specs = load_mt5_contract_specs(data_dir)
    avail = [s for s in TIGHT_SPREAD_UNIVERSE if (data_dir / f"{s}_agg_footprint.parquet").is_file()]
    per_symbol = {s: evaluate_symbol_all_sleeves(s, data_dir, cost_mode, specs) for s in avail}
    # Derive Per-Asset All-13 Optimal Recipes directly for the tight-spread universe
    per_asset_opt = optimize_per_asset_strict_is_oos(per_symbol, data_dir=data_dir, cost_mode=cost_mode, specs_map=specs, mc_runs=2000)
    by_sym_opt: dict[str, list[dict[str, Any]]] = {}
    for tr in per_asset_opt["tight_trades"]:
        by_sym_opt.setdefault(str(tr["symbol"]), []).append(tr)

    # Walk-forward verification across BOTH Pooled Tight-Spread and Per-Asset All-13 Opt13 + 1.5y Time-Decay
    cv_td = run_ml_expected_r_exploration(
        symbols=avail,
        cost_mode=cost_mode,
        max_concurrent=5,
        label=f"Final Tight-Spread ({len(avail)} Assets) | 1.5y Time-Decay",
        half_life_years=half_life_years,
        decay_floor=decay_floor,
        per_symbol_precomputed=per_symbol,
    )
    cv_opt13_td = run_ml_expected_r_exploration(
        symbols=avail,
        cost_mode=cost_mode,
        max_concurrent=5,
        label=f"Per-Asset All-13 Robust ({len(avail)} Assets) | 1.5y Time-Decay",
        half_life_years=half_life_years,
        decay_floor=decay_floor,
        per_symbol_precomputed=per_symbol,
        override_trades_by_sym=by_sym_opt,
    )

    # Train final production ML bundle on combined tight-spread + per-asset opt13 universe (deduped by ts_in/sym/sleeve)
    merged_by_sym: dict[str, list[dict[str, Any]]] = {}
    for s in avail:
        seen_k = set()
        merged = []
        for t in (per_symbol[s]["all_trades"] + by_sym_opt.get(s, [])):
            k = (t["ts_in"], t["side"], t["sleeve"].split(" ")[0])
            if k not in seen_k:
                seen_k.add(k)
                merged.append(t)
        merged_by_sym[s] = merged

    x_df, x_seq, meta_df, feat_names = build_ml_dataset(per_symbol, override_trades_by_sym=merged_by_sym)
    x_np = x_df.to_numpy(dtype=np.float64)
    r_raw = meta_df["r"].to_numpy(dtype=np.float64)
    y_er = np.clip(r_raw, -1.5, 4.0)
    ts_in = meta_df["ts_in"].to_numpy(dtype=np.int64)
    ts_out = meta_df["ts_out"].to_numpy(dtype=np.int64)

    # Fit final 100%-history production ensemble up to now_ts with 1.5y time decay
    now_ts = int(time.time())
    closed_mask = ts_out < now_ts
    x_tr, seq_tr, y_tr = x_np[closed_mask], x_seq[closed_mask], y_er[closed_mask]
    sw = _compute_decay_weights(now_ts, ts_out[closed_mask], half_life_years=half_life_years, decay_floor=decay_floor)
    fitted = _fit_er_ensemble(x_tr, seq_tr, y_tr, sample_weight=sw, seed=2026)

    slv_names = sorted({t["sleeve"] for s in avail for t in merged_by_sym[s]})
    slv_to_id = {s: idx for idx, s in enumerate(slv_names)}

    if MULTI_REPORT_PATH.is_file():
        port_rep = json.loads(MULTI_REPORT_PATH.read_text(encoding="utf-8"))
        # Strip heavy curves for crisp blueprint
        port_cands = [
            {
                "name": c["name"],
                **{k: c["metrics"][k] for k in ("end_equity", "cagr_pct", "total_trades", "win_rate", "total_r", "profit_factor", "max_dd_pct", "calmar_ratio", "sharpe_ratio", "is_r", "oos_r", "t_hac", "dsr_40_pct") if k in c.get("metrics", {})},
                "mc_20k": c.get("metrics", {}).get("mc_20k"),
            }
            for c in port_rep.get("candidates", [])
        ]
    else:
        port_rep = run_full_multi_asset_optimization(data_dir=data_dir, cost_mode=cost_mode, mc_runs=20000, save_report=True)
        port_cands = [{"name": c["name"], **c.get("metrics", {})} for c in port_rep.get("candidates", [])]

    mt5_routing = []
    for sym in avail:
        sp = specs.get(sym, {})
        mt5_sym = sp.get("mt5", sym)
        min_notional = float(sp.get("min_notional", 0.0) or 0.0)
        mt5_routing.append({
            "binance": sym,
            "mt5_symbol": mt5_sym,
            "spread_bps": sp.get("sp_bps"),
            "min_notional_usd": min_notional,
            "mt5_6usd_compatible": bool(sym in ("ETHUSDT", "BCHUSDT") or mt5_sym == "GAUUSD.pi"),
            "note": (
                "Proxy via GAUUSD.pi on MT5 ($1.82/ATR vs XAUUSD $56.71/ATR)" if sym == "XAUUSDT"
                else ("Delta micro / high-min-lot on MT5 ($5-$6 cap checks 0.01 risk)" if sym in ("BTCUSDT", "ZECUSDT", "XAGUSDT")
                      else "Direct MT5 $5.00-$6.00 micro sizing")
            ),
        })

    blueprint = {
        "generated_ts": now_ts,
        "generated_iso": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "universe_tight_spread": avail,
        "half_life_years": half_life_years,
        "decay_floor": decay_floor,
        "total_training_trades": int(closed_mask.sum()),
        "blend_weights": BLEND_TD_WEIGHTS,
        "portfolio_candidates_comparison": port_cands,
        "per_asset_opt13_recipes": per_asset_opt["per_asset_recipes"],
        "ml_walkforward_oos": cv_td,
        "ml_walkforward_opt13_oos": cv_opt13_td,
        "venue_routing": mt5_routing,
        "bundle_path": str(FINAL_BUNDLE_PATH),
    }

    if save_bundle:
        bundle_out = {
            "HuberLinear_ER": fitted["HuberLinear_ER"],
            "LightGBM_Huber_ER": fitted["LightGBM_Huber_ER"],
            "XGBoost_Huber_ER": fitted["XGBoost_Huber_ER"],
            "CatBoost_Huber_ER": fitted["CatBoost_Huber_ER"],
            "pt_state_dict": fitted["pt_state_dict"],
            "pt_imp": fitted["pt_imp"],
            "pt_scl": fitted["pt_scl"],
            "pt_n_tab": fitted["pt_n_tab"],
            "pt_n_seq_ch": fitted["pt_n_seq_ch"],
            "feat_names": feat_names,
            "slv_to_id": slv_to_id,
            "weights": BLEND_TD_WEIGHTS,
            "universe": avail,
            "trained_ts": now_ts,
            "n_trades": int(closed_mask.sum()),
        }
        with open(FINAL_BUNDLE_PATH, "wb") as bf:
            pickle.dump(bundle_out, bf)
        FINAL_BLUEPRINT_PATH.write_text(json.dumps(blueprint, indent=2), encoding="utf-8")
        _BUNDLE_CACHE["mtime"] = None
    return blueprint


def evaluate_live_mt5_signals(
    symbols: list[str] | None = None,
    data_dir: Path = DATA_DIR,
    bundle_path: Path = FINAL_BUNDLE_PATH,
    lookback_bars: int = 3,
) -> dict[str, Any]:
    """Score latest closed 4H signals across the tight-spread universe using Final ML Bundle."""
    import time

    specs = load_mt5_contract_specs(data_dir)
    target_syms = [s.upper() for s in (symbols or TIGHT_SPREAD_UNIVERSE) if (data_dir / f"{s.upper()}_agg_footprint.parquet").is_file()]
    per_symbol = {s: evaluate_symbol_all_sleeves(s, data_dir, "mt5", specs) for s in target_syms}

    # Build BTC reference for cross-asset features
    btc_ref = None
    if "BTCUSDT" in per_symbol:
        bg4 = per_symbol["BTCUSDT"]["g4"]
        bfp = build_fp_context(bg4)
        batr = np.maximum(bg4["atr"].to_numpy(dtype=np.float64), 1e-6)
        bc, be233 = bfp["c"], bfp["ema233"]
        bret6 = ((pd.Series(bc) - pd.Series(bc).shift(6)) / pd.Series(batr)).fillna(0.0).to_numpy(dtype=np.float64)
        btc_ref = {
            "end_ts": bg4["end_ts"].to_numpy(dtype=np.int64),
            "dist233": np.where(np.isfinite(be233), (bc - be233) / batr, 0.0),
            "ret6": bret6,
            "vpin": np.where(np.isfinite(bfp["vpin_rank"]), bfp["vpin_rank"], 0.5),
        }

    bundle = _load_bundle_cached(bundle_path)
    slv_to_id = bundle.get("slv_to_id", {}) if bundle else {}

    recent_signals: list[dict[str, Any]] = []
    for sym, res in per_symbol.items():
        g4 = res["g4"]
        n_bars = len(g4)
        min_bar = max(SEQ_LEN, n_bars - max(1, lookback_bars))
        fa4 = _precompute_tf_feature_arrays(g4, build_fp_context(g4), btc_ref)
        sp_bps = float((res.get("spec") or {}).get("sp_bps", 12.0))
        is_metal = 1.0 if (res.get("spec") or {}).get("cat") == "METAL" else 0.0

        for t in res["all_trades"]:
            bi = int(t["bar_i"])
            if bi < min_bar or bi >= n_bars:
                continue
            side = 1.0 if t["side"] == "BUY" else -1.0
            ext = _extract_fa_row_and_seq(fa4, bi, side, str(t["sleeve"]), 4, sp_bps, is_metal, slv_to_id)
            if ext is None:
                continue
            row, seq_m = ext
            score = score_live_signal(row, seq_m, bundle_path=bundle_path)
            recent_signals.append({
                "symbol": sym,
                "mt5_symbol": (res.get("spec") or {}).get("mt5", sym),
                "sleeve": t["sleeve"],
                "side": t["side"],
                "bar_i": bi,
                "bars_ago": int(n_bars - 1 - bi),
                "bar_ts": int(fa4["end_ts"][bi]),
                "bar_iso": datetime.fromtimestamp(int(fa4["end_ts"][bi]), tz=timezone.utc).isoformat(),
                "close": round(float(fa4["c"][bi]), 4),
                **score,
            })

    recent_signals.sort(key=lambda x: (x["bars_ago"], -x["expected_r"]))
    return {
        "evaluated_ts": int(time.time()),
        "symbols": target_syms,
        "lookback_bars": lookback_bars,
        "signals": recent_signals,
    }
