# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Safety & protocol checks on the real app: MRTR decline, state gating with a modal open,
Tasks extension on a long-running action, resources, explain."""
import asyncio
import json

from common_mcp import OUT, Recorder, client


async def main():
    rec = Recorder("more")
    # 1) MRTR decline: destructive action must NOT run
    async with client(rec, accept=False) as c:
        s = (await c.call_tool("open_session", {})).structured_content["session"]
        before = (await c.call_tool("get_track_count", {"session": s})).structured_content["value"]
        r = await rec.call(c, s, "remove_all_tracks")
        after = (await c.call_tool("get_track_count", {"session": s})).structured_content["value"]
        rec.log("mrtr_decline_kept_tracks", {"before": before, "after": after},
                r.is_error and r.structured_content["error"] == "declined by user" and before == after)

    async with client(rec, accept=True) as c:
        s = (await c.call_tool("open_session", {})).structured_content["session"]
        await rec.call(c, s, "remove_all_tracks")
        await rec.call(c, s, "generate_tone", frequency=440, duration=3)

        # 2) state gating: open the Tone dialog with low-level keys, then try a semantic action
        await rec.call(c, s, "key", combo="%g")      # Alt+G -> Generate menu
        await asyncio.sleep(0.5)
        await rec.call(c, s, "key", combo="t")       # first item starting with T -> Tone...
        await asyncio.sleep(1.0)
        st = (await c.call_tool("get_state", {"session": s})).structured_content
        rec.log("state_with_modal", st, st.get("state") == "Dialog")
        now = (await c.call_tool("available_now", {"session": s})).structured_content
        rec.log("available_now_with_modal", now, "generate_tone" not in now["actions"])
        r = await rec.call(c, s, "select_all")
        rec.log("gated_refusal", r.structured_content.get("error"), r.is_error)
        await rec.call(c, s, "click", target="name:Cancel")
        await asyncio.sleep(0.5)
        st = (await c.call_tool("get_state", {"session": s})).structured_content
        rec.log("state_after_cancel", st, st.get("state") == "MainWindow")

        # 3) Tasks extension: long-running export returns a task, client polls to completion
        ext = c._lb_tasks_ext
        ext.seen.clear()
        r = await rec.call(c, s, "export_wav", path=str(OUT / "task_export.wav"))
        statuses = [x["status"] for x in ext.seen]
        rec.log("tasks_statuses", {"polls": len(statuses), "statuses": sorted(set(statuses)),
                                   "last": statuses[-1] if statuses else None,
                                   "status_messages": [x.get("statusMessage") for x in ext.seen][-3:]},
                bool(statuses) and statuses[-1] == "completed" and r.structured_content.get("ok"))

        # 4) resources + explain + trace
        for uri in (f"legacy://{s}/state", f"legacy://{s}/capabilities", "legacy://manifest"):
            res = await c.read_resource(uri)
            txt = res.contents[0].text
            rec.log(f"resource {uri.split('/')[-1] or uri}", {"bytes": len(txt), "head": txt[:120]}, len(txt) > 0)
        await rec.call(c, s, "explain", action="export_wav")
    rec.save()


asyncio.run(main())
