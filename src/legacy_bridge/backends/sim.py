# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Simulated layer for tests and non-Windows development.

Replaces L2/L3 (or any layer) with an in-memory "virtual GUI" so the router,
capability registry, MCP server and CLI can be exercised anywhere.

Config (dict, or JSON in env LB_SIM), per layer:
  {"L2": {"available": true, "fail": ["set_baudrate"], "probe": {"read_log": "probe_failed"},
          "ask": {"flash_firmware": "Device firmware is newer. Downgrade?"}, "step_delay": 0.05},
   "L3": {...}, "state": "MainWindow"}
"""
from __future__ import annotations

import time
from typing import Any

from ..manifest import expand
from ..models import ActionSpec, BackendError, BackendHealth, Probe, Support
from .base import Backend, ExecContext


class SimBackend(Backend):
    def __init__(self, session, layer: str, cfg: dict[str, Any]):  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.layer = layer
        self.cfg = cfg
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def world(self) -> dict[str, Any]:
        return self.session.sim_world

    def probe_health(self) -> BackendHealth:
        if not self.cfg.get("available", True):
            return BackendHealth(self.layer, "unavailable", "sim: layer disabled")
        return BackendHealth(self.layer, "ok", "simulated", {"sim": True, "ocr": True})

    def has_impl(self, impl: str) -> bool:
        return True

    def probe_action(self, spec: ActionSpec, cfg: dict[str, Any]) -> Probe:
        if self.health.status == "unavailable":
            return Probe(Support.PROBE_FAILED, self.health.note)
        forced = (self.cfg.get("probe") or {}).get(spec.name)
        if forced:
            return Probe(Support(forced), "sim: forced")
        return Probe(Support.OK, "simulated")

    def execute(self, spec: ActionSpec, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        self.calls.append((spec.name, params))
        if spec.name in (self.cfg.get("fail") or []):
            raise BackendError(f"sim: injected failure on {self.layer}")
        values = self.world.setdefault("values", {})
        if spec.kind == "observe":
            if spec.name not in values:
                raise BackendError(f"sim: no value for {spec.name}")
            return values[spec.name]
        question = (self.cfg.get("ask") or {}).get(spec.name)
        delay = float(self.cfg.get("step_delay", 0.0))
        if spec.long_running:
            self.world["state"] = "Flashing"
        try:
            for i, pct in enumerate((25, 50, 75, 100)):
                if ctx.cancel.is_set():
                    raise BackendError("cancelled")
                if question and i == 1:
                    answer = ctx.ask(question, {"type": "object", "properties": {"proceed": {"type": "boolean"}},
                                                "required": ["proceed"]})
                    if not answer or not answer.get("proceed"):
                        raise BackendError("sim: operator declined mid-flight question")
                time.sleep(delay)
                ctx.progress(pct, f"sim {spec.name} {pct}%")
        finally:
            self.world["state"] = "MainWindow"
        # make the declared postcondition true
        checks = spec.verify if isinstance(spec.verify, list) else ([spec.verify] if spec.verify else [])
        for v in checks:
            if "equals" in v:
                values[v["observe"]] = expand(v["equals"], params)
            if "contains" in v:
                values[v["observe"]] = str(values.get(v["observe"], "")) + "\n" + v["contains"]
        return {"sim": self.layer, "action": spec.name}

    def detect_state(self, rules: dict[str, dict[str, Any]]) -> str | None:
        return self.world.get("state")

    # low-level surface
    def ui_tree(self, max_depth: int = 4) -> list[dict[str, Any]]:
        tree = [
            {"id": "#1", "depth": 0, "type": "Window", "name": "LegacyFlasher 3.2.1", "auto_id": "", "enabled": True, "rect": [0, 0, 640, 420]},
            {"id": "#2", "depth": 1, "type": "ComboBox", "name": "Baud rate", "auto_id": "cmbBaud", "enabled": True, "rect": [120, 10, 400, 34]},
            {"id": "#3", "depth": 1, "type": "Edit", "name": "Firmware", "auto_id": "txtPath", "enabled": True, "rect": [120, 44, 400, 68]},
            {"id": "#4", "depth": 1, "type": "Button", "name": "Flash", "auto_id": "btnFlash", "enabled": True, "rect": [520, 78, 620, 102]},
        ]
        self.session.set_marks(tree, source=self.layer)
        return tree

    def screenshot(self, som: bool = False) -> tuple[bytes, list[dict[str, Any]]]:
        # 1x1 transparent PNG
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        marks = self.ui_tree()[1:] if som else []
        return png, marks

    def click(self, target: str) -> None:
        self.calls.append(("click", {"target": target}))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", {"text": text}))

    def key(self, combo: str) -> None:
        self.calls.append(("key", {"combo": combo}))
