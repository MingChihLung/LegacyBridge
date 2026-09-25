# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Action trace: JSONL file + OpenTelemetry spans when available.

MCP 2026-07-28 deprecates protocol Logging; the spec documents W3C trace context
in `_meta` (traceparent/tracestate). The MCP SDK already records server spans,
so spans started here nest under the tools/call span automatically.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from typing import Any, Iterator

try:
    from opentelemetry import trace as _otel

    _tracer = _otel.get_tracer("legacy_bridge")
except ImportError:  # pragma: no cover
    _tracer = None

_lock = threading.Lock()
_recent: list[dict[str, Any]] = []
MAX_RECENT = 200


def record(event: dict[str, Any]) -> None:
    event = {"ts": round(time.time(), 3), **event}
    with _lock:
        _recent.append(event)
        del _recent[:-MAX_RECENT]
        path = os.environ.get("LB_TRACE")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    if os.environ.get("LB_DEBUG"):
        print(json.dumps(event, ensure_ascii=False, default=str), file=sys.stderr)  # stderr: stdio-safe


def recent(session: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        items = [e for e in _recent if session is None or e.get("session") == session]
    return items[-limit:]


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[None]:
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(f"lb.{k}", v if isinstance(v, (str, int, float, bool)) else str(v))
        yield
