# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Shared helpers for the Windows verification scripts."""
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


def server_params(manifest="audacity.yaml"):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "legacy_bridge.mcp_server", "--manifest", str(ROOT / manifest)],
        env={**os.environ, "LB_TRACE": str(ROOT / "trace.jsonl"), "PYTHONUTF8": "1", "LB_TASK_POLL_MS": "200"},
    )


class Recorder:
    def __init__(self, name):
        self.name, self.steps = name, []

    def log(self, step, value, ok=None):
        self.steps.append({"step": step, "ok": ok, "value": value})
        print(f"[{step}] ok={ok} {json.dumps(value, ensure_ascii=False, default=str)[:300]}", flush=True)

    async def call(self, c, s, _tool, **args):
        t0 = time.time()
        r = await c.call_tool(_tool, {"session": s, **args})
        sc = r.structured_content or {}
        ok = (not r.is_error) and sc.get("ok", True)
        label = _tool + (f"[{args['backend']}]" if args.get("backend") else "")
        self.log(label, {"ms": int((time.time() - t0) * 1000), "via": sc.get("backend"), "verified": sc.get("verified"),
                         "value": sc.get("value", sc), "error": sc.get("error"), "chain": sc.get("fallback_chain")}, ok)
        return r

    def save(self):
        (OUT / f"{self.name}.json").write_text(json.dumps(self.steps, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print("REPORT", self.name, sum(1 for s in self.steps if s["ok"] is True), "ok /",
              sum(1 for s in self.steps if s["ok"] is False), "failed", flush=True)


def elicit(accept=True, rec=None):
    async def cb(ctx, params):
        if rec:
            rec.log("elicitation", params.message)
        return t.ElicitResult(action="accept", content={"confirm": accept})
    return cb


def client(rec, accept=True, tasks=True, manifest="audacity.yaml"):
    ext = [TasksClientExtension()] if tasks else []
    c = Client(server_params(manifest), elicitation_callback=elicit(accept, rec), extensions=ext)
    c._lb_tasks_ext = ext[0] if ext else None
    return c
