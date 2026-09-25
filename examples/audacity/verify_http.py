# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Streamable HTTP transport on Windows: start lb-mcp --transport streamable-http, connect by URL."""
import asyncio
import subprocess
import sys
import time

from mcp import Client

from common_mcp import ROOT, Recorder
from legacy_bridge.tasks_client import TasksClientExtension


async def main():
    rec = Recorder("http")
    p = subprocess.Popen([sys.executable, "-m", "legacy_bridge.mcp_server", "--manifest", str(ROOT / "audacity.yaml"),
                          "--transport", "streamable-http", "--port", "8799"])
    try:
        time.sleep(4)
        async with Client("http://127.0.0.1:8799/mcp", extensions=[TasksClientExtension()]) as c:
            rec.log("protocol", c.protocol_version, c.protocol_version == "2026-07-28")
            s = (await c.call_tool("open_session", {})).structured_content["session"]
            await rec.call(c, s, "get_version")
            await rec.call(c, s, "get_track_count")
            await rec.call(c, s, "get_preference", name="Locale/Language")
    finally:
        p.terminate()
    rec.save()


asyncio.run(main())
