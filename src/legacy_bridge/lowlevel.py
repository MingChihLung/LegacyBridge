# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Generic low-level operations (for situations the manifest did not anticipate)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import trace
from .models import BackendError

if TYPE_CHECKING:
    from .session import Session


def _up(session: "Session", layer: str) -> bool:
    return session.backends[layer].health.status != "unavailable"


def get_ui_tree(session: "Session", max_depth: int = 4) -> dict[str, Any]:
    if not _up(session, "L2"):
        raise BackendError("UI tree needs L2 (UIA), which is unavailable: " + session.backends["L2"].health.note)
    with session.lock:
        return {"via": "L2", "elements": session.backends["L2"].ui_tree(max_depth)}


def screenshot(session: "Session", som: bool = True) -> tuple[bytes, dict[str, Any]]:
    if not _up(session, "L3"):
        raise BackendError("screenshot needs L3, which is unavailable: " + session.backends["L3"].health.note)
    with session.lock:
        png, marks = session.backends["L3"].screenshot(som=som)
    return png, {"via": "L3", "marks": marks, "hint": "click('#n') targets a mark" if som else None}


def _click_layer(session: "Session", target: str) -> str:
    if target.startswith(("auto_id:", "name:")):
        return "L2"
    if re.fullmatch(r"\d+,\d+", target) or target.startswith("text:"):
        return "L3"
    src = session.mark(target).get("source", "L3")
    return src


def click(session: "Session", target: str) -> dict[str, Any]:
    layer = _click_layer(session, target)
    if not _up(session, layer):
        raise BackendError(f"click via {layer} unavailable")
    with session.lock:
        session.backends[layer].click(target)
    trace.record({"session": session.id, "action": "click", "layer": layer, "target": target, "ok": True})
    return {"ok": True, "via": layer, "target": target, **session.detect_state()}


def _input_layer(session: "Session") -> str:
    for layer in ("L2", "L3"):
        if _up(session, layer):
            return layer
    raise BackendError("no input-capable layer (L2/L3) available")


def type_text(session: "Session", text: str) -> dict[str, Any]:
    layer = _input_layer(session)
    with session.lock:
        session.backends[layer].type_text(text)
    trace.record({"session": session.id, "action": "type", "layer": layer, "ok": True})
    return {"ok": True, "via": layer, "chars": len(text)}


def key(session: "Session", combo: str) -> dict[str, Any]:
    layer = _input_layer(session)
    with session.lock:
        session.backends[layer].key(combo)
    trace.record({"session": session.id, "action": "key", "layer": layer, "combo": combo, "ok": True})
    return {"ok": True, "via": layer, "combo": combo}
