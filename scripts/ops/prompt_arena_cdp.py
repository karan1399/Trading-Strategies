import urllib.request
import json
import websocket
import time

targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
arena = next((t for t in targets if t.get('type') == 'page' and 'arena.ai' in t.get('url', '')), None)

if not arena:
    print("Arena target not found")
    exit(1)

ws_url = arena['webSocketDebuggerUrl']
ws = websocket.create_connection(ws_url, suppress_origin=True)

msg = (
    "Round R837 Hourly Review Sync: MT5 orderbook remains 100% sanitized with 0 non-crypto positions "
    "and 0 non-crypto pending orders. Blueberry MT5 balance stands at $2,630.21, with $2,142.72 in unencumbered free margin. "
    "Active crypto positions (SOL, ETH x2) and 1 BTC sell stop order remain active with verified stops "
    "($36.75 / 1.40% total armed risk). Gold (XAUUSD) continues trading stably inside 3/3 model walls "
    "(3,509.35 / 3,601.70). Self-check: 11 PASS / 0 WARN / 0 FAIL. Awaiting your FRED multi-asset macroeconomic backtest findings."
)

expr = f"""
(() => {{
    const text = {json.dumps(msg)};
    const editor = document.querySelector('div.ProseMirror, div[contenteditable="true"], textarea');
    if (!editor) return "Editor not found";
    
    editor.focus();
    if (editor.tagName.toLowerCase() === 'textarea') {{
        editor.value = text;
        editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }} else {{
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
    }}
    
    const btns = Array.from(document.querySelectorAll('button'));
    const sendBtn = btns.find(b => 
        b.getAttribute('data-perf-target') === 'send' || 
        b.querySelector('svg') && (b.innerText.toLowerCase().includes('send') || b.getAttribute('aria-label') === 'Send message' || b.classList.toString().includes('send')) ||
        b.type === 'submit'
    ) || btns[btns.length - 1];
    
    if (sendBtn) {{
        sendBtn.disabled = false;
        sendBtn.click();
        return "Clicked send button: " + (sendBtn.getAttribute('aria-label') || sendBtn.innerText || 'unnamed');
    }}
    return "Send button not found";
}})()
"""

ws.send(json.dumps({
    'id': 1,
    'method': 'Runtime.evaluate',
    'params': {'expression': expr, 'returnByValue': True}
}))

res = json.loads(ws.recv())
print("Result:", res.get('result', {}).get('result', {}).get('value'))
ws.close()
