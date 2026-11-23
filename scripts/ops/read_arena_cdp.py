import urllib.request
import json
import websocket

# Find arena target
targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
arena = next((t for t in targets if t.get('type') == 'page' and 'arena.ai' in t.get('url', '')), None)

if not arena:
    print("Arena target not found")
    exit(1)

ws_url = arena['webSocketDebuggerUrl']
ws = websocket.create_connection(ws_url, suppress_origin=True)

# Evaluate to get the last assistant message
expr = """
(() => {
    // Get text of all paragraphs or articles
    const texts = Array.from(document.querySelectorAll('div[class*="message"], article, [data-testid*="message"], .markdown, p'))
        .map(el => el.innerText.trim())
        .filter(t => t.length > 50);
    return texts.slice(-5).join("\\n---CHUNK---\\n");
})()
"""

ws.send(json.dumps({
    'id': 1,
    'method': 'Runtime.evaluate',
    'params': {'expression': expr, 'returnByValue': True}
}))

res = json.loads(ws.recv())
val = res.get('result', {}).get('result', {}).get('value', 'No value')
print("=== ARENA LATEST MESSAGE ===")
print(val[-1500:])
ws.close()
