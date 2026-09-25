# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""N repetitions of generate_tone per layer (L1/L2/L3), each checked by the relative postcondition."""
import asyncio
import statistics
import sys

from common_mcp import Recorder, client

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
LAYERS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["L1", "L2", "L3"]


async def main():
    rec = Recorder("stability_" + "_".join(LAYERS))
    async with client(rec) as c:
        s = (await c.call_tool("open_session", {})).structured_content["session"]
        summary = {}
        for layer in LAYERS:
            await rec.call(c, s, "remove_all_tracks")
            oks, times = 0, []
            for i in range(N):
                r = await rec.call(c, s, "generate_tone", frequency=220 + 110 * i, backend=layer)
                sc = r.structured_content or {}
                if sc.get("ok") and sc.get("verified"):
                    oks += 1
                times.append(sc.get("elapsed_ms", 0))
            cnt = (await c.call_tool("get_track_count", {"session": s})).structured_content["value"]
            summary[layer] = {"ok": oks, "n": N, "tracks_after": cnt, "median_ms": statistics.median(times), "max_ms": max(times)}
        rec.log("summary", summary, all(v["ok"] == N and v["tracks_after"] == N for v in summary.values()))
    rec.save()


asyncio.run(main())
