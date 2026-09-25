# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""End-to-end MCP test on Windows against the real Audacity.

Spawns `legacy_bridge.mcp_server` over stdio (protocol 2026-07-28) and drives it the way
an agent would. One long-lived server process = one persistent mod-script-pipe connection.
Writes out/e2e_report.json and out/e2e_som.png.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import mcp_types as t
from mcp import Client, StdioServerParameters

from legacy_bridge.tasks_client import TasksClientExtension

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
report: dict = {"steps": []}


def log(name, value):
    report["steps"].append({"step": name, "value": value})
    v = json.dumps(value, ensure_ascii=False, default=str)
    print(f"[{name}] {v[:400]}", flush=True)


async def on_elicit(ctx, params):
    log("elicitation", params.message)
    return t.ElicitResult(action="accept", content={"confirm": True})


async def call(c, s, _tool, **args):
    t0 = time.time()
    r = await c.call_tool(_tool, {"session": s, **args})
    sc = r.structured_content
    log(_tool + (f"[{args.get('backend')}]" if args.get("backend") else ""),
        {"is_error": r.is_error, "ms": int((time.time() - t0) * 1000), "result": sc})
    return r


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "legacy_bridge.mcp_server", "--manifest", str(ROOT / "audacity.yaml")],
        env={**os.environ, "LB_TRACE": str(ROOT / "trace.jsonl"), "PYTHONUTF8": "1"},
    )
    async with Client(params, elicitation_callback=on_elicit, extensions=[TasksClientExtension()]) as c:
        report["protocol"] = c.protocol_version
        report["server_extensions"] = list((c.server_capabilities.extensions or {}).keys())
        report["tools"] = [x.name for x in (await c.list_tools()).tools]
        log("handshake", {"protocol": report["protocol"], "extensions": report["server_extensions"],
                          "n_tools": len(report["tools"])})
        s = (await c.call_tool("open_session", {})).structured_content["session"]
        caps = (await c.call_tool("get_capabilities", {"session": s})).structured_content
        log("capabilities", {"state": caps["state"],
                             "backends": {k: v["status"] for k, v in caps["backends"].items()},
                             "actions": {a["name"]: a["preferred"] for a in caps["actions"]}})

        await call(c, s, "get_version")
        await call(c, s, "get_preference", name="Locale/Language")
        await call(c, s, "remove_all_tracks")                       # MRTR confirmation
        await call(c, s, "generate_tone", frequency=880, duration=1.5)  # L1 pipe
        await call(c, s, "get_track_count")
        await call(c, s, "generate_tone", frequency=440, backend="L2")  # UIA: menu + dialog
        await call(c, s, "get_track_count")
        await call(c, s, "get_track_count", backend="L2")          # same fact, read via UIA
        await call(c, s, "undo", backend="L3")                     # vision layer: keyboard
        await call(c, s, "get_track_count")
        await call(c, s, "get_clips")
        await call(c, s, "select_all", backend="L2")
        await call(c, s, "export_wav", path=str(OUT / "mcp_tone.wav"))  # L1 + L0 verify
        await call(c, s, "explain", action="generate_tone")

        shot = await c.call_tool("screenshot", {"session": s, "som": True})
        img = next(x for x in shot.content if x.type == "image")
        (OUT / "e2e_som.png").write_bytes(base64.b64decode(img.data))
        log("screenshot", {"marks": len(shot.structured_content["marks"]),
                           "sample": shot.structured_content["marks"][:8]})
        tree = (await c.call_tool("get_ui_tree", {"session": s, "max_depth": 3})).structured_content
        log("ui_tree", {"elements": len(tree["elements"])})
        st = await c.read_resource(f"legacy://{s}/state")
        log("resource_state", json.loads(st.contents[0].text))
        tr = (await c.call_tool("get_trace", {"session": s, "limit": 30})).structured_content
        log("trace", tr)
    (OUT / "e2e_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("REPORT WRITTEN", flush=True)


asyncio.run(main())
