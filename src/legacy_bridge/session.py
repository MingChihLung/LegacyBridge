# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Sessions: explicit server-minted handles (MCP 2026-07-28 has no protocol sessions).

A handle binds: the manifest, the tool process (launched or attached), the four
layer backends, the capability registry, the router and the SoM mark table.
Handles live only on the machine that owns the GUI.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from typing import Any

from .backends.base import Backend, ExecContext
from .backends.l0_native import NativeBackend
from .backends.l1_script import ScriptBackend
from .backends.l2_uia import UIABackend
from .backends.l3_vision import VisionBackend
from .backends.sim import SimBackend
from .manifest import Manifest
from .models import LAYERS, BackendError

_REAL = {"L0": NativeBackend, "L1": ScriptBackend, "L2": UIABackend, "L3": VisionBackend}


class Session:
    def __init__(self, manifest: Manifest, *, launch: bool = False, attach_pid: int | None = None,
                 sim: dict[str, Any] | None = None, handle: str | None = None) -> None:
        from .registry import CapabilityRegistry
        from .router import Router

        self.id = handle or "s_" + secrets.token_hex(4)
        self.manifest = manifest
        self.created_at = time.time()
        self.proc: subprocess.Popen[bytes] | None = None
        self.pid: int | None = attach_pid
        self.sim_cfg = sim if sim is not None else (json.loads(os.environ["LB_SIM"]) if os.environ.get("LB_SIM") else None)
        self.sim_world: dict[str, Any] = {"state": (self.sim_cfg or {}).get("state", "MainWindow"),
                                          "values": dict((self.sim_cfg or {}).get("values", {}))}
        self._marks: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()  # one GUI, one actor at a time
        if launch:
            self.launch()
        self.backends: dict[str, Backend] = {}
        for layer in LAYERS:
            if self.sim_cfg and layer in self.sim_cfg:
                self.backends[layer] = SimBackend(self, layer, self.sim_cfg[layer])
            else:
                self.backends[layer] = _REAL[layer](self)
        self.registry = CapabilityRegistry(self)
        self.router = Router(self)

    # ---- process -------------------------------------------------------------
    def launch(self) -> None:
        cmd = [str(c) for c in self.manifest.tool.get("launch") or []]
        if not cmd:
            raise BackendError("manifest has no tool.launch")
        kw: dict[str, Any] = {}
        if os.name == "nt":  # survive the CLI process that launched it
            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        self.proc = subprocess.Popen(cmd, cwd=os.path.dirname(cmd[0]) or None, **kw)
        self.pid = self.proc.pid
        time.sleep(float(self.manifest.tool.get("launch_wait_s", 2)))

    def gui_running(self) -> bool:
        if self.sim_cfg is not None:
            return self.sim_cfg.get("gui_running", True)
        if self.proc is not None:
            return self.proc.poll() is None
        if self.pid is None:
            self.pid = self.find_pid()
        return self.pid is not None

    def find_pid(self) -> int | None:
        """Attach by process image name (tool.process_name) on Windows."""
        name = self.manifest.tool.get("process_name")
        if not name or os.name != "nt":
            return None
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                                 capture_output=True, timeout=10).stdout.decode("oem" if os.name == "nt" else "utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in out.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) > 1 and cols[0].lower() == name.lower():
                return int(cols[1])
        return None

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    # ---- screen state ----------------------------------------------------------
    def handle_popups(self) -> list[str]:
        """Auto-dismiss manifest-declared popups (first-run prompts etc.) via L2."""
        rules = self.manifest.tool.get("popups") or []
        l2 = self.backends.get("L2")
        if not rules or l2 is None or isinstance(l2, SimBackend) or not hasattr(l2, "handle_popups"):
            return []
        if not self.gui_running():
            return []
        done: list[str] = []
        for _ in range(5):  # dismissing one dialog can reveal the next
            got = l2.handle_popups(rules)  # type: ignore[attr-defined]
            if not got:
                break
            done += got
            time.sleep(0.7)
        if done:
            from . import trace
            trace.record({"session": self.id, "action": "auto_dismiss", "layer": "L2", "ok": True, "popups": done})
            time.sleep(0.5)
        return done

    def detect_state(self) -> dict[str, Any]:
        """Ask layers (most reliable first) whether they recognise the current screen."""
        self.handle_popups()
        for layer in ("L2", "L0", "L3", "L1"):
            be = self.backends[layer]
            rules = {s: r[layer] for s, r in self.manifest.states.items() if layer in r}
            if not rules and not isinstance(be, SimBackend):
                continue
            if be.health.status == "unavailable":
                continue
            st = be.detect_state(rules)
            if st:
                return {"state": st, "via": layer}
        return {"state": "Unknown" if self.gui_running() else "NotRunning", "via": None}

    def wait_for(self, cond: dict[str, Any], ctx: ExecContext | None = None) -> dict[str, Any]:
        """Poll a condition: {state}, {log_contains}, {ocr_contains}, {observe, equals|contains}."""
        timeout = float(cond.get("timeout_s", 30))
        interval = float(cond.get("interval_s", 0.5))
        t0 = time.monotonic()
        while True:
            if ctx and ctx.cancel.is_set():
                raise BackendError("cancelled")
            if self._check(cond):
                return {"met": True, "waited_ms": int((time.monotonic() - t0) * 1000)}
            if time.monotonic() - t0 > timeout:
                raise BackendError(f"wait_for timeout after {timeout}s: {cond}")
            time.sleep(interval)

    def _check(self, cond: dict[str, Any]) -> bool:
        if "state" in cond:
            return self.detect_state()["state"] == cond["state"]
        if "log_contains" in cond:
            r = self.router.run("read_log", {"lines": 50}, verify=False) if "read_log" in self.manifest.actions else None
            return bool(r and r.ok and cond["log_contains"] in str(r.value))
        if "ocr_contains" in cond:
            vb = self.backends["L3"]
            try:
                img, _ = vb.grab()  # type: ignore[attr-defined]
                return cond["ocr_contains"].lower() in " ".join(b["text"] for b in vb.ocr(img)).lower()  # type: ignore[attr-defined]
            except (BackendError, AttributeError):
                return False
        if "observe" in cond:
            r = self.router.run(cond["observe"], cond.get("params", {}), verify=False)
            if not r.ok:
                return False
            if "equals" in cond:
                return str(r.value) == str(cond["equals"])
            return str(cond.get("contains", "")) in str(r.value)
        raise BackendError(f"unknown wait condition {cond}")

    # ---- SoM marks shared by L2 tree and L3 screenshot ---------------------------
    def set_marks(self, marks: list[dict[str, Any]], source: str) -> None:
        self._marks = {m["id"]: {**m, "source": source} for m in marks}

    def mark(self, mark_id: str) -> dict[str, Any]:
        if mark_id not in self._marks:
            raise BackendError(f"unknown mark {mark_id}; call get_ui_tree or screenshot(som=true) first")
        return self._marks[mark_id]

    def info(self) -> dict[str, Any]:
        return {"session": self.id, "tool": self.manifest.name, "pid": self.pid,
                "gui_running": self.gui_running(), "sim": bool(self.sim_cfg), **self.detect_state()}


class SessionManager:
    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self._s: dict[str, Session] = {}
        self._lock = threading.Lock()

    def open(self, **kw: Any) -> Session:
        s = Session(self.manifest, **kw)
        with self._lock:
            self._s[s.id] = s
        return s

    def get(self, handle: str) -> Session:
        try:
            return self._s[handle]
        except KeyError:
            raise BackendError(f"unknown session '{handle}'; call open_session first") from None

    def close(self, handle: str) -> None:
        with self._lock:
            s = self._s.pop(handle, None)
        if s:
            s.close()

    def all(self) -> list[Session]:
        return list(self._s.values())
