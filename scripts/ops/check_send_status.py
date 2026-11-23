import urllib.request
import json
import websocket

targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
arena = next((t for t in targets if t.get('type') == 'page' and 'arena.ai' in t.get('url', '')), None)

ws = websocket.create_connection(arena['webSocketDebuggerUrl'], suppress_origin=True)

expr = """
(() => {
    const sendBtn = document.querySelector('button[data-perf-target="send"], button[aria-label="Send message"], button[aria-label*="Send"], button[type="submit"]');
    const isGenerating = !!document.querySelector('button[aria-label*="Stop"], button[data-perf-target="stop"], .animate-spin');
    const editor = document.querySelector('div.ProseMirror, textarea');
    return {
        sendBtnFound: !!sendBtn,
        sendBtnDisabled: sendBtn ? sendBtn.disabled : null,
        isGenerating: isGenerating,
        editorText: editor ? editor.innerText : null
    };
})()
"""

ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': expr, 'returnByValue': True}}))
res = json.loads(ws.recv())
print(res.get('result', {}).get('result', {}).get('value'))
ws.close()
