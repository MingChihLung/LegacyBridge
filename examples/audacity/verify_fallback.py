# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Run with mod-script-pipe DISABLED: agent calls actions without forcing a layer."""
import asyncio

from common_mcp import OUT, Recorder, client


async def main():
    rec = Recorder("fallback")
    async with client(rec) as c:
        s = (await c.call_tool("open_session", {})).structured_content["session"]
        caps = (await c.call_tool("get_capabilities", {"session": s})).structured_content
        acts = {a["name"]: {"pref": a["preferred"], "L1": a["support"]["L1"]} for a in caps["actions"]}
        rec.log("capabilities_without_pipe", acts, acts["generate_tone"]["pref"] == "L2")
        r = await rec.call(c, s, "generate_tone", frequency=500)
        rec.log("auto_routed_to", r.structured_content.get("backend"), r.structured_content.get("backend") == "L2")
        await rec.call(c, s, "get_track_count")
        await rec.call(c, s, "undo")
        r = await rec.call(c, s, "export_wav", path=str(OUT / "should_not_exist.wav"))
        rec.log("export_blocked_without_pipe", r.structured_content.get("error"), r.is_error)
        await rec.call(c, s, "explain", action="export_wav")
    rec.save()


asyncio.run(main())
