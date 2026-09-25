# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Router: pick the most reliable runnable layer, verify, fall back, learn."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from . import trace
from .backends.base import ExecContext
from .manifest import expand
from .models import GUI_LAYERS, ActionSpec, BackendError, Result

if TYPE_CHECKING:
    from .session import Session


class ParamError(ValueError):
    pass


def validate_params(spec: ActionSpec, params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    unknown = set(params) - set(spec.params)
    if unknown:
        raise ParamError(f"{spec.name}: unknown params {sorted(unknown)}")
    for name, p in spec.params.items():
        if name not in params or params[name] is None:
            if p.required:
                raise ParamError(f"{spec.name}: missing param '{name}'")
            out[name] = p.default
            continue
        v = params[name]
        try:
            v = {"integer": int, "number": float, "boolean": bool}.get(p.type, str)(v)
        except (TypeError, ValueError) as e:
            raise ParamError(f"{spec.name}.{name}: expected {p.type}") from e
        if p.enum is not None and v not in p.enum:
            raise ParamError(f"{spec.name}.{name}: {v!r} not in {p.enum}")
        out[name] = v
    return out


class Router:
    def __init__(self, session: "Session") -> None:
        self.session = session

    def run(self, action: str, params: dict[str, Any] | None = None, *, force: str | None = None,
            verify: bool = True, confirmed: bool = False, ctx: ExecContext | None = None,
            check_state: bool = True) -> Result:
        reg = self.session.registry
        t0 = time.monotonic()
        try:
            spec = reg._spec(action)
            params = validate_params(spec, params or {})
        except (KeyError, ParamError) as e:
            return Result(ok=False, action=action, error=str(e).strip("'\""))
        ctx = ctx or ExecContext(session=self.session)

        if (spec.destructive or spec.confirm) and not confirmed:
            return Result(ok=False, action=action, error="confirmation required",
                          explain={"confirm": expand(spec.confirm or f"Run {action}?", params)})

        st = self.session.detect_state()["state"] if (check_state and spec.states) else None
        if force:
            layers = [(force, reg.support(spec, force))]
            if st is not None and st not in (spec.states or []) and (force in GUI_LAYERS or st != "NotRunning"):
                return Result(ok=False, action=action, state=st,
                              error=f"{force} requires state {spec.states}, current is {st}", explain=reg.explain(action))
        else:
            layers = reg.ranked_layers(spec, st)
        if not layers:
            err = (f"requires state {spec.states}, current is {st}" if st is not None and st not in (spec.states or [])
                   else "no runnable layer")
            return Result(ok=False, action=action, state=st, error=err, explain=reg.explain(action))

        chain: list[dict[str, Any]] = []
        with self.session.lock, trace.span(f"lb.{action}", session=self.session.id):
            for layer, probe in layers:
                be = self.session.backends[layer]
                baseline = self._baseline(spec, params, layer) if (verify and spec.verify) else None
                try:
                    with trace.span(f"lb.{action}.{layer}", layer=layer, support=probe.support.value):
                        value = be.execute(spec, spec.backends.get(layer, {}), params, ctx)
                    if spec.kind == "act":
                        reg.invalidate()  # the world changed: files may now exist, controls may differ
                    verified = self._verify(spec, params, layer, baseline) if (verify and spec.verify) else None
                    if verified is False:
                        raise BackendError("postcondition failed")
                except BackendError as e:
                    reg.record(action, layer, ok=False)
                    chain.append({"layer": layer, "error": str(e)})
                    trace.record({"session": self.session.id, "action": action, "layer": layer, "ok": False, "error": str(e)})
                    if ctx.cancel.is_set():
                        break
                    continue
                except Exception as e:  # unexpected: still fall back, but say so
                    reg.record(action, layer, ok=False)
                    chain.append({"layer": layer, "error": f"{type(e).__name__}: {e}"})
                    trace.record({"session": self.session.id, "action": action, "layer": layer, "ok": False, "error": repr(e)})
                    continue
                reg.record(action, layer, ok=True)
                ms = int((time.monotonic() - t0) * 1000)
                trace.record({"session": self.session.id, "action": action, "layer": layer, "ok": True,
                              "verified": verified, "ms": ms, "params": params})
                return Result(ok=True, action=action, backend=layer, value=value, verified=verified,
                              fallback_chain=chain, state=self.session.detect_state()["state"] if spec.kind == "act" else None,
                              elapsed_ms=ms)
        return Result(ok=False, action=action, error="all layers failed", fallback_chain=chain,
                      explain=reg.explain(action), elapsed_ms=int((time.monotonic() - t0) * 1000))

    @staticmethod
    def _checks(spec: ActionSpec) -> list[dict[str, Any]]:
        v = spec.verify
        return list(v) if isinstance(v, list) else ([v] if v else [])

    def _observe_for_verify(self, chk: dict[str, Any], params: dict[str, Any], layer: str) -> Result:
        reg = self.session.registry
        obs = reg._spec(chk["observe"])
        # Prefer reading back through the same layer (sees the same "world"), else best observer.
        force = layer if reg.support(obs, layer).support.runnable else None
        return self.run(obs.name, expand(chk.get("params", {}), params), force=force, verify=False, check_state=False)

    def _observable(self, chk: dict[str, Any]) -> bool:
        reg = self.session.registry
        return bool(reg.ranked_layers(reg._spec(chk["observe"])))

    def _baseline(self, spec: ActionSpec, params: dict[str, Any], layer: str) -> list[Any]:
        """For relative postconditions (increases_by) read observed values first."""
        out: list[Any] = []
        for chk in self._checks(spec):
            if "increases_by" in chk and self._observable(chk):
                r = self._observe_for_verify(chk, params, layer)
                out.append(r.value if r.ok else None)
            else:
                out.append(None)
        return out

    def _verify(self, spec: ActionSpec, params: dict[str, Any], layer: str, baseline: Any = None) -> bool:
        """All checks must pass. A check with `required: false` is skipped when no layer can
        observe it (e.g. the scripting pipe is down) - the result then says less, not more."""
        baseline = baseline or []
        ran = 0
        for i, chk in enumerate(self._checks(spec)):
            if not self._observable(chk):
                if chk.get("required", True):
                    return False
                continue
            r = self._observe_for_verify(chk, params, layer)
            if not r.ok:
                return False
            ran += 1
            if "increases_by" in chk:
                b = baseline[i] if i < len(baseline) else None
                if b is None or float(r.value) - float(b) < float(expand(chk["increases_by"], params)):
                    return False
            elif "at_least" in chk:
                if float(r.value) < float(expand(chk["at_least"], params)):
                    return False
            elif "equals" in chk:
                if str(r.value).strip() != str(expand(chk["equals"], params)).strip():
                    return False
            elif "contains" in chk:
                if str(expand(chk["contains"], params)) not in str(r.value):
                    return False
        return ran > 0
