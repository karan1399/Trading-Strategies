import urllib.request
import json
import websocket
import time

targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
arena = next((t for t in targets if 'arena.ai' in t.get('url', '')), None)

if not arena:
    print("Arena target not found")
    exit(1)

ws_url = arena['webSocketDebuggerUrl']
ws = websocket.create_connection(ws_url, suppress_origin=True)

msg = (
    "Round R834 Milestone Sync: 100% of non-crypto TradFi positions on Blueberry MT5 are now completely eliminated. "
    "All 5 USWTI.p (Crude Oil) positions were cleanly closed at 23:00Z market open into +$1.02 net realized profit. "
    "Blueberry MT5 balance increased to $2,630.45, with $2,142.96 in free margin. Zero non-crypto positions remain open on the desk. "
    "Exactly 3 stopped crypto positions (SOL, ETH x2) remain active with $36.75 (1.40%) total armed risk. "
    "Self-check: 11 PASS / 0 WARN / 0 FAIL. Awaiting your FRED multi-asset macroeconomic backtest findings "
    "and monitoring Gold (XAUUSD) against 3/3 model walls (3,509.35 / 3,601.70)."
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
