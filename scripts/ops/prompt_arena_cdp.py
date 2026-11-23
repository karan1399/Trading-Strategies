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
    "Round R844 Squeeze Absorption Sync: Asian short squeeze triggered mechanical hard stops on ETH 0.03 lots at 1740.00 (-$1.08) "
    "and SOL 0.01 lots at 71.85 (-$1.53), capping realized loss strictly to -$2.61. "
    "Single open position remaining on MT5: ETHUSD 0.50 lots short (#20386324 @ 1707.01), trading at 1735.50 with hard stop at 1755.00. "
    "Max risk strictly capped at $23.99 (0.92% of equity, adhering to $25.00 ceiling). "
    "MT5 balance is $2,614.76, equity is $2,599.81 with $2,173.06 in unencumbered free margin (83.6% buffer). "
    "Gap to Blueberry Phase 1 goal ($2,700): $100.19. Self-check: 11 PASS / 0 WARN / 0 FAIL. "
    "Council recorded in desk/COUNCIL.md (commit 0ebd7642). "
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
