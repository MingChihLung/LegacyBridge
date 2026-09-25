# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Backend interface shared by the four layers."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..manifest import expand
from ..models import ActionSpec, BackendError, BackendHealth, Probe, Support

if TYPE_CHECKING:
    from ..session import Session


@dataclass
class ExecContext:
    """What a backend gets besides params: progress, mid-flight questions, cancel."""

    session: "Session"
    progress: Callable[[float, str], None] = lambda pct, msg: None
    # ask(message, json_schema) -> dict | None  (None = declined). Wired to MCP Tasks
    # `input_required` when running as a task, to elicitation/CLI prompt otherwise.
    ask: Callable[[str, dict[str, Any]], dict[str, Any] | None] = lambda msg, schema: None
    cancel: threading.Event = field(default_factory=threading.Event)


class Backend:
    """One layer (L0..L3). Subclasses map manifest `impl:` names to methods.

    An impl method is `impl_<name>(cfg, params, ctx) -> value` and raises
    BackendError on failure. Optional `probe_<name>(cfg) -> Probe` refines
    per-action availability; without it, layer health decides.
    """

    layer: str = "L?"

    def __init__(self, session: "Session") -> None:
        self.session = session
        self.manifest = session.manifest
        self._health: BackendHealth | None = None

    # ---- layer-wide -------------------------------------------------------
    def probe_health(self) -> BackendHealth:
        raise NotImplementedError

    @property
    def health(self) -> BackendHealth:
        if self._health is None:
            self._health = self.probe_health()
        return self._health

    def reprobe(self) -> BackendHealth:
        self._health = None
        return self.health

    # ---- per action -------------------------------------------------------
    def has_impl(self, impl: str) -> bool:
        return callable(getattr(self, f"impl_{impl}", None))

    def probe_action(self, spec: ActionSpec, cfg: dict[str, Any]) -> Probe:
        impl = cfg.get("impl", "")
        if not self.has_impl(impl):
            return Probe(Support.PROBE_FAILED, f"{self.layer} has no implementation named '{impl}'")
        if self.health.status == "unavailable":
            return Probe(Support.PROBE_FAILED, self.health.note)
        fn = getattr(self, f"probe_{impl}", None)
        if fn is not None:
            try:
                return fn(cfg)
            except Exception as e:  # probe must never crash the registry
                return Probe(Support.PROBE_FAILED, f"probe error: {e}")
        return Probe(Support.OK if self.health.status == "ok" else Support.DEGRADED, self.health.note)

    def execute(self, spec: ActionSpec, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        impl = cfg.get("impl", "")
        fn = getattr(self, f"impl_{impl}", None)
        if fn is None:
            raise BackendError(f"{self.layer}: no impl '{impl}'")
        return fn(expand(cfg, params), params, ctx)

    # ---- state detection ----------------------------------------------------
    def detect_state(self, rules: dict[str, dict[str, Any]]) -> str | None:
        """rules = {state_name: this layer's rule}. Return first match or None."""
        return None

    # ---- low-level (only L2/L3 implement) -----------------------------------
    def ui_tree(self, max_depth: int = 4) -> list[dict[str, Any]]:
        raise BackendError(f"{self.layer} does not provide a UI tree")

    def screenshot(self, som: bool = False) -> tuple[bytes, list[dict[str, Any]]]:
        raise BackendError(f"{self.layer} does not provide screenshots")

    def click(self, target: str) -> None:
        raise BackendError(f"{self.layer} cannot click")

    def type_text(self, text: str) -> None:
        raise BackendError(f"{self.layer} cannot type")

    def key(self, combo: str) -> None:
        raise BackendError(f"{self.layer} cannot send keys")


def cast(value: Any, how: str | None) -> Any:
    if how is None or value is None:
        return value
    return {"int": int, "float": float, "str": str, "bool": lambda v: str(v).lower() in ("1", "true", "yes")}[how](
        str(value).strip() if how != "str" else value
    )
