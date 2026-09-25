# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""MCP front-end (protocol 2026-07-28, Python SDK `mcp` 2.x / `MCPServer`).

Mapping of the design onto the 2026-07-28 spec:
  * no protocol sessions      -> `open_session` mints a handle passed to every tool
  * static tools/list         -> one tool per manifest action + fixed query/low-level tools,
                                 deterministic order, CacheHint(ttlMs, cacheScope)
  * capability discovery      -> get_capabilities / available_now / describe / explain / probe
  * destructive confirmation  -> MRTR: Resolve(...)+Elicit -> InputRequiredResult round trip
  * long-running actions      -> Tasks extension (tasks/get, tasks/update, tasks/cancel)
  * change notification       -> subscriptions/listen resource updates for legacy://{session}/state
  * logging deprecated        -> stderr + OpenTelemetry (see trace.py)
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from typing import Annotated, Any, Literal

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server import CacheHint, MCPServer
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__, lowlevel, trace
from .backends.base import ExecContext
from .manifest import expand, load_manifest
from .models import ActionSpec, BackendError, Result
from .session import SessionManager
from .tasks import TaskStore, TasksExtension, task_slot

Layer = Literal["L0", "L1", "L2", "L3"]
_PY = {"string": str, "path": str, "integer": int, "number": float, "boolean": bool}


class ActionResult(BaseModel):
    ok: bool
    action: str
    backend: str | None = Field(None, description="Layer that executed the action (L0..L3)")
    value: Any = None
    verified: bool | None = Field(None, description="Postcondition check result, if the action declares one")
    fallback_chain: list[dict[str, Any]] = Field(default_factory=list, description="Layers tried before success")
    state: str | None = Field(None, description="Screen state after the action")
    error: str | None = None
    explain: dict[str, Any] | None = None
    elapsed_ms: int = 0
    task_id: str | None = None


class Confirm(BaseModel):
    confirm: bool = Field(description="確認執行 / confirm")


class SessionInfo(BaseModel):
    session: str
    tool: str
    pid: int | None = None
    gui_running: bool
    sim: bool
    state: str
    via: str | None = None


INSTRUCTIONS = """\
Bridge to the legacy tool '{tool}'. Workflow:
1. open_session -> keep the returned `session` handle and pass it to every tool.
2. get_capabilities (or available_now) to see which actions work right now and via which layer
   (L0 native files/DLL, L1 CLI/COM, L2 UI Automation, L3 vision). Prefer semantic action tools.
3. Use explain(action) when something is unavailable or failed; it lists blocked layers and why.
4. Only if no semantic action fits: get_ui_tree / screenshot(som=true) then click('#n'), type_text, key.
Destructive actions ask for confirmation; long-running ones may return a task (poll tasks/get).
"""


def _result(r: Result, isError: bool | None = None) -> CallToolResult:
    data = ActionResult(**{k: v for k, v in r.to_dict().items() if k in ActionResult.model_fields}).model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))],
        structured_content=data,
        is_error=(not r.ok) if isError is None else isError,
    )


def build_server(manifest_path: str, sim: dict[str, Any] | None = None) -> MCPServer:
    manifest = load_manifest(manifest_path)
    sessions = SessionManager(manifest)
    store = TaskStore(poll_ms=int(os.environ.get("LB_TASK_POLL_MS", "1000")))
    mcp = MCPServer(
        name="legacy-bridge",
        title=f"{manifest.name} bridge",
        version=__version__,
        instructions=INSTRUCTIONS.format(tool=manifest.name),
        extensions=[TasksExtension(store)],
        cache_hints={
            "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
            "resources/templates/list": CacheHint(ttl_ms=300_000, scope="public"),
            "resources/list": CacheHint(ttl_ms=300_000, scope="public"),
            "resources/read": CacheHint(ttl_ms=0, scope="private"),  # state must never be served stale
        },
    )
    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    def S(handle: str):  # type: ignore[no-untyped-def]
        return sessions.get(handle)

    async def notify_state(ctx: Context, handle: str) -> None:
        try:
            await ctx.notify_resource_updated(f"legacy://{handle}/state")
        except Exception:
            pass

    # ------------------------------------------------------------------ sessions
    @mcp.tool(description="Launch or attach to the tool and return a session handle for all other tools.")
    def open_session(launch: bool = False, attach_pid: int | None = None) -> SessionInfo:
        s = sessions.open(launch=launch, attach_pid=attach_pid, sim=sim)
        s.registry.probe()
        return SessionInfo(**s.info())

    @mcp.tool(description="Close a session (terminates the tool if this session launched it).")
    def close_session(session: str) -> dict[str, Any]:
        sessions.close(session)
        return {"closed": session}

    # ------------------------------------------------------------------ capability queries
    @mcp.tool(description="Full capability matrix: per action and per layer support, preferred layer, "
                          "backend health/coverage, current screen state, low-level ops support.",
              annotations=ro)
    def get_capabilities(session: str, kind: Literal["act", "observe"] | None = None,
                         only_available: bool = False) -> dict[str, Any]:
        return S(session).registry.get_capabilities(kind=kind, only_available=only_available)

    @mcp.tool(description="Actions runnable in the current screen state.", annotations=ro)
    def available_now(session: str) -> dict[str, Any]:
        return S(session).registry.available_now()

    @mcp.tool(description="Parameters, effects, verification and per-layer detail for one action.", annotations=ro)
    def describe(session: str, action: str) -> dict[str, Any]:
        return S(session).registry.describe(action)

    @mcp.tool(description="Why an action is (un)available, which layers are blocked and the fallback order.",
              annotations=ro)
    def explain(session: str, action: str) -> dict[str, Any]:
        return S(session).registry.explain(action)

    @mcp.tool(description="Health of the four layers (L0 native, L1 script, L2 UIA, L3 vision).", annotations=ro)
    def get_backends(session: str) -> dict[str, Any]:
        return S(session).registry.backends()

    @mcp.tool(description="Re-probe layers (after tool update, window resize, reinstall...).",
              annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def probe(session: str, layers: list[Layer] | None = None, refresh: bool = True) -> dict[str, Any]:
        return S(session).registry.probe(layers=list(layers) if layers else None, refresh=refresh)

    # ------------------------------------------------------------------ low-level
    @mcp.tool(description="Current screen state (from manifest state rules).", annotations=ro)
    def get_state(session: str) -> dict[str, Any]:
        return S(session).detect_state()

    @mcp.tool(description="UI Automation element tree (L2). Elements get ids '#n' usable with click.",
              annotations=ro)
    def get_ui_tree(session: str, max_depth: int = 4) -> dict[str, Any]:
        return lowlevel.get_ui_tree(S(session), max_depth)

    @mcp.tool(description="Screenshot of the tool window (L3). som=true draws numbered marks; "
                          "then click('#n'). Returns the image plus the mark table.", annotations=ro)
    def screenshot(session: str, som: bool = True) -> CallToolResult:
        png, meta = lowlevel.screenshot(S(session), som)
        return CallToolResult(
            content=[Image(data=png, format="png").to_image_content(),
                     TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))],
            structured_content=meta,
        )

    @mcp.tool(description="Click a target: '#n' (mark from get_ui_tree/screenshot), 'auto_id:X', 'name:X', "
                          "'text:X' (OCR), or 'x,y' window-relative pixels.",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def click(session: str, target: str, ctx: Context) -> dict[str, Any]:
        r = await anyio.to_thread.run_sync(lowlevel.click, S(session), target)
        await notify_state(ctx, session)
        return r

    @mcp.tool(description="Type text into the focused control (L2, else L3). Non-ASCII is pasted.",
              annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    async def type_text(session: str, text: str) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(lowlevel.type_text, S(session), text)

    @mcp.tool(description="Send a key combo, e.g. 'enter', 'ctrl+s', 'alt+f4' (L2 uses pywinauto syntax).",
              annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    async def key(session: str, combo: str) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(lowlevel.key, S(session), combo)

    @mcp.tool(description="Wait until a condition holds instead of sleeping: state, log_contains, "
                          "ocr_contains, or observe+equals/contains.", annotations=ro)
    async def wait_for(session: str, state: str | None = None, log_contains: str | None = None,
                       ocr_contains: str | None = None, observe: str | None = None,
                       equals: str | None = None, contains: str | None = None,
                       timeout_s: float = 30) -> dict[str, Any]:
        cond: dict[str, Any] = {k: v for k, v in dict(state=state, log_contains=log_contains,
                                ocr_contains=ocr_contains, observe=observe, equals=equals,
                                contains=contains).items() if v is not None}
        cond["timeout_s"] = timeout_s
        try:
            return await anyio.to_thread.run_sync(S(session).wait_for, cond)
        except BackendError as e:
            return {"met": False, "error": str(e)}

    @mcp.tool(description="Recent action trace for this session (layers used, failures, timing).",
              annotations=ro)
    def get_trace(session: str, limit: int = 50) -> list[dict[str, Any]]:
        return trace.recent(session, limit)

    # ------------------------------------------------------------------ semantic actions
    for spec in manifest.actions.values():
        mcp.add_tool(_make_action_tool(spec, S, store, notify_state), name=spec.name,
                     title=spec.name.replace("_", " "),
                     description=_describe(spec),
                     annotations=ToolAnnotations(
                         readOnlyHint=spec.read_only,
                         destructiveHint=spec.destructive if not spec.read_only else None,
                         idempotentHint=spec.idempotent if not spec.read_only else None,
                         openWorldHint=False),
                     meta={"legacy_bridge/layers": sorted(spec.backends),
                           "legacy_bridge/long_running": spec.long_running})

    # ------------------------------------------------------------------ resources
    @mcp.resource("legacy://manifest", name="manifest", mime_type="application/yaml",
                  description="Declared actions and per-layer implementations")
    def manifest_res() -> str:
        return manifest.path.read_text(encoding="utf-8")

    @mcp.resource("legacy://{session}/capabilities", name="capabilities", mime_type="application/json")
    def caps_res(session: str) -> str:
        return json.dumps(S(session).registry.get_capabilities(), ensure_ascii=False, default=str)

    @mcp.resource("legacy://{session}/state", name="state", mime_type="application/json",
                  description="Current screen state; subscribe via subscriptions/listen")
    def state_res(session: str) -> str:
        return json.dumps(S(session).detect_state())

    @mcp.resource("legacy://{session}/trace", name="trace", mime_type="application/json")
    def trace_res(session: str) -> str:
        return json.dumps(trace.recent(session), ensure_ascii=False, default=str)

    mcp._lb_sessions = sessions  # type: ignore[attr-defined]  # for tests
    mcp._lb_tasks = store  # type: ignore[attr-defined]
    return mcp


def _describe(spec: ActionSpec) -> str:
    bits = [spec.description or spec.name, f"[{spec.kind}; layers: {', '.join(sorted(spec.backends))}]"]
    if spec.destructive:
        bits.append("DESTRUCTIVE: asks for confirmation.")
    if spec.long_running:
        bits.append("Long-running: returns a task to clients that support the Tasks extension.")
    if spec.states:
        bits.append(f"Needs state {spec.states}.")
    bits.append("Optional `backend` forces a layer.")
    return " ".join(bits)


def _make_action_tool(spec: ActionSpec, S, store: TaskStore, notify_state):  # type: ignore[no-untyped-def]
    """Build a tool function whose signature is generated from the manifest params."""
    needs_confirm = bool(spec.destructive or spec.confirm)
    KW = inspect.Parameter.KEYWORD_ONLY
    params = [inspect.Parameter("session", KW, annotation=Annotated[str, Field(description="handle from open_session")])]
    for p in spec.params.values():
        t: Any = _PY.get(p.type, str)
        if p.enum is not None:
            t = Literal[tuple(p.enum)]  # type: ignore[valid-type]
        ann = Annotated[t, Field(description=p.description or p.name)]
        params.append(inspect.Parameter(p.name, KW, annotation=ann,
                                        default=inspect.Parameter.empty if p.required else p.default))
    params.append(inspect.Parameter("backend", KW, default=None, annotation=Annotated[
        Layer | None, Field(description="Force a specific layer (debugging/evaluation)")]))

    if needs_confirm:
        # MRTR resolver: runs before the body; asks the client once, answer rides requestState.
        def ask_confirm(**kw: Any) -> Elicit[Confirm]:
            args = {k: kw.get(k) for k in spec.params}
            return Elicit(expand(spec.confirm or f"Run {spec.name}?", args), Confirm)

        ask_confirm.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [inspect.Parameter(n, KW) for n in spec.params])
        ask_confirm.__annotations__ = {"return": Elicit[Confirm]}
        params.append(inspect.Parameter("confirmation", KW, annotation=Annotated[
            ElicitationResult[Confirm], Resolve(ask_confirm)]))
    params.append(inspect.Parameter("ctx", KW, annotation=Context))

    async def tool(**kw: Any) -> ActionResult:
        ctx: Context = kw.pop("ctx")
        handle = kw.pop("session")
        force = kw.pop("backend", None)
        confirmation = kw.pop("confirmation", None)
        s = S(handle)
        confirmed = False
        if needs_confirm:
            if not (isinstance(confirmation, AcceptedElicitation) and confirmation.data.confirm):
                return _result(Result(ok=False, action=spec.name, error="declined by user"))
            confirmed = True
        args = {k: v for k, v in kw.items() if k in spec.params}

        slot = task_slot.get()
        if spec.long_running and slot is not None and slot.get("allowed"):
            def work(h):  # type: ignore[no-untyped-def]
                ectx = ExecContext(session=s, progress=h.progress, ask=h.ask, cancel=h.cancel)
                r = s.router.run(spec.name, args, force=force, confirmed=confirmed, ctx=ectx)
                return _result(r).model_dump(by_alias=True, mode="json", exclude_none=True)

            rec = store.spawn(work)
            slot["task"] = rec
            return ActionResult(ok=True, action=spec.name, task_id=rec.task_id, state="working")

        # synchronous path: progress notifications from the worker thread
        def progress(pct: float, msg: str) -> None:
            try:
                anyio.from_thread.run(ctx.report_progress, pct, 100.0, msg)
            except Exception:
                pass

        def ask(message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
            # Stateless MRTR cannot pause mid-execution; without Tasks we decline safely.
            return None

        ectx = ExecContext(session=s, progress=progress, ask=ask)
        r = await anyio.to_thread.run_sync(
            lambda: s.router.run(spec.name, args, force=force, confirmed=confirmed, ctx=ectx))
        if spec.kind == "act":
            await notify_state(ctx, handle)
        return _result(r)

    ret = ActionResult
    tool.__signature__ = inspect.Signature(params, return_annotation=ret)  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": ret}
    tool.__name__ = spec.name
    return tool


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="lb-mcp", description="legacy-bridge MCP server (protocol 2026-07-28)")
    ap.add_argument("--manifest", default=os.environ.get("LB_MANIFEST"), required=not os.environ.get("LB_MANIFEST"))
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sim", help="JSON sim config replacing layers (tests / non-Windows)")
    a = ap.parse_args(argv)
    server = build_server(a.manifest, sim=json.loads(a.sim) if a.sim else None)
    if a.transport == "stdio":
        server.run("stdio")
    else:
        print(f"legacy-bridge MCP on http://{a.host}:{a.port}/mcp", file=sys.stderr)
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
