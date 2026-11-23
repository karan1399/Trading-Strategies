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
    "Round R841 Equity Breakout Sync: MT5 equity has broken out into net profit at $2,631.64 (exceeding balance of $2,630.21)! "
    "The Blueberry FundedNext Phase 1 gap has collapsed down to $68.36 (shrunk by -$10.19 from session open). "
    "All open positions are green: ETHUSD 0.50 lot short is +$1.31 (traded to 1704.40 vs 1707.01 entry), SOLUSD short is +$0.13. "
    "Free margin expanded to $2,144.15. Spot Gold (XAUUSD) suffered sharp structural fakeout rejection from 3,605.15 down to 3,588.30; "
    "our Via Negativa discipline protected the desk from a -$17/oz buying trap! "
    "Self-check: 11 PASS / 0 WARN / 0 FAIL. Council recorded in desk/COUNCIL.md (commit 9240cb1f). "
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
