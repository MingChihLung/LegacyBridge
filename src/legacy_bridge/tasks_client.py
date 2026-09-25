# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Client-side helper for the Tasks extension (mcp 2.x Client).

Usage:
    async with Client(params, extensions=[TasksClientExtension(on_input=answer)]) as c:
        result = await c.call_tool("flash_firmware", {...})   # polls tasks/get transparently

`on_input(task_id, key, elicit_request_dict) -> ElicitResult-like dict` answers
`input_required` rounds via tasks/update.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Literal

import mcp_types as t
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from pydantic import ConfigDict, TypeAdapter

from .tasks import TASKS_EXT

OnInput = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
OnStatus = Callable[[dict[str, Any]], Awaitable[None]]


class CreateTaskResult(t.Result):
    model_config = ConfigDict(extra="allow")
    result_type: Literal["task"] = "task"
    task_id: str
    status: str
    poll_interval_ms: int | None = None


class _P(t.RequestParams):
    task_id: str


class _PU(t.RequestParams):
    task_id: str
    input_responses: dict[str, Any]


class TasksGet(t.Request[_P, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    name_param = "taskId"


class TasksUpdate(t.Request[_PU, Literal["tasks/update"]]):
    method: Literal["tasks/update"] = "tasks/update"
    name_param = "taskId"


class TasksCancel(t.Request[_P, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    name_param = "taskId"


_DICT = TypeAdapter(dict[str, Any])


async def _decline(task_id: str, key: str, req: dict[str, Any]) -> dict[str, Any]:
    return {"action": "decline"}


class TasksClientExtension(ClientExtension):
    identifier = TASKS_EXT

    def __init__(self, on_input: OnInput | None = None, on_status: OnStatus | None = None,
                 timeout_s: float = 3600) -> None:
        self.on_input = on_input or _decline
        self.on_status = on_status
        self.timeout_s = timeout_s
        self.seen: list[dict[str, Any]] = []

    def claims(self) -> list[ResultClaim[Any]]:
        return [ResultClaim(result_type="task", model=CreateTaskResult, resolve=self._resolve)]

    async def _resolve(self, created: CreateTaskResult, ctx: ClaimContext) -> t.CallToolResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        interval = (created.poll_interval_ms or 1000) / 1000
        answered: set[str] = set()
        while True:
            task = await ctx.session.send_request(TasksGet(params=_P(task_id=created.task_id)), _DICT)
            self.seen.append(task)
            if self.on_status:
                await self.on_status(task)
            st = task["status"]
            if st == "completed":
                return t.CallToolResult.model_validate(task["result"])
            if st == "failed":
                err = task.get("error", {})
                return t.CallToolResult(content=[t.TextContent(type="text", text=f"task failed: {err.get('message')}")],
                                        is_error=True)
            if st == "cancelled":
                return t.CallToolResult(content=[t.TextContent(type="text", text="task cancelled")], is_error=True)
            if st == "input_required":
                responses = {}
                for key, req in (task.get("inputRequests") or {}).items():
                    if key not in answered:
                        responses[key] = await self.on_input(created.task_id, key, req)
                        answered.add(key)
                if responses:
                    await ctx.session.send_request(
                        TasksUpdate(params=_PU(task_id=created.task_id, input_responses=responses)), _DICT)
                    continue
            if loop.time() > deadline:
                await ctx.session.send_request(TasksCancel(params=_P(task_id=created.task_id)), _DICT)
                raise TimeoutError(f"task {created.task_id} timed out")
            await asyncio.sleep(interval)
