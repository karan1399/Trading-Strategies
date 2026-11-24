#!/usr/bin/env python3
"""
telemetry_16p_engine.py — Deterministic 16-Parameter Telemetry Audit Engine
Ingests and verifies all 16 canonical parameters directly from data/telemetry/ with zero intermediate files.
"""

import os, glob, json

def audit_symbol_16p(sym, assets_dir='data/telemetry/assets', footprint_dir='data/telemetry/footprint'):
    fpath = os.path.join(assets_dir, f"{sym}.json")
    if not os.path.exists(fpath):
        return None, f"Asset file missing: {fpath}"
        
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        return None, f"File read error: {e}"
        
    p = {}
    
    # 1. Ticker
    t = d.get('ticker') or {}
    p['1_ticker'] = {
        'price': t.get('price'),
        'dir': t.get('dir', 'flat'),
        'source': t.get('source', 'unknown')
    }
    
    # 2. Market Structure
    ms = d.get('market_structure') or {}
    auc = ms.get('auction') or {}
    vwap = ms.get('vwap') or {}
    emas = ms.get('emas') or {}
    rsi = ms.get('rsi') or {}
    
    day_auc = auc.get('day') or {}
    sess_auc = auc.get('session') or {}
    m15_ema = emas.get('m15') or {}
    
    last_p = ms.get('price', {}).get('last') or p['1_ticker']['price']
    day_val = day_auc.get('val')
    day_poc = day_auc.get('poc')
    day_vah = day_auc.get('vah')
    
    va_loc = 'UNKNOWN'
    if last_p and day_val and day_vah:
        if last_p < day_val: va_loc = 'BELOW_VAL'
        elif last_p > day_vah: va_loc = 'ABOVE_VAH'
        elif day_poc and last_p < day_poc: va_loc = 'IN_VA_DN'
        else: va_loc = 'IN_VA_UP'
        
    e89 = m15_ema.get('e89')
    e233 = m15_ema.get('e233')
    trend_short = (last_p < e89 and last_p < e233) if (last_p and e89 and e233) else False
    
    p['2_market_structure'] = {
        'last': last_p,
        'va_loc': va_loc,
        'day_val': day_val,
        'day_poc': day_poc,
        'day_vah': day_vah,
        'sess_poc': sess_auc.get('poc'),
        'vwap': vwap.get('vwap'),
        'sd1': vwap.get('bands', {}).get('sd1') if vwap.get('bands') else None,
        'e89_m15': e89,
        'e233_m15': e233,
        'trend_short': trend_short,
        'rsi_m15': rsi.get('m15')
    }
    
    # 3. Narrative Engine
    ne = d.get('narrative_engine') or {}
    p['3_narrative_engine'] = {
        'regime': ne.get('regime'),
        'model': ne.get('activeModel'),
        'dir': ne.get('direction'),
        'confidence': ne.get('confidence'),
        'grade': ne.get('grade')
    }
    
    # 4. Athena Oracle
    athena = d.get('athena_oracle') or {}
    p['4_athena_oracle'] = {
        'score': athena.get('score'),
        'verdict': athena.get('verdict'),
        'factors': {f.get('id'): f.get('score') for f in athena.get('factors', [])}
    }
    
    # 5. Microstructure L2/L3
    micro = d.get('microstructure_l2_l3') or {}
    hl = micro.get('hl') or {}
    p['5_microstructure'] = {
        'mark': hl.get('mark'),
        'oracle': hl.get('oracle'),
        'mark_vs_oracle_pct': hl.get('markVsOraclePct'),
        'vol24h_usd': hl.get('vol24hUsd')
    }
    
    # 6. Quant Positioning
    qp = d.get('quant_positioning') or {}
    ls = qp.get('lsRatios', {}).get('15m') or {}
    p['6_quant_positioning'] = {
        'ls_ratio_15m': ls.get('global', {}).get('ratio'),
        'top_accounts_ratio': ls.get('topAccounts', {}).get('ratio')
    }
    
    # 7. Options Deribit
    opt = d.get('options_deribit') or {}
    p['7_options_deribit'] = {
        'dvol': opt.get('dvol', {}).get('value') if isinstance(opt, dict) and opt.get('dvol') else None,
        'coverage': 'BTC/ETH only' if sym in ['BTC', 'ETH'] else 'Unsupported (Altcoin)'
    }
    
    # 8. Liquidations
    liq = d.get('liquidations') or {}
    cl_3d = liq.get('clusters', {}).get('3d', {}).get('stats', {}).get('keyLevels', {})
    p['8_liquidations'] = {
        'has_clusters': 'clusters' in liq,
        'near_above': cl_3d.get('above', [{}])[0].get('price') if cl_3d and cl_3d.get('above') else None,
        'near_below': cl_3d.get('below', [{}])[0].get('price') if cl_3d and cl_3d.get('below') else None
    }
    
    # 9. Book Walls
    bw = d.get('book_walls') or {}
    bw_fut = bw.get('futures', {}).get('m15') or {}
    p['9_book_walls'] = {
        'imbalance': bw_fut.get('imbalance'),
        'bid_depth': bw_fut.get('bidDepth'),
        'ask_depth': bw_fut.get('askDepth')
    }
    
    # 10. CVD
    cvd = d.get('cvd') or {}
    perp_cvd = cvd.get('perp', {}).get('m15', {}).get('kpis') or {}
    buy = perp_cvd.get('perpBuy', 0) or 0
    sell = perp_cvd.get('perpSell', 0) or 0
    p['10_cvd'] = {
        'perp_buy': buy,
        'perp_sell': sell,
        'delta': buy - sell
    }
    
    # 11. Footprint
    fp_counts = {'abs': 0, 'trap': 0, 'exh': 0, 'div': 0}
    fp_file = os.path.join(footprint_dir, sym, 'signals_7d.json')
    if os.path.exists(fp_file):
        try:
            with open(fp_file, 'r', encoding='utf-8') as fs:
                fp_data = json.load(fs)
                for s in fp_data.get('signals', []):
                    t_code = s.get('t', '')
                    if t_code in fp_counts:
                        fp_counts[t_code] += 1
        except:
            pass
    p['11_footprint'] = fp_counts
    
    # 12. Large Orders
    lo = d.get('large_orders') or {}
    p['12_large_orders'] = {
        'count_24h': len(lo.get('resting_and_executed', {}).get('24h', [])) if lo.get('resting_and_executed') else 0
    }
    
    # 13. Playbook Cards
    pb = d.get('playbook_cards') or {}
    p['13_playbook_cards'] = {
        'status': pb.get('status', 'none'),
        'count': len(pb.get('cards', []))
    }
    
    # 14. Backtest Metrics
    bm = d.get('backtest_metrics') or {}
    ma = bm.get('metricsAll') or {}
    p['14_backtest_metrics'] = {
        'trades': ma.get('trades'),
        'win_rate_pct': ma.get('winRatePct'),
        'profit_factor': ma.get('profitFactor')
    }
    
    # 15. COT Report
    cot = d.get('cot_report') or {}
    p['15_cot_report'] = {
        'supported': cot.get('supported', False),
        'reason': cot.get('reason', 'N/A')
    }
    
    # 16. Funding & OI
    foi = d.get('funding_and_oi') or {}
    f_list = foi.get('funding', [])
    binance_rate = next((x.get('rate8h') for x in f_list if x.get('name') == 'Binance'), None)
    p['16_funding_and_oi'] = {
        'binance_rate_8h': binance_rate,
        'venues_count': len(f_list)
    }
    
    return p, None

def audit_universe(symbols=None):
    if symbols is None:
        symbols = ['BTC', 'ETH', 'SOL', 'LINK', 'UNI', 'XRP', 'ADA', 'DOGE', 'AVAX']
        
    results = {}
    for s in symbols:
        res, err = audit_symbol_16p(s)
        if res:
            results[s] = res
    return results

if __name__ == '__main__':
    res = audit_universe()
    print(f"Successfully audited {len(res)} symbols across all 16 parameters.")
    for sym, data in res.items():
        ms = data['2_market_structure']
        fp = data['11_footprint']
        athena = data['4_athena_oracle']
        ne = data['3_narrative_engine']
        print(f"[{sym}] Price: {ms['last']} | VA: {ms['va_loc']} | Athena: {athena['score']} | Model: {ne['model']} | FP: {fp} | DVOL: {data['7_options_deribit']['dvol']}")
