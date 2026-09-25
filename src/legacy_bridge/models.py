# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Core data model: action specs, support levels, backend health, results."""
from __future__ import annotations

import enum
import time
from dataclasses import asdict, dataclass, field
from typing import Any

LAYERS = ("L0", "L1", "L2", "L3")
GUI_LAYERS = ("L2", "L3")  # screen-state rules gate only these; L0/L1 work headless
LAYER_NAMES = {"L0": "native", "L1": "script", "L2": "uia", "L3": "vision"}


class Support(str, enum.Enum):
    """Per (action, layer) support level reported to the agent."""

    OK = "ok"
    DEGRADED = "degraded"            # works but has failed recently / low confidence
    NEEDS_RESTART = "needs_restart"  # e.g. INI write while GUI is running
    NEEDS_STATE = "needs_state"      # works, but not in the current screen state
    UNSUPPORTED = "unsupported"      # manifest declares no implementation for this layer
    PROBE_FAILED = "probe_failed"    # declared, but the probe says it can't work here
    NOT_PROBED = "not_probed"

    @property
    def runnable(self) -> bool:
        return self in (Support.OK, Support.DEGRADED, Support.NEEDS_RESTART)


@dataclass
class ParamSpec:
    name: str
    type: str = "string"             # string | integer | number | boolean | path
    description: str = ""
    enum: list[Any] | None = None
    default: Any = None
    required: bool = True

    def json_schema(self) -> dict[str, Any]:
        t = "string" if self.type == "path" else self.type
        s: dict[str, Any] = {"type": t}
        if self.description:
            s["description"] = self.description
        if self.enum is not None:
            s["enum"] = self.enum
        if self.default is not None:
            s["default"] = self.default
        if self.type == "path":
            s["format"] = "path"
        return s


@dataclass
class ActionSpec:
    name: str
    kind: str                        # "act" | "observe"
    description: str = ""
    params: dict[str, ParamSpec] = field(default_factory=dict)
    returns: dict[str, Any] | None = None
    states: list[str] | None = None  # None = any state
    reversible: bool = True
    idempotent: bool = False
    destructive: bool = False
    long_running: bool = False
    confirm: str | None = None
    verify: dict[str, Any] | None = None
    prefer: list[str] | None = None
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.kind == "observe"

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "params": {k: p.json_schema() for k, p in self.params.items()},
            "returns": self.returns,
            "states": self.states,
            "effects": {
                "reversible": self.reversible,
                "idempotent": self.idempotent,
                "destructive": self.destructive,
                "long_running": self.long_running,
            },
            "requires_confirm": bool(self.destructive or self.confirm),
            "declared_layers": sorted(self.backends),
        }


@dataclass
class BackendHealth:
    layer: str
    status: str                      # ok | partial | unavailable
    note: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    probed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["name"] = LAYER_NAMES[self.layer]
        return d


@dataclass
class Probe:
    """Result of probing one (action, layer)."""

    support: Support
    note: str = ""
    confidence: float = 1.0


@dataclass
class Result:
    ok: bool
    action: str
    backend: str | None = None
    value: Any = None
    verified: bool | None = None
    fallback_chain: list[dict[str, Any]] = field(default_factory=list)
    state: str | None = None
    error: str | None = None
    explain: dict[str, Any] | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != []}


class BackendError(Exception):
    """Raised by a backend implementation when an action fails at that layer."""


class NotAvailable(BackendError):
    """Raised when a layer cannot run on this machine at all (e.g. no pywinauto)."""
