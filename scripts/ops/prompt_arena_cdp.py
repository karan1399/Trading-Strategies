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
    "Round R843 Risk Enforcement Sync: BTC Sell Stop (#20390524) was triggered at 60,300 on liquidity wick down, "
    "then stopped out cleanly at hard broker stop 60,555.00 for -$12.84 loss (deal #18491932). "
    "Loss strictly respected the $25.00 ceiling and 1% risk limit. "
    "MT5 balance is $2,617.37, equity is $2,614.13 with $2,126.64 in unencumbered free margin. "
    "Total armed desk risk on MT5 drops to $26.57 (1.01% of equity) across 3 active positions (ETH 0.50 short, ETH 0.03 short, SOL 0.01 short). "
    "All positions have hard broker stops. Gap to Blueberry Phase 1 goal ($2,700): $85.87. "
    "Spot Gold (XAUUSD) has stabilized around 3,600 after its -$26.16 fakeout collapse. "
    "Self-check: 11 PASS / 0 WARN / 0 FAIL. Council recorded in desk/COUNCIL.md (commit e150adbe). "
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
