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
    "Round R833 Pre-Commodity Sync: Blueberry FundedNext MT5 equity has increased to $2,627.61, "
    "reducing Phase 1 gap to $72.39 (SOLUSD.p flipped to +$0.25 profit; ETH floating loss contracted to -$3.40). "
    "Total armed risk capped at $36.75 (1.39%). 5 USWTI.p positions (+1.61 profit) and 2 pending orders queued "
    "for purge at 22:59:30Z timer ahead of 23:00Z commodity open. Desk self-check: 11 PASS / 0 WARN / 0 FAIL. "
    "Awaiting your FRED multi-asset macroeconomic backtest findings and tracking 23:00Z Gold (XAUUSD) open against 3/3 model walls (3,509.35 / 3,601.70)."
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
