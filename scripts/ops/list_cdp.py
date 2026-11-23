import urllib.request
import json

targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
for i, t in enumerate(targets):
    if t.get('type') == 'page':
        print(f"Index {i}: {t.get('title')} | {t.get('url')} | ID: {t.get('id')}")
