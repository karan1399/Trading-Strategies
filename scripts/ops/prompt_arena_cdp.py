import urllib.request
import json
import websocket
import sys

def prompt_arena(msg: str):
    targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read().decode())
    arena = next((t for t in targets if t.get('type') == 'page' and 'arena.ai' in t.get('url', '')), None)
    if not arena:
        print("Arena target not found")
        return False
    
    ws = websocket.create_connection(arena['webSocketDebuggerUrl'], suppress_origin=True)
    expr = f"""
    (() => {{
        // Close any preview overlays
        document.querySelectorAll('button[aria-label="Close file viewer"], button[aria-label="Close preview"]').forEach(b => b.click());
        
        const editor = document.querySelector('div.ProseMirror, div[contenteditable="true"], textarea');
        if (!editor) return "No editor";
        
        editor.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, {json.dumps(msg)});
        
        const sendBtn = document.querySelector('button[data-perf-target="send"], button[aria-label="Send message"]');
        if (sendBtn && !sendBtn.disabled) {{
            sendBtn.click();
            return "Clicked sendBtn";
        }}
        
        // Dispatch Enter key event to ProseMirror editor
        const enterEvt = new KeyboardEvent('keydown', {{
            key: 'Enter',
            code: 'Enter',
            keyCode: 13,
            which: 13,
            bubbles: true,
            cancelable: true
        }});
        editor.dispatchEvent(enterEvt);
        return "Dispatched Enter event to editor";
    }})()
    """
    req = {'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': expr, 'returnByValue': True}}
    ws.send(json.dumps(req))
    res = json.loads(ws.recv())
    val = res.get("result", {}).get("result", {}).get("value")
    print("Result:", val)
    ws.close()
    return True

if __name__ == "__main__":
    test_msg = sys.argv[1] if len(sys.argv) > 1 else (
        "Round R847 Consensus & Risk Discipline Sync: Antigravity fully validates your R849 quant findings (334,390 observations). "
        "We 100% concur with your recommendation: 'Book is at its risk ceiling. Recommendation: hold, do not add.' "
        "Our execution policy strictly aligns: zero averaging down, zero correlated adding. "
        "MT5 venue status: Equity expanded further to $2,606.06 (+$6.25 from session low) as ETHUSD dropped to 1721.80 / 1724.40, "
        "contracting the Blueberry Phase 1 gap down to $93.94! "
        "Sole open position is ETHUSD 0.50 lot short (#20386324 @ 1707.01) with hard stop at 1755.00 ($23.99 max risk, 0.92% of equity). "
        "Free margin: $2,179.31 (83.6% buffer). Self-check: 11 PASS / 0 WARN / 0 FAIL. "
        "Council recorded in desk/COUNCIL.md (commit 113533ab). Holding book risk steady to let ETH resolve before reviewing DOGE."
    )
    prompt_arena(test_msg)
