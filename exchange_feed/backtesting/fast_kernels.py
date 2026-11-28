"""Numba JIT + Polars high-speed kernels for 1m simulation, footprint aggregation, and Monte Carlo."""

from __future__ import annotations

import concurrent.futures
import math
from typing import Any

from numba import njit, prange
import numpy as np
import pyarrow.parquet as pq


@njit(cache=True, nogil=True)
def _ss_left_i64(arr: np.ndarray, val: int) -> int:
    lo = 0
    hi = len(arr)
    while lo < hi:
        mid = (lo + hi) >> 1
        if arr[mid] < val:
            lo = mid + 1
        else:
            hi = mid
    return lo


@njit(cache=True, nogil=True)
def _ss_right_i64(arr: np.ndarray, val: int) -> int:
    lo = 0
    hi = len(arr)
    while lo < hi:
        mid = (lo + hi) >> 1
        if arr[mid] <= val:
            lo = mid + 1
        else:
            hi = mid
    return lo


@njit(cache=True, nogil=True)
def ladder_round_nb(e: float) -> float:
    """Coinglass q(e) scale-invariant ladder rounding to {1, 2, 2.5, 5, 10} * 10^k."""
    if not (e > 0.0):
        return 1.0
    t = 10.0 ** math.floor(math.log10(e))
    s = e / t
    if s <= 1.0:
        mult = 1.0
    elif s <= 2.0:
        mult = 2.0
    elif s <= 2.5:
        mult = 2.5
    elif s <= 5.0:
        mult = 5.0
    else:
        mult = 10.0
    return mult * t


@njit(cache=True, nogil=True)
def sim_1m_trades_nb(
    ts: np.ndarray,
    end_ts: np.ndarray,
    atr: np.ndarray,
    ema89_a: np.ndarray,
    ema233_a: np.ndarray,
    c_a: np.ndarray,
    side_arr: np.ndarray,
    m1_ts: np.ndarray,
    m1_o: np.ndarray,
    m1_h: np.ndarray,
    m1_l: np.ndarray,
    m1_c: np.ndarray,
    tf_sec: int,
    sl_atr: float,
    tp_r: float,
    trail_atr: float,
    be_r: float,
    be_pad: float,
    step_lock: bool,
    max_hold: int,
    delay: int,
    dyn_atr: bool,
    use_mt5: bool,
    sp_bps: float,
    realistic_costs: bool,
    cost_bps: float,
    fund_bps_8h: float,
    roll3d: int,
    sw_l_bps: float,
    sw_s_bps: float,
    f_ts: np.ndarray,
    f_rt: np.ndarray,
    closed_bar_replay: bool = False,
):
    """Numba-compiled exact 1m trade replay kernel matching `_sim_sleeve_1m_exact` bit-for-bit."""
    n = len(ts)
    m1_n = len(m1_ts)
    f_n = len(f_ts)
    has_tp = tp_r > 0.0
    has_f = f_n > 0

    max_tr = max(1, n)
    out_bar_i = np.empty(max_tr, dtype=np.int64)
    out_ts_in = np.empty(max_tr, dtype=np.int64)
    out_ts_out = np.empty(max_tr, dtype=np.int64)
    out_side = np.empty(max_tr, dtype=np.int8)
    out_reason = np.empty(max_tr, dtype=np.int8)  # 0=TIME, 1=SL, 2=TRAIL, 3=TP
    out_entry = np.empty(max_tr, dtype=np.float64)
    out_ex_px = np.empty(max_tr, dtype=np.float64)
    out_r = np.empty(max_tr, dtype=np.float64)
    out_fee_r = np.empty(max_tr, dtype=np.float64)
    out_reg = np.empty(max_tr, dtype=np.int8)  # 0=TRANSITION, 1=BULL, 2=BEAR
    cnt = 0

    i = 235
    signal_limit = n if closed_bar_replay else n - 3
    entry_limit = m1_n if closed_bar_replay else m1_n - 10
    while i < signal_limit:
        side = int(side_arr[i])
        ai = atr[i]
        if side == 0 or math.isnan(ai) or ai <= 0.0:
            i += 1
            continue

        k_in = _ss_left_i64(m1_ts, int(end_ts[i])) + delay
        if k_in >= entry_limit:
            break

        entry = m1_o[k_in]
        if closed_bar_replay and (not math.isfinite(entry) or entry <= 0 or m1_ts[k_in] - end_ts[i] > 300):
            i += 1
            continue
        sd = sl_atr * ai
        sl = entry - side * sd
        tp = entry + side * (tp_r * sd) if has_tp else 0.0
        hold_end = int(m1_ts[k_in]) + max_hold * tf_sec
        k_max = _ss_left_i64(m1_ts, hold_end) if closed_bar_replay else _ss_right_i64(m1_ts, hold_end)
        if k_max >= m1_n:
            k_max = m1_n - 1

        k_ex = k_max
        ex_px = entry
        ex_code = 0  # TIME
        best = entry
        trailed = False
        prev_b = int(m1_ts[k_in]) // tf_sec
        cur_bar_idx = i

        for k in range(k_in, k_max + 1):
            hi = m1_h[k]
            lo = m1_l[k]
            if side == 1:
                if lo <= sl:
                    ex_px = min(sl, m1_o[k]) if closed_bar_replay else sl
                    ex_code = 2 if trailed else 1
                    k_ex = k
                    break
                if has_tp and hi >= tp:
                    ex_px = tp
                    ex_code = 3
                    k_ex = k
                    break
                if hi > best:
                    best = hi
                cur_b = int(m1_ts[k]) // tf_sec
                if cur_b != prev_b:
                    if dyn_atr:
                        while cur_bar_idx + 1 < n and (end_ts[cur_bar_idx + 1] <= m1_ts[k] if closed_bar_replay
                                                       else (int(ts[cur_bar_idx + 1]) // tf_sec) < cur_b):
                            cur_bar_idx += 1
                        ac = atr[cur_bar_idx]
                        tr_a = ac if not math.isnan(ac) else ai
                    else:
                        tr_a = ai
                    mfe_r = (best - entry) / sd
                    if be_r > 0.0 and mfe_r >= be_r:
                        nsl = entry + be_pad * sd
                        if nsl > sl:
                            sl = nsl
                            trailed = True
                    if step_lock and mfe_r >= 2.0:
                        nsl = entry + 1.0 * sd
                        if nsl > sl:
                            sl = nsl
                            trailed = True
                    if trail_atr > 0.0:
                        nsl = best - trail_atr * tr_a
                        if nsl > sl:
                            sl = nsl
                            trailed = True
                prev_b = cur_b
            else:
                if hi >= sl:
                    ex_px = max(sl, m1_o[k]) if closed_bar_replay else sl
                    ex_code = 2 if trailed else 1
                    k_ex = k
                    break
                if has_tp and lo <= tp:
                    ex_px = tp
                    ex_code = 3
                    k_ex = k
                    break
                if lo < best:
                    best = lo
                cur_b = int(m1_ts[k]) // tf_sec
                if cur_b != prev_b:
                    if dyn_atr:
                        while cur_bar_idx + 1 < n and (end_ts[cur_bar_idx + 1] <= m1_ts[k] if closed_bar_replay
                                                       else (int(ts[cur_bar_idx + 1]) // tf_sec) < cur_b):
                            cur_bar_idx += 1
                        ac = atr[cur_bar_idx]
                        tr_a = ac if not math.isnan(ac) else ai
                    else:
                        tr_a = ai
                    mfe_r = (entry - best) / sd
                    if be_r > 0.0 and mfe_r >= be_r:
                        nsl = entry - be_pad * sd
                        if nsl < sl:
                            sl = nsl
                            trailed = True
                    if step_lock and mfe_r >= 2.0:
                        nsl = entry - 1.0 * sd
                        if nsl < sl:
                            sl = nsl
                            trailed = True
                    if trail_atr > 0.0:
                        nsl = best + trail_atr * tr_a
                        if nsl < sl:
                            sl = nsl
                            trailed = True
                prev_b = cur_b

        if ex_code == 0:
            if closed_bar_replay and m1_ts[k_ex] < hold_end:
                break  # The position is still open at the observed boundary.
            ex_px = m1_c[k_ex]
        raw_r = ((ex_px - entry) * side) / sd

        if use_mt5:
            in_bps = 0.5 * sp_bps + 2.0
            if ex_code == 3:
                out_bps = 0.5 * sp_bps
            elif ex_code == 2:
                out_bps = 0.5 * sp_bps + 4.0
            elif ex_code == 1:
                spike = 5.0 if (m1_h[k_ex] - m1_l[k_ex]) > 0.4 * ai else 0.0
                out_bps = 0.5 * sp_bps + 8.0 + spike
            else:
                out_bps = 0.5 * sp_bps + 2.0
            eff_cost_bps = in_bps + out_bps
        elif realistic_costs:
            in_bps = 6.5
            if ex_code == 3:
                out_bps = 2.0
            elif ex_code == 2:
                out_bps = 8.5
            elif ex_code == 1:
                spike = 5.0 if (m1_h[k_ex] - m1_l[k_ex]) > 0.4 * ai else 0.0
                out_bps = 15.0 + spike
            else:
                out_bps = 6.5
            eff_cost_bps = in_bps + out_bps
        else:
            eff_cost_bps = cost_bps

        fee_r = (eff_cost_bps / 10000.0 * entry) / sd

        ts_in_v = int(m1_ts[k_in])
        ts_ex_v = int(m1_ts[k_ex])
        if use_mt5:
            d_in = ts_in_v // 86400
            d_ex = ts_ex_v // 86400
            swap_days = 0
            for d in range(d_in, d_ex):
                wd = (d + 4) % 7
                swap_days += 3 if wd == roll3d else 1
            sw_bps = sw_l_bps if side == 1 else sw_s_bps
            fund_r = ((-sw_bps * swap_days) / 10000.0 * entry) / sd
        elif has_f and ts_ex_v <= int(f_ts[f_n - 1]):
            fl = _ss_right_i64(f_ts, ts_in_v)
            fr = _ss_right_i64(f_ts, ts_ex_v)
            f_sum = 0.0
            for fi in range(fl, fr):
                f_sum += f_rt[fi]
            fund_r = (side * f_sum * entry) / sd
        else:
            hold_hrs = (ts_ex_v - ts_in_v) / 3600.0
            if hold_hrs < 0.0:
                hold_hrs = 0.0
            fund_r = ((fund_bps_8h * (hold_hrs / 8.0)) / 10000.0 * entry) / sd

        e89 = ema89_a[i]
        e233 = ema233_a[i]
        ci = c_a[i]
        if (not math.isnan(e89)) and (not math.isnan(e233)):
            if e89 > e233 and ci > e233:
                reg = 1
            elif e89 < e233 and ci < e233:
                reg = 2
            else:
                reg = 0
        else:
            reg = 0

        out_bar_i[cnt] = i
        out_ts_in[cnt] = ts_in_v
        out_ts_out[cnt] = ts_ex_v
        out_side[cnt] = side
        out_reason[cnt] = ex_code
        out_entry[cnt] = entry
        out_ex_px[cnt] = ex_px
        out_r[cnt] = raw_r - fee_r - fund_r
        out_fee_r[cnt] = fee_r + fund_r
        out_reg[cnt] = reg
        cnt += 1

        nxt = _ss_right_i64(ts, ts_ex_v)
        i = nxt if nxt > i + 1 else (i + 1)

    return (
        out_bar_i[:cnt], out_ts_in[:cnt], out_ts_out[:cnt], out_side[:cnt],
        out_reason[:cnt], out_entry[:cnt], out_ex_px[:cnt], out_r[:cnt],
        out_fee_r[:cnt], out_reg[:cnt],
    )


@njit(cache=True, nogil=True)
def reduce_month_tf_nb(
    bar_1m: np.ndarray,
    price_bucket: np.ndarray,
    buy_trades: np.ndarray,
    sell_trades: np.ndarray,
    buy_qty: np.ndarray,
    sell_qty: np.ndarray,
    o_a: np.ndarray,
    h_a: np.ndarray,
    l_a: np.ndarray,
    c_a: np.ndarray,
    tf_sec: int,
    bps_tick: float,
    adaptive_tick: bool,
    fixed_tick: float,
):
    """Stream one sorted monthly 1m price-bucket array into M1 OHLC + compact TF (ts, pb, bv, sv) cells."""
    n = len(bar_1m)
    if n == 0:
        return (
            np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64),
            np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64),
            np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64),
            np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64),
            np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64),
            np.empty(0, dtype=np.float64),
        )

    # 1) M1 OHLCV pass (sorted by bar_1m)
    m1_ts = np.empty(n, dtype=np.int64)
    m1_o = np.empty(n, dtype=np.float64)
    m1_h = np.empty(n, dtype=np.float64)
    m1_l = np.empty(n, dtype=np.float64)
    m1_c = np.empty(n, dtype=np.float64)
    m1_cnt = 0

    tf_ts = np.empty(n, dtype=np.int64)
    tf_o = np.empty(n, dtype=np.float64)
    tf_h = np.empty(n, dtype=np.float64)
    tf_l = np.empty(n, dtype=np.float64)
    tf_c = np.empty(n, dtype=np.float64)
    tf_bv = np.empty(n, dtype=np.float64)
    tf_sv = np.empty(n, dtype=np.float64)
    tf_nt = np.empty(n, dtype=np.int64)
    tf_cnt = 0

    cur_m = int(bar_1m[0])
    cur_t = (cur_m // tf_sec) * tf_sec
    mo, mh, ml, mc = o_a[0], h_a[0], l_a[0], c_a[0]
    to, th, tl, tc = o_a[0], h_a[0], l_a[0], c_a[0]
    tbv = 0.0
    tsv = 0.0
    tnt = 0

    for k in range(n):
        bm = int(bar_1m[k])
        bt = (bm // tf_sec) * tf_sec
        if bm != cur_m:
            m1_ts[m1_cnt] = cur_m
            m1_o[m1_cnt] = mo
            m1_h[m1_cnt] = mh
            m1_l[m1_cnt] = ml
            m1_c[m1_cnt] = mc
            m1_cnt += 1
            cur_m = bm
            mo, mh, ml, mc = o_a[k], h_a[k], l_a[k], c_a[k]
        else:
            if h_a[k] > mh:
                mh = h_a[k]
            if l_a[k] < ml:
                ml = l_a[k]
            mc = c_a[k]

        if bt != cur_t:
            tf_ts[tf_cnt] = cur_t
            tf_o[tf_cnt] = to
            tf_h[tf_cnt] = th
            tf_l[tf_cnt] = tl
            tf_c[tf_cnt] = tc
            tf_bv[tf_cnt] = tbv
            tf_sv[tf_cnt] = tsv
            tf_nt[tf_cnt] = tnt
            tf_cnt += 1
            cur_t = bt
            to, th, tl, tc = o_a[k], h_a[k], l_a[k], c_a[k]
            tbv = buy_qty[k]
            tsv = sell_qty[k]
            tnt = int(buy_trades[k]) + int(sell_trades[k])
        else:
            if h_a[k] > th:
                th = h_a[k]
            if l_a[k] < tl:
                tl = l_a[k]
            tc = c_a[k]
            tbv += buy_qty[k]
            tsv += sell_qty[k]
            tnt += int(buy_trades[k]) + int(sell_trades[k])

    m1_ts[m1_cnt] = cur_m
    m1_o[m1_cnt] = mo
    m1_h[m1_cnt] = mh
    m1_l[m1_cnt] = ml
    m1_c[m1_cnt] = mc
    m1_cnt += 1

    tf_ts[tf_cnt] = cur_t
    tf_o[tf_cnt] = to
    tf_h[tf_cnt] = th
    tf_l[tf_cnt] = tl
    tf_c[tf_cnt] = tc
    tf_bv[tf_cnt] = tbv
    tf_sv[tf_cnt] = tsv
    tf_nt[tf_cnt] = tnt
    tf_cnt += 1

    tf_ticks = np.empty(tf_cnt, dtype=np.float64)
    for j in range(tf_cnt):
        if adaptive_tick:
            floor_tick = 5.0 if tf_o[j] >= 10000.0 else 1e-6
            tk = ladder_round_nb(tf_o[j] * bps_tick / 10000.0)
            tf_ticks[j] = tk if tk > floor_tick else floor_tick
        else:
            tf_ticks[j] = fixed_tick

    # Compute price_bucket floored onto TF tick grid for every row
    c_ts = (bar_1m // tf_sec) * tf_sec
    c_pb = np.empty(n, dtype=np.float64)
    t_idx = 0
    for k in range(n):
        bt = int(c_ts[k])
        while t_idx + 1 < tf_cnt and tf_ts[t_idx] != bt:
            t_idx += 1
        tk = tf_ticks[t_idx]
        c_pb[k] = math.floor(price_bucket[k] / tk) * tk

    return (
        m1_ts[:m1_cnt], m1_o[:m1_cnt], m1_h[:m1_cnt], m1_l[:m1_cnt], m1_c[:m1_cnt],
        tf_ts[:tf_cnt], tf_ticks, tf_o[:tf_cnt], tf_h[:tf_cnt], tf_l[:tf_cnt], tf_c[:tf_cnt],
        tf_bv[:tf_cnt], tf_sv[:tf_cnt], tf_nt[:tf_cnt],
        c_ts, c_pb, buy_qty, sell_qty,
    )


@njit(cache=True, nogil=True)
def agg_tf_meta_and_cg_nb(
    g_ts: np.ndarray,
    g_o: np.ndarray,
    g_h: np.ndarray,
    g_l: np.ndarray,
    g_c: np.ndarray,
    g_ticks: np.ndarray,
    lvl_ts: np.ndarray,
    lvl_pb: np.ndarray,
    lvl_bv: np.ndarray,
    lvl_sv: np.ndarray,
):
    """Compute POC, zero-auctions, imbalances, and all 10 Coinglass signals on grouped TF levels."""
    n_g = len(g_ts)
    poc_out = np.full(n_g, np.nan, dtype=np.float64)
    ztop_out = np.zeros(n_g, dtype=np.bool_)
    zbot_out = np.zeros(n_g, dtype=np.bool_)
    bi_out = np.zeros(n_g, dtype=np.int64)
    si_out = np.zeros(n_g, dtype=np.int64)

    cg_abs_b = np.zeros(n_g, dtype=np.bool_)
    cg_abs_s = np.zeros(n_g, dtype=np.bool_)
    cg_div_b = np.zeros(n_g, dtype=np.bool_)
    cg_div_s = np.zeros(n_g, dtype=np.bool_)
    cg_exh_b = np.zeros(n_g, dtype=np.bool_)
    cg_exh_s = np.zeros(n_g, dtype=np.bool_)
    cg_trap_b = np.zeros(n_g, dtype=np.bool_)
    cg_trap_s = np.zeros(n_g, dtype=np.bool_)
    cg_sdiv_b = np.zeros(n_g, dtype=np.bool_)
    cg_sdiv_s = np.zeros(n_g, dtype=np.bool_)

    n_l = len(lvl_ts)
    if n_g == 0 or n_l == 0:
        return (
            poc_out, ztop_out, zbot_out, bi_out, si_out,
            cg_abs_b, cg_abs_s, cg_div_b, cg_div_s, cg_exh_b, cg_exh_s,
            cg_trap_b, cg_trap_s, cg_sdiv_b, cg_sdiv_s,
        )

    # Identify unique level runs matching bars in g_ts
    g_idx_of_bar = np.empty(n_g, dtype=np.int64)
    bar_s = np.empty(n_g, dtype=np.int64)
    bar_e = np.empty(n_g, dtype=np.int64)
    t_bars = 0
    gi = 0
    li = 0
    while li < n_l:
        cur_ts = int(lvl_ts[li])
        lj = li + 1
        while lj < n_l and int(lvl_ts[lj]) == cur_ts:
            lj += 1
        while gi < n_g and int(g_ts[gi]) < cur_ts:
            gi += 1
        if gi < n_g and int(g_ts[gi]) == cur_ts:
            g_idx_of_bar[t_bars] = gi
            bar_s[t_bars] = li
            bar_e[t_bars] = lj
            t_bars += 1
        li = lj

    vol = np.zeros(t_bars, dtype=np.float64)
    cvd = np.zeros(t_bars, dtype=np.float64)
    tot_b_arr = np.zeros(t_bars, dtype=np.float64)
    tot_s_arr = np.zeros(t_bars, dtype=np.float64)

    acc_cvd = 0.0
    for r in range(t_bars):
        gi = g_idx_of_bar[r]
        s_i = bar_s[r]
        e_i = bar_e[r]
        tick = g_ticks[gi]
        lo = lvl_pb[s_i]
        hi_p = lvl_pb[e_i - 1]
        nb = int(round((hi_p - lo) / tick)) + 1
        if nb < 1:
            nb = 1
        if nb > 20000:
            nb = 20000
        ba = np.zeros(nb, dtype=np.float64)
        sa = np.zeros(nb, dtype=np.float64)
        tb = 0.0
        tsum = 0.0
        for k in range(s_i, e_i):
            idx = int(round((lvl_pb[k] - lo) / tick))
            if idx < 0:
                idx = 0
            elif idx >= nb:
                idx = nb - 1
            bv = lvl_bv[k]
            sv = lvl_sv[k]
            ba[idx] += bv
            sa[idx] += sv
            tb += bv
            tsum += sv
        tot_b_arr[r] = tb
        tot_s_arr[r] = tsum
        vol[r] = tb + tsum
        acc_cvd += (tb - tsum)
        cvd[r] = acc_cvd

        best_k = 0
        best_v = -1.0
        for k in range(nb):
            tv = ba[k] + sa[k]
            if tv > best_v:
                best_v = tv
                best_k = k
        poc_out[gi] = lo + best_k * tick
        ztop_out[gi] = (sa[nb - 1] == 0.0) and (ba[nb - 1] > 0.0)
        zbot_out[gi] = (ba[0] == 0.0) and (sa[0] > 0.0)

        bi_c = 0
        for k in range(1, nb):
            if ba[k] >= 1.0 and sa[k - 1] > 0.0 and ba[k] >= 3.0 * sa[k - 1]:
                bi_c += 1
        si_c = 0
        for k in range(0, nb - 1):
            if sa[k] >= 1.0 and ba[k + 1] > 0.0 and sa[k] >= 3.0 * ba[k + 1]:
                si_c += 1
        bi_out[gi] = bi_c
        si_out[gi] = si_c

    # Coinglass signals on valid footprint bars `0..t_bars-1`
    prior_buf = np.empty(20, dtype=np.float64)
    for l_idx in range(5, t_bars - 1):
        ov = vol[l_idx]
        if ov <= 0.0:
            continue
        p_cnt = 0
        st_p = l_idx - 20 if l_idx >= 20 else 0
        for e in range(st_p, l_idx):
            if vol[e] > 0.0:
                prior_buf[p_cnt] = vol[e]
                p_cnt += 1
        if p_cnt < 5:
            continue
        sp = np.sort(prior_buf[:p_cnt])
        if ov < 0.8 * sp[p_cnt >> 1]:
            continue

        gi = g_idx_of_bar[l_idx]
        s_i = bar_s[l_idx]
        e_i = bar_e[l_idx]
        tick = g_ticks[gi]
        lo = lvl_pb[s_i]
        hi_p = lvl_pb[e_i - 1]
        nb = int(round((hi_p - lo) / tick)) + 1
        if nb < 1:
            nb = 1
        if nb > 20000:
            nb = 20000
        ba = np.zeros(nb, dtype=np.float64)
        sa = np.zeros(nb, dtype=np.float64)
        for k in range(s_i, e_i):
            idx = int(round((lvl_pb[k] - lo) / tick))
            if idx < 0:
                idx = 0
            elif idx >= nb:
                idx = nb - 1
            ba[idx] += lvl_bv[k]
            sa[idx] += lvl_sv[k]

        p_top = -1
        u_bot = -1
        for k in range(nb):
            if (ba[k] + sa[k]) > 0.0:
                if u_bot < 0:
                    u_bot = k
                p_top = k
        if p_top < 0:
            continue

        tot_b = tot_b_arr[l_idx]
        tot_s = tot_s_arr[l_idx]
        bar_delta = tot_b - tot_s
        f_open = g_o[gi]
        g_high = g_h[gi]
        m_low = g_l[gi]
        b_close = g_c[gi]
        rng = g_high - m_low
        close_pos = (b_close - m_low) / rng if rng > 0.0 else 0.5
        w_rows = p_top - u_bot + 1

        if b_close > f_open and bar_delta < 0.0 and (-bar_delta) >= 0.15 * ov:
            cg_div_s[gi] = True
        if b_close < f_open and bar_delta > 0.0 and bar_delta >= 0.15 * ov:
            cg_div_b[gi] = True

        if w_rows < 5 or rng <= 0.0:
            continue

        zone_n = int(round(0.2 * w_rows))
        if zone_n < 2:
            zone_n = 2
        t_top_b = 0.0
        s_top_s = 0.0
        for e in range(p_top - zone_n + 1, p_top + 1):
            t_top_b += ba[e]
            s_top_s += sa[e]
        j_bot_b = 0.0
        c_bot_s = 0.0
        for e in range(u_bot, u_bot + zone_n):
            j_bot_b += ba[e]
            c_bot_s += sa[e]

        if tot_s > 0.0 and (c_bot_s / tot_s) >= 0.3 and c_bot_s > 1.2 * j_bot_b and close_pos >= 0.6:
            cg_abs_b[gi] = True
        if tot_b > 0.0 and (t_top_b / tot_b) >= 0.3 and t_top_b > 1.2 * s_top_s and close_pos <= 0.4:
            cg_abs_s[gi] = True

        st10 = l_idx - 10 if l_idx >= 10 else 0
        prev_hi = -1e18
        prev_lo = 1e18
        for e in range(st10, l_idx):
            ge = g_idx_of_bar[e]
            if g_h[ge] > prev_hi:
                prev_hi = g_h[ge]
            if g_l[ge] < prev_lo:
                prev_lo = g_l[ge]
        d_rows = int(round(0.1 * w_rows))
        if d_rows < 1:
            d_rows = 1
        top_ext = 0.0
        for e in range(p_top - d_rows + 1, p_top + 1):
            top_ext += ba[e] + sa[e]
        bot_ext = 0.0
        for e in range(u_bot, u_bot + d_rows):
            bot_ext += ba[e] + sa[e]
        if g_high > prev_hi and top_ext <= 0.02 * ov and close_pos <= 0.5:
            cg_exh_s[gi] = True
        if m_low < prev_lo and bot_ext <= 0.02 * ov and close_pos >= 0.5:
            cg_exh_b[gi] = True

        st5 = l_idx - 5 if l_idx >= 5 else 0
        p5 = -1e18
        a5 = 1e18
        for e in range(st5, l_idx):
            ge = g_idx_of_bar[e]
            if g_h[ge] > p5:
                p5 = g_h[ge]
            if g_l[ge] < a5:
                a5 = g_l[ge]
        end_i2 = l_idx + 4 if (l_idx + 4) < (t_bars - 1) else (t_bars - 1)
        if g_high > p5 and tot_b > 0.0 and (t_top_b / tot_b) >= 0.3 and t_top_b > 1.5 * s_top_s:
            for i2 in range(l_idx + 1, end_i2):
                gi2 = g_idx_of_bar[i2]
                if g_h[gi2] > g_high:
                    break
                if g_c[gi2] <= m_low + 0.25 * rng:
                    cg_trap_s[gi2] = True
                    break
        if m_low < a5 and tot_s > 0.0 and (c_bot_s / tot_s) >= 0.3 and c_bot_s > 1.5 * j_bot_b:
            for i2 in range(l_idx + 1, end_i2):
                gi2 = g_idx_of_bar[i2]
                if g_l[gi2] < m_low:
                    break
                if g_c[gi2] >= g_high - 0.25 * rng:
                    cg_trap_b[gi2] = True
                    break

    # sdiv causal assignment (index + 3)
    last_hi = -1
    last_lo = -1
    for a in range(3, t_bars - 4):
        ga = g_idx_of_bar[a]
        is_hi = True
        is_lo = True
        for e in range(a - 3, a + 4):
            if e == a:
                continue
            ge = g_idx_of_bar[e]
            if (g_h[ge] >= g_h[ga]) if e < a else (g_h[ge] > g_h[ga]):
                is_hi = False
            if (g_l[ge] <= g_l[ga]) if e < a else (g_l[ge] < g_l[ga]):
                is_lo = False
        if not is_hi and not is_lo:
            continue
        p_cnt = 0
        st_p = a - 20 if a >= 20 else 0
        for e in range(st_p, a):
            if vol[e] > 0.0:
                prior_buf[p_cnt] = vol[e]
                p_cnt += 1
        thr = 0.0
        if p_cnt > 0:
            sp = np.sort(prior_buf[:p_cnt])
            thr = 0.25 * sp[p_cnt >> 1]
        c_idx = ga + 3
        if is_hi:
            if last_hi >= 0 and (a - last_hi) <= 40:
                glh = g_idx_of_bar[last_hi]
                if g_h[ga] > g_h[glh] and (cvd[last_hi] - cvd[a]) > thr and c_idx < n_g:
                    cg_sdiv_s[c_idx] = True
            last_hi = a
        if is_lo:
            if last_lo >= 0 and (a - last_lo) <= 40:
                gll = g_idx_of_bar[last_lo]
                if g_l[ga] < g_l[gll] and (cvd[a] - cvd[last_lo]) > thr and c_idx < n_g:
                    cg_sdiv_b[c_idx] = True
            last_lo = a

    return (
        poc_out, ztop_out, zbot_out, bi_out, si_out,
        cg_abs_b, cg_abs_s, cg_div_b, cg_div_s, cg_exh_b, cg_exh_s,
        cg_trap_b, cg_trap_s, cg_sdiv_b, cg_sdiv_s,
    )


def _process_one_parquet_chunk(
    path_str: str,
    cols: list[str],
    min_bar: int = -1,
    adaptive_tick: bool = True,
) -> tuple[Any, Any, Any]:
    """Read one monthly (or tail) parquet via PyArrow and reduce 1m + 2H + 4H in Numba (nogil)."""
    import polars as pl

    flt = [("bar_1m", ">", min_bar)] if min_bar >= 0 else None
    tbl = pq.read_table(path_str, columns=cols, filters=flt)
    if tbl.num_rows == 0:
        return None, None, None
    b1 = tbl["bar_1m"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    pb = tbl["price_bucket"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    bt = tbl["buy_trades"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    st = tbl["sell_trades"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    bq = tbl["buy_qty"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    sq = tbl["sell_qty"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    oa = tbl["open"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    ha = tbl["high"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    la = tbl["low"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    ca = tbl["close"].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)

    # Ensure monotonic sort if unordered tail
    if len(b1) > 1 and np.any(b1[1:] < b1[:-1]):
        order = np.lexsort((pb, b1))
        b1, pb, bt, st, bq, sq, oa, ha, la, ca = (
            b1[order], pb[order], bt[order], st[order], bq[order], sq[order], oa[order], ha[order], la[order], ca[order],
        )

    r2 = reduce_month_tf_nb(b1, pb, bt, st, bq, sq, oa, ha, la, ca, 7200, 10.0, adaptive_tick, 60.0)
    r4 = reduce_month_tf_nb(b1, pb, bt, st, bq, sq, oa, ha, la, ca, 14400, 16.0, adaptive_tick, 100.0)

    c2 = (
        pl.DataFrame({"ts": r2[14], "pb": r2[15], "bv": r2[16], "sv": r2[17]})
        .group_by(["ts", "pb"])
        .sum()
    )
    c4 = (
        pl.DataFrame({"ts": r4[14], "pb": r4[15], "bv": r4[16], "sv": r4[17]})
        .group_by(["ts", "pb"])
        .sum()
    )
    m1_tup = r2[:5]
    b2_tup = r2[5:14]
    b4_tup = r4[5:14]
    return m1_tup, (b2_tup, c2), (b4_tup, c4)


def parallel_build_asset_tf_arrays(
    monthly_paths: list[str],
    tail_path: str | None,
    adaptive_tick: bool = True,
    max_workers: int = 6,
) -> tuple[dict[str, np.ndarray], dict[str, Any], dict[str, Any]]:
    """Parallel multi-month PyArrow + Numba + Polars reduction into M1, 2H, and 4H arrays."""
    cols = ["bar_1m", "price_bucket", "buy_trades", "sell_trades", "buy_qty", "sell_qty", "open", "high", "low", "close"]
    max_m_bar = -1
    if monthly_paths:
        last_b = pq.read_table(monthly_paths[-1], columns=["bar_1m"])["bar_1m"].to_numpy(zero_copy_only=False)
        if len(last_b):
            max_m_bar = int(last_b.max())

    jobs: list[tuple[str, int]] = [(p, -1) for p in monthly_paths]
    if tail_path:
        jobs.append((tail_path, max_m_bar))

    results: list[Any] = [None] * len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(jobs)))) as ex:
        futs = {ex.submit(_process_one_parquet_chunk, path_s, cols, mb, adaptive_tick): idx for idx, (path_s, mb) in enumerate(jobs)}
        for fut in concurrent.futures.as_completed(futs):
            results[futs[fut]] = fut.result()

    import polars as pl

    m1_ts_l, m1_o_l, m1_h_l, m1_l_l, m1_c_l = [], [], [], [], []
    b2_parts, c2_parts = [], []
    b4_parts, c4_parts = [], []
    for res in results:
        if not res or res[0] is None:
            continue
        m1_t, d2, d4 = res
        m1_ts_l.append(m1_t[0])
        m1_o_l.append(m1_t[1])
        m1_h_l.append(m1_t[2])
        m1_l_l.append(m1_t[3])
        m1_c_l.append(m1_t[4])
        b2_parts.append(d2[0])
        c2_parts.append(d2[1])
        b4_parts.append(d4[0])
        c4_parts.append(d4[1])

    m1_df = pl.DataFrame({
        "bar_1m": np.concatenate(m1_ts_l),
        "o": np.concatenate(m1_o_l),
        "h": np.concatenate(m1_h_l),
        "l": np.concatenate(m1_l_l),
        "c": np.concatenate(m1_c_l),
    }).unique(subset=["bar_1m"], keep="last").sort("bar_1m")

    def _assemble_tf(b_parts, c_parts, dur_m: int) -> dict[str, np.ndarray]:
        ts_a = np.concatenate([p[0] for p in b_parts])
        tk_a = np.concatenate([p[1] for p in b_parts])
        o_a = np.concatenate([p[2] for p in b_parts])
        h_a = np.concatenate([p[3] for p in b_parts])
        l_a = np.concatenate([p[4] for p in b_parts])
        c_a = np.concatenate([p[5] for p in b_parts])
        bv_a = np.concatenate([p[6] for p in b_parts])
        sv_a = np.concatenate([p[7] for p in b_parts])
        nt_a = np.concatenate([p[8] for p in b_parts])
        g_pl = (
            pl.DataFrame({"ts": ts_a, "tk": tk_a, "o": o_a, "h": h_a, "l": l_a, "c": c_a, "bv": bv_a, "sv": sv_a, "nt": nt_a})
            .group_by("ts")
            .agg([
                pl.col("tk").first().alias("tk"),
                pl.col("o").first().alias("o"),
                pl.col("h").max().alias("h"),
                pl.col("l").min().alias("l"),
                pl.col("c").last().alias("c"),
                pl.col("bv").sum().alias("bv"),
                pl.col("sv").sum().alias("sv"),
                pl.col("nt").sum().alias("nt"),
            ])
            .sort("ts")
        )
        c_pl = (
            pl.concat(c_parts)
            .group_by(["ts", "pb"])
            .sum()
            .sort(["ts", "pb"])
        )
        g_ts = g_pl["ts"].to_numpy()
        g_tk = g_pl["tk"].to_numpy()
        g_o = g_pl["o"].to_numpy()
        g_h = g_pl["h"].to_numpy()
        g_l = g_pl["l"].to_numpy()
        g_c = g_pl["c"].to_numpy()
        meta = agg_tf_meta_and_cg_nb(
            g_ts, g_o, g_h, g_l, g_c, g_tk,
            c_pl["ts"].to_numpy(), c_pl["pb"].to_numpy(), c_pl["bv"].to_numpy(), c_pl["sv"].to_numpy(),
        )
        g_bv = g_pl["bv"].to_numpy()
        g_sv = g_pl["sv"].to_numpy()
        g_nt = g_pl["nt"].to_numpy()
        tf_sec = dur_m * 60
        return {
            "ts": g_ts, "o": g_o, "h": g_h, "l": g_l, "c": g_c,
            "bv": g_bv, "sv": g_sv, "v": g_bv + g_sv, "nt": g_nt,
            "end_ts": g_ts + tf_sec, "dur_m": np.full(len(g_ts), dur_m, dtype=np.int64),
            "poc": meta[0], "zero_top": meta[1], "zero_bot": meta[2], "buy_imb": meta[3], "sell_imb": meta[4],
            "cg_abs_b": meta[5], "cg_abs_s": meta[6], "cg_div_b": meta[7], "cg_div_s": meta[8],
            "cg_exh_b": meta[9], "cg_exh_s": meta[10], "cg_trap_b": meta[11], "cg_trap_s": meta[12],
            "cg_sdiv_b": meta[13], "cg_sdiv_s": meta[14],
        }

    m1_dict = {
        "bar_1m": m1_df["bar_1m"].to_numpy(),
        "o": m1_df["o"].to_numpy(),
        "h": m1_df["h"].to_numpy(),
        "l": m1_df["l"].to_numpy(),
        "c": m1_df["c"].to_numpy(),
    }
    return m1_dict, _assemble_tf(b2_parts, c2_parts, 120), _assemble_tf(b4_parts, c4_parts, 240)


@njit(cache=True, nogil=True)
def sim_ohlc_trades_nb(
    ts_sec: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    atr: np.ndarray,
    side_arr: np.ndarray,
    rd_arr: np.ndarray,
    sl_atr: float,
    tp_r: float,
    trail_atr: float,
    be_r: float,
    be_pad: float,
    max_hold: int,
    sp_bps: float,
    sw_l_bps: float,
    sw_s_bps: float,
    roll3d: int,
    cooldown_bars: int,
):
    """Numba-compiled closed-bar next-open OHLC trade simulator with MT5 spread, swap & slippage."""
    n = len(c)
    max_tr = max(1, n // 2 + 1)
    out_bi = np.empty(max_tr, dtype=np.int64)
    out_ti = np.empty(max_tr, dtype=np.int64)
    out_to = np.empty(max_tr, dtype=np.int64)
    out_sd = np.empty(max_tr, dtype=np.int8)
    out_r = np.empty(max_tr, dtype=np.float64)
    out_fr = np.empty(max_tr, dtype=np.float64)
    cnt = 0

    has_tp = tp_r > 0.0
    i = 205
    while i < n - 3:
        sd = int(side_arr[i])
        ai = atr[i]
        if sd == 0 or math.isnan(ai) or ai <= 0.0:
            i += 1
            continue
        rd = rd_arr[i] if rd_arr[i] > 0.0 else (sl_atr * ai)
        if rd <= 0.0:
            i += 1
            continue

        k_in = i + 1
        entry = o[k_in]
        if entry <= 0.0:
            i += 1
            continue
        sl = entry - sd * rd
        tp = entry + sd * (tp_r * rd) if has_tp else 0.0
        k_max = min(n - 1, k_in + max_hold)
        k_ex = k_max
        ex_px = c[k_max]
        ex_code = 0  # 0=TIME, 1=SL, 2=TRAIL, 3=TP
        best = entry
        trailed = False

        for k in range(k_in, k_max + 1):
            hi = h[k]
            lo = l[k]
            if sd == 1:
                if lo <= sl:
                    ex_px = sl
                    ex_code = 2 if trailed else 1
                    k_ex = k
                    break
                if has_tp and hi >= tp:
                    ex_px = tp
                    ex_code = 3
                    k_ex = k
                    break
                if hi > best:
                    best = hi
                mfe_r = (best - entry) / rd
                if be_r > 0.0 and mfe_r >= be_r:
                    nsl = entry + be_pad * rd
                    if nsl > sl:
                        sl = nsl
                        trailed = True
                if trail_atr > 0.0:
                    ak = atr[k] if not math.isnan(atr[k]) and atr[k] > 0.0 else ai
                    nsl = best - trail_atr * ak
                    if nsl > sl:
                        sl = nsl
                        trailed = True
            else:
                if hi >= sl:
                    ex_px = sl
                    ex_code = 2 if trailed else 1
                    k_ex = k
                    break
                if has_tp and lo <= tp:
                    ex_px = tp
                    ex_code = 3
                    k_ex = k
                    break
                if lo < best:
                    best = lo
                mfe_r = (entry - best) / rd
                if be_r > 0.0 and mfe_r >= be_r:
                    nsl = entry - be_pad * rd
                    if nsl < sl:
                        sl = nsl
                        trailed = True
                if trail_atr > 0.0:
                    ak = atr[k] if not math.isnan(atr[k]) and atr[k] > 0.0 else ai
                    nsl = best + trail_atr * ak
                    if nsl < sl:
                        sl = nsl
                        trailed = True

        raw_r = ((ex_px - entry) * sd) / rd
        in_bps = 0.5 * sp_bps + 1.5
        if ex_code == 3:
            out_bps = 0.5 * sp_bps
        elif ex_code == 2:
            out_bps = 0.5 * sp_bps + 3.0
        elif ex_code == 1:
            out_bps = 0.5 * sp_bps + 6.0
        else:
            out_bps = 0.5 * sp_bps + 1.5
        fee_r = ((in_bps + out_bps) / 10000.0 * entry) / rd

        ts_in_v = int(ts_sec[k_in])
        ts_ex_v = int(ts_sec[k_ex])
        d_in = ts_in_v // 86400
        d_ex = ts_ex_v // 86400
        swap_days = 0
        for d in range(d_in, d_ex):
            wd = (d + 4) % 7
            swap_days += 3 if wd == roll3d else 1
        sw_bps = sw_l_bps if sd == 1 else sw_s_bps
        fund_r = ((-sw_bps * swap_days) / 10000.0 * entry) / rd

        out_bi[cnt] = i
        out_ti[cnt] = ts_in_v
        out_to[cnt] = ts_ex_v
        out_sd[cnt] = sd
        out_r[cnt] = raw_r - fee_r - fund_r
        out_fr[cnt] = fee_r + fund_r
        cnt += 1

        nxt = k_ex + cooldown_bars
        i = nxt if nxt > i + 1 else (i + 1)

    return out_bi[:cnt], out_ti[:cnt], out_to[:cnt], out_sd[:cnt], out_r[:cnt], out_fr[:cnt]
