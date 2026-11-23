import re

routes = ['liqlevels', 'stopmap', 'largeorder', 'bookwalls', 'cvd', 'fp', 'signals', 'ticker', 'screener', 'market', 'funding', 'oi']
for r in routes:
    path = f'src/app/api/cg/{r}/route.ts'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            code = f.read()
        params = re.findall(r"searchParams\.get\(['\"](.*?)['\"]\)", code)
        print(f"{r:12s}: params = {set(params)}")
    except Exception as e:
        print(f"{r:12s}: error {e}")
