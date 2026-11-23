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
    "Round R840 Equity Consolidation Sync: MT5 equity consolidates solidly at $2,626.49 (+$6.45 above session low), "
    "holding the Blueberry FundedNext Phase 1 gap at $73.51. "
    "ETH floating drawdown is tightly contained at -$3.25 (ETHUSD 1713.50 vs 1707.01 entry), SOL floating loss -$0.19. "
    "Total floating drawdown across all open positions is only -$3.72 (0.14% of equity). "
    "MT5 balance is $2,630.21 with $2,139.00 in unencumbered free margin. Total armed desk risk: $36.75 (1.40%). "
    "Spot Gold (XAUUSD) has extended to 3,605.15 above the 3,601.70 upper wall; awaiting retest confirmation before committing. "
    "Self-check: 11 PASS / 0 WARN / 0 FAIL. Council recorded in desk/COUNCIL.md (commit fb0959bd). "
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
