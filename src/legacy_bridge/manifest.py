# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Manifest loading and placeholder expansion."""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import LAYERS, ActionSpec, ParamSpec

_ENV = re.compile(r"\$\{(\w+)(?::-((?:[^{}]|\{\w+\})*))?\}")
_VAR = re.compile(r"\{(\w+)\}")


def expand(value: Any, vars: dict[str, Any]) -> Any:
    """Recursively replace {name} with vars[name]; unknown names are left as-is.

    A string that is exactly "{name}" keeps the variable's type (e.g. int).
    """
    if isinstance(value, str):
        m = _VAR.fullmatch(value)
        if m and m.group(1) in vars:
            return vars[m.group(1)]
        return _VAR.sub(lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), value)
    if isinstance(value, list):
        return [expand(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, vars) for k, v in value.items()}
    return value


def _expand_env(s: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), s)


@dataclass
class Manifest:
    path: Path
    tool: dict[str, Any]
    layer_order: list[str]
    states: dict[str, dict[str, Any]]
    actions: dict[str, ActionSpec]
    vars: dict[str, Any] = field(default_factory=dict)
    digest: str = ""

    @property
    def name(self) -> str:
        return self.tool.get("name", self.path.stem)


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    p = Path(path).resolve()
    raw_text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(raw_text)

    vars: dict[str, Any] = {"manifest_dir": p.parent.as_posix(), "python": sys.executable}
    tool = dict(raw.get("tool") or {})
    # Resolve tool-level vars in order so later ones can reference earlier ones.
    for key in ("home", "exe"):
        if key in tool:
            val = expand(_expand_env(str(tool[key])), vars)
            val = Path(val).resolve().as_posix() if key in ("home", "exe") else val
            vars[key] = tool[key] = val
    tool = expand(tool, vars)

    order = raw.get("layer_order") or list(LAYERS)
    actions: dict[str, ActionSpec] = {}
    for name, a in (raw.get("actions") or {}).items():
        effects = a.get("effects") or {}
        params = {
            pn: ParamSpec(
                name=pn,
                type=pd.get("type", "string"),
                description=pd.get("description", ""),
                enum=pd.get("enum"),
                default=pd.get("default"),
                required="default" not in pd,
            )
            for pn, pd in (a.get("params") or {}).items()
        }
        backends = {layer: expand(cfg, vars) for layer, cfg in (a.get("backends") or {}).items()}
        bad = set(backends) - set(LAYERS)
        if bad:
            raise ValueError(f"action {name}: unknown layers {sorted(bad)}")
        actions[name] = ActionSpec(
            name=name,
            kind=a.get("kind", "act"),
            description=a.get("description", ""),
            params=params,
            returns=a.get("returns"),
            states=a.get("states"),
            reversible=effects.get("reversible", True),
            idempotent=effects.get("idempotent", False),
            destructive=effects.get("destructive", False),
            long_running=effects.get("long_running", False),
            confirm=a.get("confirm"),
            verify=a.get("verify"),
            prefer=a.get("prefer"),
            backends=backends,
        )
    return Manifest(
        path=p,
        tool=tool,
        layer_order=order,
        states={k: expand(v, vars) for k, v in (raw.get("states") or {}).items()},
        actions=actions,
        vars=vars,
        digest=hashlib.sha256(raw_text.encode()).hexdigest()[:12],
    )
