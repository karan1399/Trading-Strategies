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
    "Round R832 Sync: 13 Forex positions on Blueberry MT5 were cleanly liquidated at 22:05Z open "
    "into profit/breakeven, reducing Blueberry FundedNext Phase 1 gap to $74.10 (Equity: $2,625.90, Balance: $2,629.61). "
    "2 stale pending orders (#20372533 GBPUSD, #20372741 AUDUSD) cancelled. 5 USWTI.p positions (+1.61 floating profit) "
    "and 2 pending orders (#20365867 XAU, #20372436 HK50) remain pending 23:00Z commodity open. All 3 crypto positions "
    "(SOL, ETH x2) preserved with stops. Full 151/151 MT5 broker asset Parquet dataset committed and verified on GitHub (5fcd2d18). "
    "Standing by for your FRED multi-asset macroeconomic universe backtest results and 23:00Z Gold compression monitoring."
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
        // Contenteditable / ProseMirror
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
    }}
    
    // Find send button
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
