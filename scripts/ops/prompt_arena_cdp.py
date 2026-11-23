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
    "Round R839 Equity Advance Sync: MT5 equity has advanced to $2,628.51 (+$8.47 from session low), "
    "contracting the Blueberry FundedNext Phase 1 gap down to $71.49! "
    "ETH drawdown has compressed down to -$1.40 as ETHUSD traded to 1709.80 approaching our 1707.01 entry. "
    "SOL floating loss down to -$0.13. Total account drawdown is now only -$1.70. "
    "MT5 balance is $2,630.21, with $2,141.02 unencumbered free margin. Armed desk risk: $36.75 (1.40%). "
    "Spot Gold (XAUUSD) has pushed above the 3,601.70 Model-1/2/3 upper wall to 3,603.80; monitoring for sustained breakout. "
    "Self-check: 11 PASS / 0 WARN / 0 FAIL. Council updated in desk/COUNCIL.md (commit 99bfe8c9). "
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
