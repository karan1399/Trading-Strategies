import urllib.request
import json

base = 'http://127.0.0.1:3000'

try:
    req = urllib.request.urlopen(base + '/api/cg/stopmap?sym=BTC&model=1&mode=coin&range=24h', timeout=8)
    sm = json.loads(req.read().decode())
    if sm.get('ok'):
        ov = sm.get('model', {}).get('overview', {})
        px = ov.get('lastPrice', 0)
        longs = ov.get('longs', 0) / 1e6
        shorts = ov.get('shorts', 0) / 1e6
        print('=== BTC POST-SQUEEZE STOPMAP ===')
        print(f'Last Price: {px:.2f} | Longs Open: ${longs:.1f}M | Shorts Open: ${shorts:.1f}M')
        
        kl = sm.get('model', {}).get('keyLevels', {})
        print('\nOverhead Short Liquidation Levels (Above Price):')
        for l in kl.get('above', [])[:5]:
            p = l.get('price', 0)
            u = l.get('usd', 0) / 1e6
            dist = ((p - px) / px) * 100 if px > 0 else 0
            print(f'  ${p:,.2f} ({dist:+.2f}%) -> ${u:.1f}M')
            
        print('\nUnderlying Long Liquidation Pools (Below Price - Potential Magnets):')
        for l in kl.get('below', [])[:5]:
            p = l.get('price', 0)
            u = l.get('usd', 0) / 1e6
            dist = ((p - px) / px) * 100 if px > 0 else 0
            print(f'  ${p:,.2f} ({dist:+.2f}%) -> ${u:.1f}M')
except Exception as e:
    print('Stopmap err:', e)

try:
    req = urllib.request.urlopen(base + '/api/cg/bookwalls?sym=BTC&market=futures&interval=m15&band=5', timeout=8)
    bw = json.loads(req.read().decode())
    if bw.get('ok'):
        imb = bw.get('imbalance', 0)
        b_dep = bw.get('bidDepth', 0) / 1e6
        a_dep = bw.get('askDepth', 0) / 1e6
        print(f'\n=== BTC ORDER-BOOK DEPTH (5% BAND) ===')
        print(f'Imbalance: {imb:+.2f}% | Bid Depth: ${b_dep:.1f}M | Ask Depth: ${a_dep:.1f}M')
except Exception as e:
    print('Bookwalls err:', e)
