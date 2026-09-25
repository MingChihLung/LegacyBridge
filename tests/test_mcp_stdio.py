# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""End-to-end over stdio JSON-RPC with the real mcp 2.x client at protocol 2026-07-28."""
import copy
import json
import os
import sys

import mcp_types as t
from mcp import Client, StdioServerParameters

from legacy_bridge.tasks_client import TasksClientExtension

from conftest import SIM


def server_params(manifest_path, home, sim):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "legacy_bridge.mcp_server", "--manifest", manifest_path, "--sim", json.dumps(sim)],
        env={**os.environ, "LEGACY_FLASHER_HOME": str(home), "LB_TASK_POLL_MS": "50"},
    )


def elicit_answer(accept: bool):
    asked = []

    async def cb(ctx, params):
        asked.append(params.message)
        return t.ElicitResult(action="accept", content={"confirm": accept})

    return cb, asked


async def test_discovery_capabilities_and_actions(tool_home, manifest_path):
    cb, _ = elicit_answer(True)
    async with Client(server_params(manifest_path, tool_home, SIM), elicitation_callback=cb,
                      extensions=[TasksClientExtension()]) as c:
        assert c.protocol_version == "2026-07-28"
        assert "io.modelcontextprotocol/tasks" in (c.server_capabilities.extensions or {})
        tools = {tl.name: tl for tl in (await c.list_tools()).tools}
        assert tools["flash_firmware"].annotations.destructive_hint is True
        assert tools["get_baudrate"].annotations.read_only_hint is True
        assert tools["set_baudrate"].output_schema is not None

        sid = (await c.call_tool("open_session", {})).structured_content["session"]
        caps = (await c.call_tool("get_capabilities", {"session": sid})).structured_content
        assert caps["state"] == "MainWindow"
        r = (await c.call_tool("set_baudrate", {"session": sid, "value": 115200, "backend": "L1"})).structured_content
        assert r["ok"] and r["backend"] == "L1" and r["verified"]
        r = (await c.call_tool("get_baudrate", {"session": sid})).structured_content
        assert r["value"] == 115200 and r["backend"] == "L0"

        ex = (await c.call_tool("explain", {"session": sid, "action": "get_version"})).structured_content
        assert ex["fallback_chain"] == ["L1"]
        state = await c.read_resource(f"legacy://{sid}/state")
        assert json.loads(state.contents[0].text)["state"] == "MainWindow"
        shot = await c.call_tool("screenshot", {"session": sid, "som": True})
        assert shot.content[0].type == "image" and shot.structured_content["marks"]


async def test_destructive_mrtr_decline(tool_home, manifest_path):
    cb, asked = elicit_answer(False)
    async with Client(server_params(manifest_path, tool_home, SIM), elicitation_callback=cb) as c:
        sid = (await c.call_tool("open_session", {})).structured_content["session"]
        r = await c.call_tool("flash_firmware", {"session": sid, "path": str(tool_home / "fw.bin")})
        assert r.is_error and r.structured_content["error"] == "declined by user"
        assert asked and "fw.bin" in asked[0]


async def test_long_running_sync_without_tasks_ext(tool_home, manifest_path):
    cb, asked = elicit_answer(True)
    async with Client(server_params(manifest_path, tool_home, SIM), elicitation_callback=cb) as c:
        sid = (await c.call_tool("open_session", {})).structured_content["session"]
        progress = []

        async def on_progress(p, total, msg):
            progress.append(p)

        r = await c.call_tool("flash_firmware", {"session": sid, "path": str(tool_home / "fw.bin")},
                              progress_callback=on_progress)
        assert asked, "confirmation must be elicited via MRTR"
        assert r.structured_content["ok"] and r.structured_content["backend"] == "L1"
        assert r.structured_content["task_id"] is None
        assert progress and progress[-1] == 100.0


async def test_long_running_as_task_with_midflight_input(tool_home, manifest_path):
    sim = copy.deepcopy(SIM)
    sim["L2"]["ask"] = {"flash_firmware": "裝置韌體較新，要降版嗎？"}
    sim["L2"]["step_delay"] = 0.05
    cb, asked = elicit_answer(True)
    questions = []

    async def on_input(task_id, key, req):
        questions.append(req["params"]["message"])
        return {"action": "accept", "content": {"proceed": True}}

    ext = TasksClientExtension(on_input=on_input)
    async with Client(server_params(manifest_path, tool_home, sim), elicitation_callback=cb, extensions=[ext]) as c:
        sid = (await c.call_tool("open_session", {})).structured_content["session"]
        # force L2 (simulated) so the mid-flight question fires
        r = await c.call_tool("flash_firmware", {"session": sid, "path": str(tool_home / "fw.bin"), "backend": "L2"})
        assert asked, "MRTR confirmation happens before the task is created"
        assert questions == ["裝置韌體較新，要降版嗎？"]
        statuses = [s["status"] for s in ext.seen]
        assert "input_required" in statuses and statuses[-1] == "completed"
        assert r.structured_content["ok"] and r.structured_content["backend"] == "L2"


async def test_task_midflight_decline_fails_cleanly(tool_home, manifest_path):
    sim = copy.deepcopy(SIM)
    sim["L2"]["ask"] = {"flash_firmware": "降版？"}
    cb, _ = elicit_answer(True)
    ext = TasksClientExtension()  # default on_input declines
    async with Client(server_params(manifest_path, tool_home, sim), elicitation_callback=cb, extensions=[ext]) as c:
        sid = (await c.call_tool("open_session", {})).structured_content["session"]
        r = await c.call_tool("flash_firmware", {"session": sid, "path": str(tool_home / "fw.bin"), "backend": "L2"})
        assert r.is_error and not r.structured_content["ok"]
        assert "declined" in r.structured_content["fallback_chain"][0]["error"]
