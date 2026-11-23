import urllib.request
import json
import websocket

targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
arena = next((t for t in targets if t.get('type') == 'page' and 'arena.ai' in t.get('url', '')), None)

if not arena:
    print("Arena target not found")
    exit(1)

print("Target URL:", arena.get('url'))
ws_url = arena['webSocketDebuggerUrl']
ws = websocket.create_connection(ws_url, suppress_origin=True)

expr = """
(() => {
    return document.body.innerText;
})()
"""

ws.send(json.dumps({
    'id': 1,
    'method': 'Runtime.evaluate',
    'params': {'expression': expr, 'returnByValue': True}
}))

res = json.loads(ws.recv())
val = res.get('result', {}).get('result', {}).get('value', '')
print("=== ARENA FULL / LATEST TEXT ===")
print(val[-4000:])
ws.close()
