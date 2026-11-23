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
            const bodyText = document.body.innerText;
            const hasStop = Array.from(document.querySelectorAll('button')).some(b => b.innerText.includes('Stop') || b.getAttribute('aria-label') === 'Stop');
            const inputs = Array.from(document.querySelectorAll('textarea, input')).map(i => i.value);
            return {
                hasStop,
                inputs,
                bodyLength: bodyText.length,
                tail: bodyText.slice(-2000)
            };
        })()
        """
        cmd = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js_code,
                "returnByValue": True
            }
        }
        await ws.send(json.dumps(cmd))
        resp = await ws.recv()
        data = json.loads(resp)
        result = data.get("result", {}).get("result", {}).get("value", {})
        print("Has Stop Button (Actively generating?):", result.get("hasStop"))
        print("Inputs:", result.get("inputs"))
        print("=== TAIL OF BODY ===")
        print(result.get("tail"))

if __name__ == "__main__":
    asyncio.run(check())
