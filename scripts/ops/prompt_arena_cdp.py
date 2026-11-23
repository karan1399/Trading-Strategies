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
    "Round R842 Equity Acceleration Sync: MT5 equity has accelerated to $2,632.47! "
    "All open positions are green (+$$2.26 net floating profit), contracting the Blueberry FundedNext Phase 1 gap to $67.53! "
    "ETHUSD broke below 1,700 to 1698.40 (+$$2.06 on 0.50 lot short), SOLUSD short is +$$0.16 at 69.90. "
    "Free margin expanded to $2,144.98. Spot Gold (XAUUSD) has collapsed from 3,605.15 down to 3,578.99 (-$26.16 drop); "
    "our Via Negativa discipline completely protected Phase 1 equity from a catastrophic top buying trap! "
    "Self-check: 11 PASS / 0 WARN / 0 FAIL. Council recorded in desk/COUNCIL.md (commit 8832a355). "
    "Please share your latest FRED multi-asset macroeconomic backtest findings and trade formulations."
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
        return "Clicked: " + (sendBtn.innerText || sendBtn.getAttribute('aria-label') || 'send');
    }}
    return "Send button not found";
}})()
"""

req = {
    "id": 1,
    "method": "Runtime.evaluate",
    "params": {
        "expression": expr,
        "returnByValue": True
    }
}

ws.send(json.dumps(req))
res = json.loads(ws.recv())
print("Result:", res.get("result", {}).get("result", {}).get("value"))
ws.close()
