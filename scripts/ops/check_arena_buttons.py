import asyncio
import websockets
import json
import urllib.request

async def check():
    res = urllib.request.urlopen("http://localhost:9222/json")
    tabs = json.loads(res.read())
    arena_tab = next(t for t in tabs if "arena.ai" in t.get("url", ""))
    ws_url = arena_tab["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url) as ws:
        js_code = """
        (() => {
            const btns = Array.from(document.querySelectorAll('button')).map(b => ({
                text: b.innerText,
                aria: b.getAttribute('aria-label'),
                disabled: b.disabled,
                tag: b.tagName
            }));
            const editable = Array.from(document.querySelectorAll('[contenteditable="true"]')).map(e => e.innerText);
            return { btns: btns.slice(-15), editable };
        })()
        """
        cmd = {"id": 1, "method": "Runtime.evaluate", "params": {"expression": js_code, "returnByValue": True}}
        await ws.send(json.dumps(cmd))
        resp = await ws.recv()
        data = json.loads(resp)["result"]["result"]["value"]
        print("Buttons:", json.dumps(data.get("btns"), indent=2))
        print("Editable:", data.get("editable"))

if __name__ == "__main__":
    asyncio.run(check())
