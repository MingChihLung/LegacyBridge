# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Keyboard-driven actions on L2/L3 checked against L1 ground truth, and state gating with a
dialog a 'human' left open."""
import asyncio
import subprocess
import sys

from common_mcp import ROOT, Recorder, client


async def val(c, s, name, **a):
    return (await c.call_tool(name, {"session": s, **a})).structured_content["value"]


async def main():
    rec = Recorder("input")
    async with client(rec) as c:
        s = (await c.call_tool("open_session", {})).structured_content["session"]
        await rec.call(c, s, "remove_all_tracks")
        await rec.call(c, s, "generate_tone", frequency=440, duration=2)
        for layer in ("L2", "L3"):
            await rec.call(c, s, "select_none")
            before = await val(c, s, "get_selection_length")
            r = await rec.call(c, s, "select_all", backend=layer)
            after = await val(c, s, "get_selection_length")
            rec.log(f"select_all[{layer}] ground truth", {"before": before, "after": after},
                    before == 0 and after > 1.5 and r.structured_content.get("verified") is True)
        for layer in ("L2", "L3"):
            await rec.call(c, s, "generate_tone", frequency=660)
            before = await val(c, s, "get_clip_count")
            await rec.call(c, s, "undo", backend=layer)
            after = await val(c, s, "get_clip_count")
            rec.log(f"undo[{layer}] ground truth", {"clips_before": before, "clips_after": after}, after == before - 1)

        # a 'human' opens a modal dialog outside the agent
        p = subprocess.run([sys.executable, str(ROOT / "open_dialog.py")], cwd=ROOT, capture_output=True, text=True)
        await asyncio.sleep(1.0)
        st = (await c.call_tool("get_state", {"session": s})).structured_content
        rec.log("state_with_modal", st, st.get("state") == "Dialog")
        now = (await c.call_tool("available_now", {"session": s})).structured_content
        rec.log("available_now_with_modal", now["actions"], "generate_tone" not in now["actions"])
        r = await rec.call(c, s, "generate_tone", frequency=100)
        rec.log("gated_refusal", r.structured_content.get("error"), r.is_error and "Dialog" in (r.structured_content.get("error") or ""))
        tree = (await c.call_tool("get_ui_tree", {"session": s, "max_depth": 2})).structured_content
        rec.log("ui_tree_sees_dialog", [e["name"] for e in tree["elements"]][:6], True)
        await rec.call(c, s, "click", target="name:Cancel")
        await asyncio.sleep(0.8)
        st = (await c.call_tool("get_state", {"session": s})).structured_content
        rec.log("state_after_cancel", st, st.get("state") == "MainWindow")
        r = await rec.call(c, s, "generate_tone", frequency=100)
        rec.log("works_again_after_cancel", r.structured_content.get("backend"), r.structured_content.get("ok"))
    rec.save()


asyncio.run(main())
