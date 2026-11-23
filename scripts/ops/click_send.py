import urllib.request
import json
import websocket

targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
arena = next((t for t in targets if t.get('type') == 'page' and 'arena.ai' in t.get('url', '')), None)

ws = websocket.create_connection(arena['webSocketDebuggerUrl'], suppress_origin=True)

expr = """
(() => {
    const sendBtn = document.querySelector('button[data-perf-target="send"], button[aria-label="Send message"], button[aria-label*="Send"], button[type="submit"]');
    if (sendBtn) {
        sendBtn.click();
        return "Clicked sendBtn successfully";
    }
    return "Send button not found";
})()
"""

ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': expr, 'returnByValue': True}}))
res = json.loads(ws.recv())
print(res.get('result', {}).get('result', {}).get('value'))
ws.close()
