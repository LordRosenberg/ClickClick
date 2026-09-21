"""Deterministic live Chrome probe; isolated synthetic page, never a scored task.

Run with PYTHONPATH=. after installing the local collector APK. Stores original
observations, action replies, and independent page click counters in --output.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlparse, parse_qs

from agent.executor import resolve_tap_index
from driver.accessibility import AccessibilityCollectorClient
from driver.android import AndroidDriver
from perception.filters import DetailedFilter
from perception.normalizer import normalize_a11y_tree
from shared.schemas import Action


HTML = b'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;font:18px sans-serif}button{width:70px;height:55px;border:1px solid black}
#palette{position:absolute;top:240px;left:35px}#decoy{position:absolute;top:240px;left:35px;display:none;background:purple}
[data-role=target]{background:tomato}[data-role=peer]{background:gold}#report{position:absolute;top:430px}</style></head>
<body><h2>Native node diagnostic</h2><p id="state">stable</p><button id="decoy"></button>
<div id="palette"><button data-role="target"></button><button data-role="peer"></button></div><p id="report">Clicks: none</p>
<script>
let last='stable';let count=0;
const target=document.querySelector('[data-role=target]'),peer=document.querySelector('[data-role=peer]');
function bind(el,name){el.onclick=()=>{count++;document.querySelector('#report').textContent='Clicks: '+name+' '+count;fetch('/click?name='+name)}}
bind(target,'target');bind(peer,'peer');bind(decoy,'decoy');
setInterval(async()=>{let s=await(await fetch('/state')).json();if(s.mode===last)return;last=s.mode;
document.querySelector('#state').textContent=last;
if(last==='move'){palette.style.top='320px';decoy.style.display='block'}
if(last==='remove'){target.remove();decoy.style.display='block'}
if(last==='relabel'){target.textContent='Different action'}
if(last==='replace'){let old=target;let replacement=old.cloneNode(true);old.replaceWith(replacement);bind(replacement,'replacement')}
if(last==='disable'){target.disabled=true}
},100);
</script></body></html>'''


async def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    shared = {"mode": "stable", "clicks": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/state":
                content = json.dumps({"mode": shared["mode"]}).encode()
                mime = "application/json"
            elif parsed.path == "/click":
                shared["clicks"].append(parse_qs(parsed.query)["name"][0])
                content, mime = b"ok", "text/plain"
            else:
                content, mime = HTML, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def adb(*argv):
        return await asyncio.to_thread(subprocess.check_output, [
            args.adb, "-s", args.serial, *argv,
        ], timeout=30)

    client = AccessibilityCollectorClient(args.serial)
    driver = AndroidDriver(serial=args.serial, collector_enabled=True)
    driver._collector = client
    results = []
    try:
        await adb("reverse", f"tcp:{args.port}", f"tcp:{args.port}")
        for mode in ["stable", "move", "remove", "relabel", "replace", "disable", "coordinate_control"]:
            shared.update(mode="stable", clicks=[])
            await adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d",
                      f"http://127.0.0.1:{args.port}/?case={mode}-{time.time_ns()}", "com.android.chrome")
            await asyncio.sleep(1.5)
            snapshot = await client.fetch_primary(timeout=4)
            raw = snapshot.to_raw_tree()
            ui = normalize_a11y_tree(raw, tree_filter=DetailedFilter())
            # Diagnostic fixture only: no text/label on the target color button.
            candidates = sorted([
                e for e in ui.elements if e.role == "android.widget.Button"
                and not e.resource_id and not e.text and not e.desc
            ], key=lambda e: e.bounds[0])
            if len(candidates) != 2:
                (output / f"{mode}-tree.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
                raise RuntimeError(f"Expected two anonymous diagnostic buttons, found {len(candidates)}")
            target = candidates[0]
            action = resolve_tap_index(Action(type="tap", index=target.index), ui, native_node_click=True)
            if not action._node_handle:
                raise RuntimeError("Target does not expose native click support")
            (output / f"{mode}-before.png").write_bytes(await adb("exec-out", "screencap", "-p"))
            shared["mode"] = "move" if mode == "coordinate_control" else mode
            await asyncio.sleep(0.7)
            # A fresh diagnostic capture proves geometry/identity changed; it is
            # deliberately NOT used to choose or rebind the original action.
            shifted = await client.fetch_primary(timeout=4)
            (output / f"{mode}-shifted.png").write_bytes(await adb("exec-out", "screencap", "-p"))
            if mode == "coordinate_control":
                left, top, right, bottom = target.bounds
                action = Action(type="tap_xy", x=(left + right)/2, y=(top + bottom)/2)
            start = time.perf_counter()
            result = await driver.act(action)
            latency = (time.perf_counter() - start) * 1000
            await asyncio.sleep(0.3)
            clicks = list(shared["clicks"])
            replay = None
            if mode == "stable":
                replay = await driver.act(action)
                await asyncio.sleep(0.2)
                assert shared["clicks"] == clicks, "Duplicate dispatch replayed a click"
            expected = ["target"] if mode in {"stable", "move"} else (["decoy"] if mode == "coordinate_control" else [])
            record = {
                "mode": mode, "passed": clicks == expected, "clicks": clicks, "expected": expected,
                "latency_ms": round(latency, 3), "target": target.model_dump(),
                "result": result.model_dump(), "replay": replay.model_dump() if replay else None,
                "before": raw, "shifted": shifted.to_raw_tree(),
            }
            (output / f"{mode}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            (output / f"{mode}-after.png").write_bytes(await adb("exec-out", "screencap", "-p"))
            results.append({k: record[k] for k in ["mode", "passed", "clicks", "expected", "latency_ms", "result", "replay"]})
            print(json.dumps(results[-1]), flush=True)
        (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        if not all(r["passed"] for r in results):
            raise RuntimeError("Live probe found an unexpected click")
    finally:
        await client.close()
        await adb("reverse", "--remove", f"tcp:{args.port}")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="emulator-5554")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
