# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""JSON CLI over the same router/registry (for coding agents that prefer shell calls).

Every command prints one JSON document to stdout; progress goes to stderr as JSON lines.
Exit codes: 0 ok, 1 failed, 2 confirmation required, 3 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import lowlevel, trace
from .backends.base import ExecContext
from .manifest import load_manifest
from .models import BackendError
from .session import SessionManager


def _out(obj: Any, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    return code


def _kv(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"expected key=value, got {p!r}")
        k, v = p.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _ask_tty(message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    if not sys.stdin.isatty():
        print(json.dumps({"input_required": message, "answer": "declined (non-interactive)"}, ensure_ascii=False),
              file=sys.stderr)
        return None
    ans = input(f"{message} [y/N] ").strip().lower()
    key = next(iter(schema.get("properties", {"proceed": {}})))
    return {key: ans in ("y", "yes")}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lb", description="legacy-bridge JSON CLI")
    ap.add_argument("--manifest", default=os.environ.get("LB_MANIFEST"))
    ap.add_argument("--sim", help="JSON sim config (tests / non-Windows)")
    ap.add_argument("--pid", type=int, help="attach to this process id")
    ap.add_argument("--launch", action="store_true", help="launch the tool first")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("caps", help="capability matrix")
    c.add_argument("--now", action="store_true", help="only actions available in the current state")
    c.add_argument("--kind", choices=["act", "observe"])
    for name in ("describe", "explain"):
        sub.add_parser(name).add_argument("action")
    sub.add_parser("backends")
    p = sub.add_parser("probe")
    p.add_argument("--layers", help="comma list, e.g. L0,L2")
    sub.add_parser("state")
    r = sub.add_parser("run", help="run a semantic action: lb run set_baudrate value=115200")
    r.add_argument("action")
    r.add_argument("params", nargs="*", help="key=value (JSON values allowed)")
    r.add_argument("--backend", choices=["L0", "L1", "L2", "L3"])
    r.add_argument("--confirm", action="store_true", help="confirm destructive actions")
    w = sub.add_parser("wait")
    w.add_argument("--state")
    w.add_argument("--log-contains")
    w.add_argument("--timeout", type=float, default=30)
    u = sub.add_parser("ui", help="low-level: tree | shot | click | type | key")
    usub = u.add_subparsers(dest="ui_cmd", required=True)
    usub.add_parser("tree").add_argument("--depth", type=int, default=4)
    sh = usub.add_parser("shot")
    sh.add_argument("--no-som", action="store_true")
    sh.add_argument("--out", default="screenshot.png")
    usub.add_parser("click").add_argument("target")
    usub.add_parser("type").add_argument("text")
    usub.add_parser("key").add_argument("combo")
    t = sub.add_parser("trace")
    t.add_argument("--limit", type=int, default=50)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if not a.manifest:
        return _out({"error": "--manifest or LB_MANIFEST required"}, 3)
    s = SessionManager(load_manifest(a.manifest)).open(
        launch=a.launch, attach_pid=a.pid, sim=json.loads(a.sim) if a.sim else None)
    reg = s.registry
    try:
        if a.cmd == "caps":
            return _out(reg.available_now() if a.now else reg.get_capabilities(kind=a.kind))
        if a.cmd == "describe":
            return _out(reg.describe(a.action))
        if a.cmd == "explain":
            return _out(reg.explain(a.action))
        if a.cmd == "backends":
            reg.probe()
            return _out(reg.backends())
        if a.cmd == "probe":
            return _out(reg.probe(layers=a.layers.split(",") if a.layers else None))
        if a.cmd == "state":
            return _out(s.detect_state())
        if a.cmd == "run":
            ctx = ExecContext(
                session=s,
                progress=lambda pct, msg: print(json.dumps({"progress": pct, "msg": msg}, ensure_ascii=False),
                                                file=sys.stderr, flush=True),
                ask=_ask_tty)
            res = s.router.run(a.action, _kv(a.params), force=a.backend, confirmed=a.confirm, ctx=ctx)
            code = 0 if res.ok else (2 if res.error == "confirmation required" else 1)
            return _out(res.to_dict(), code)
        if a.cmd == "wait":
            cond = {k: v for k, v in {"state": a.state, "log_contains": a.log_contains}.items() if v}
            return _out(s.wait_for({**cond, "timeout_s": a.timeout}))
        if a.cmd == "ui":
            if a.ui_cmd == "tree":
                return _out(lowlevel.get_ui_tree(s, a.depth))
            if a.ui_cmd == "shot":
                png, meta = lowlevel.screenshot(s, som=not a.no_som)
                with open(a.out, "wb") as f:
                    f.write(png)
                return _out({"file": a.out, **meta})
            if a.ui_cmd == "click":
                return _out(lowlevel.click(s, a.target))
            if a.ui_cmd == "type":
                return _out(lowlevel.type_text(s, a.text))
            if a.ui_cmd == "key":
                return _out(lowlevel.key(s, a.combo))
        if a.cmd == "trace":
            path = os.environ.get("LB_TRACE")
            if path and os.path.exists(path):  # CLI runs are separate processes: read the JSONL trace
                with open(path, encoding="utf-8") as f:
                    return _out([json.loads(l) for l in f.readlines()[-a.limit:]])
            return _out(trace.recent(limit=a.limit))
    except (BackendError, KeyError) as e:
        return _out({"ok": False, "error": str(e).strip("'\"")}, 1)
    return _out({"error": "unknown command"}, 3)


if __name__ == "__main__":
    sys.exit(main())
