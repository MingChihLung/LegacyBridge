# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""MCP Tasks extension (`io.modelcontextprotocol/tasks`, spec 2026-07-28).

Wire shapes follow modelcontextprotocol/ext-tasks schema/2026-07-28:
  tools/call  -> {resultType:"task", taskId, status, createdAt, lastUpdatedAt, ttlMs, pollIntervalMs}
  tasks/get   -> {resultType:"complete", ...Task, result? | error? | inputRequests?}
  tasks/update{taskId, inputResponses} -> {resultType:"complete"}
  tasks/cancel{taskId}                 -> {resultType:"complete"}

The Python SDK (mcp 2.2) ships the extension plumbing (Extension, MethodBinding,
tools/call interception) but not a Tasks implementation, so this module provides one.
Work runs in threads: GUI automation is blocking by nature.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import mcp_types as mt
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types import RequestParams

TASKS_EXT = "io.modelcontextprotocol/tasks"
TERMINAL = {"completed", "failed", "cancelled"}

# Set by the interceptor around tools/call; a tool body that wants to become a task
# stores a spawn request here instead of running synchronously.
task_slot: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("lb_task_slot", default=None)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class TaskRecord:
    task_id: str
    ttl_ms: int
    poll_ms: int
    status: str = "working"
    status_message: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    input_requests: dict[str, Any] = field(default_factory=dict)
    responses: dict[str, Any] = field(default_factory=dict)
    answered: threading.Condition = field(default_factory=threading.Condition)
    cancel: threading.Event = field(default_factory=threading.Event)
    expires: float = 0.0

    def wire(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "taskId": self.task_id, "status": self.status, "createdAt": self.created_at,
            "lastUpdatedAt": self.updated_at, "ttlMs": self.ttl_ms, "pollIntervalMs": self.poll_ms,
        }
        if self.status_message:
            d["statusMessage"] = self.status_message
        if self.status == "completed":
            d["result"] = self.result or {}
        elif self.status == "failed":
            d["error"] = self.error or {"code": -32603, "message": "failed"}
        elif self.status == "input_required":
            d["inputRequests"] = self.input_requests
        return d


class TaskStore:
    def __init__(self, ttl_ms: int = 3_600_000, poll_ms: int = 1000) -> None:
        self.ttl_ms, self.poll_ms = ttl_ms, poll_ms
        self._t: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def _touch(self, t: TaskRecord, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(t, k, v)
        t.updated_at = _now()

    def spawn(self, work: Callable[["TaskHandle"], dict[str, Any]]) -> TaskRecord:
        """Run work(handle) in a thread; its return value is the final CallToolResult dict."""
        t = TaskRecord(task_id="t_" + secrets.token_urlsafe(12), ttl_ms=self.ttl_ms, poll_ms=self.poll_ms)
        t.expires = time.time() + self.ttl_ms / 1000
        with self._lock:
            self._purge()
            self._t[t.task_id] = t
        handle = TaskHandle(self, t)

        def run() -> None:
            try:
                res = work(handle)
                if t.status not in TERMINAL:
                    self._touch(t, status="cancelled" if t.cancel.is_set() else "completed", result=res,
                                input_requests={})
            except Exception as e:  # noqa: BLE001 - becomes a JSON-RPC error on the task
                if t.status not in TERMINAL:
                    self._touch(t, status="cancelled" if t.cancel.is_set() else "failed",
                                error={"code": -32603, "message": str(e)}, input_requests={})

        threading.Thread(target=run, name=t.task_id, daemon=True).start()
        return t

    def get(self, task_id: str) -> TaskRecord:
        t = self._t.get(task_id)
        if t is None:
            raise MCPError(code=-32602, message=f"unknown task {task_id}")
        return t

    def update(self, task_id: str, responses: dict[str, Any]) -> None:
        t = self.get(task_id)
        with t.answered:
            for k, v in responses.items():
                if k in t.input_requests and k not in t.responses:  # ignore unknown / already-satisfied
                    t.responses[k] = v
            t.answered.notify_all()

    def cancel(self, task_id: str) -> None:
        t = self.get(task_id)
        t.cancel.set()
        with t.answered:
            t.answered.notify_all()

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, t in self._t.items() if t.expires < now]:
            del self._t[k]


class TaskHandle:
    """Given to the worker: progress, mid-flight questions, cancellation."""

    def __init__(self, store: TaskStore, rec: TaskRecord) -> None:
        self.store, self.rec = store, rec
        self._n = 0

    @property
    def cancel(self) -> threading.Event:
        return self.rec.cancel

    def progress(self, pct: float, msg: str) -> None:
        self.store._touch(self.rec, status_message=f"{pct:.0f}% {msg}")

    def ask(self, message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        """Move the task to input_required with an elicitation and block until tasks/update."""
        self._n += 1
        key = f"q{self._n}"
        req = mt.ElicitRequest(params=mt.ElicitRequestFormParams(message=message, requested_schema=schema))
        with self.rec.answered:
            self.rec.input_requests = {key: req.model_dump(by_alias=True, mode="json", exclude_none=True)}
            self.store._touch(self.rec, status="input_required", status_message=message)
            while key not in self.rec.responses and not self.rec.cancel.is_set():
                self.rec.answered.wait(timeout=1.0)
            resp = self.rec.responses.get(key)
            self.rec.input_requests = {}
            self.store._touch(self.rec, status="working")
        if not resp or resp.get("action") != "accept":
            return None
        return resp.get("content") or {}


# ---- extension ---------------------------------------------------------------
class TaskIdParams(RequestParams):
    taskId: str


class TaskUpdateParams(RequestParams):
    taskId: str
    inputResponses: dict[str, Any]


def client_supports_tasks(ctx: Any) -> bool:
    caps = getattr(getattr(ctx, "session", None), "client_capabilities", None)
    return bool(caps and caps.extensions and TASKS_EXT in caps.extensions)


class TasksExtension(Extension):
    identifier = TASKS_EXT

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def methods(self) -> list[MethodBinding]:
        async def get(ctx: Any, p: TaskIdParams) -> dict[str, Any]:
            return {"resultType": "complete", **self.store.get(p.taskId).wire()}

        async def update(ctx: Any, p: TaskUpdateParams) -> dict[str, Any]:
            self.store.update(p.taskId, p.inputResponses)
            return {"resultType": "complete"}

        async def cancel(ctx: Any, p: TaskIdParams) -> dict[str, Any]:
            self.store.cancel(p.taskId)
            return {"resultType": "complete"}

        return [MethodBinding("tasks/get", TaskIdParams, get),
                MethodBinding("tasks/update", TaskUpdateParams, update),
                MethodBinding("tasks/cancel", TaskIdParams, cancel)]

    async def intercept_tool_call(self, params: Any, ctx: Any, call_next: Any) -> Any:
        slot: dict[str, Any] = {"allowed": client_supports_tasks(ctx)}
        token = task_slot.set(slot)
        try:
            result = await call_next(ctx)
        finally:
            task_slot.reset(token)
        rec = slot.get("task")
        if rec is not None:  # the tool body chose to run as a task
            return {"resultType": "task", **rec.wire()}
        return result
