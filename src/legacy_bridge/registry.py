# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Capability registry: manifest (declared) x probes (measured) x runtime stats."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .models import GUI_LAYERS, LAYERS, ActionSpec, Probe, Support

if TYPE_CHECKING:
    from .session import Session

_RANK = {Support.OK: 0, Support.DEGRADED: 1, Support.NEEDS_RESTART: 2}
DEGRADE_AFTER = 2  # consecutive failures before an OK layer is reported degraded


class CapabilityRegistry:
    def __init__(self, session: "Session", ttl_s: float = 60.0) -> None:
        self.session = session
        self.manifest = session.manifest
        self.ttl_s = ttl_s
        self._probes: dict[tuple[str, str], Probe] = {}
        self._probed_at = 0.0
        self._version: str | None = None
        # runtime stats: (action, layer) -> counters
        self.stats: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"ok": 0, "fail": 0, "streak": 0})

    # ---- probing ---------------------------------------------------------------
    def probe(self, layers: list[str] | None = None, refresh: bool = True) -> dict[str, Any]:
        layers = layers or list(LAYERS)
        for layer in layers:
            be = self.session.backends[layer]
            if refresh:
                be.reprobe()
            for name, spec in self.manifest.actions.items():
                if layer in spec.backends:
                    self._probes[(name, layer)] = be.probe_action(spec, spec.backends[layer])
        self._probed_at = time.time()
        if refresh:
            self._version = None
        return {"probed": layers, "at": self._probed_at, "backends": self.backends()}

    def _ensure(self) -> None:
        if not self._probed_at or time.time() - self._probed_at > self.ttl_s:
            self.probe(refresh=bool(self._probed_at))

    def support(self, spec: ActionSpec, layer: str) -> Probe:
        if layer not in spec.backends:
            return Probe(Support.UNSUPPORTED, "not declared in manifest")
        self._ensure()
        p = self._probes.get((spec.name, layer), Probe(Support.NOT_PROBED))
        st = self.stats[(spec.name, layer)]
        if p.support == Support.OK and st["streak"] >= DEGRADE_AFTER:
            return Probe(Support.DEGRADED, f"failed {st['streak']}x in a row", p.confidence * 0.5)
        return p

    def ranked_layers(self, spec: ActionSpec, state: str | None = None) -> list[tuple[str, Probe]]:
        """Runnable layers, best first, filtered by the action's required `states`:
        state matches -> all layers; GUI not running -> headless L0/L1 only (safe, no
        screen to disturb); GUI running in another state (e.g. Flashing) -> none."""
        order = spec.prefer or self.manifest.layer_order
        order = list(order) + [l for l in LAYERS if l not in order]
        cands = [(l, self.support(spec, l)) for l in order]
        cands = [(l, p) for l, p in cands if p.support.runnable]
        if state is not None and spec.states and state not in spec.states:
            cands = [(l, p) for l, p in cands if l not in GUI_LAYERS] if state == "NotRunning" else []
        return sorted(cands, key=lambda lp: _RANK[lp[1].support])  # stable: keeps preference order

    def invalidate(self) -> None:
        """Re-run per-action probes (cheap) after the world changed; keeps layer health."""
        self.probe(refresh=False)

    def record(self, action: str, layer: str, ok: bool) -> None:
        st = self.stats[(action, layer)]
        if ok:
            st["ok"] += 1
            st["streak"] = 0
        else:
            st["fail"] += 1
            st["streak"] += 1

    # ---- queries (exposed via MCP + CLI) ---------------------------------------
    def tool_version(self) -> str | None:
        if self._version is None and "get_version" in self.manifest.actions:
            r = self.session.router.run("get_version", {}, verify=False)
            self._version = str(r.value) if r.ok else None
        return self._version

    def backends(self) -> dict[str, Any]:
        out = {}
        for layer, be in self.session.backends.items():
            h = be.health.to_dict()
            declared = [n for n, s in self.manifest.actions.items() if layer in s.backends]
            usable = [n for n in declared if (n, layer) in self._probes and self._probes[(n, layer)].support.runnable]
            h["coverage"] = round(len(usable) / len(declared), 2) if declared else None
            out[layer] = h
        return out

    def _action_entry(self, spec: ActionSpec, state: str) -> dict[str, Any]:
        support = {l: self.support(spec, l) for l in LAYERS}
        ranked = self.ranked_layers(spec, state)
        state_ok = spec.states is None or state in spec.states
        sup = {l: p.support.value for l, p in support.items()}
        if not state_ok:
            blocked = GUI_LAYERS if state == "NotRunning" else LAYERS
            sup.update({l: Support.NEEDS_STATE.value for l in blocked if support[l].support.runnable})
        entry: dict[str, Any] = {
            "name": spec.name,
            "kind": spec.kind,
            "available_now": bool(ranked),
            "support": sup,
            "preferred": ranked[0][0] if ranked else None,
            "confidence": round(ranked[0][1].confidence, 2) if ranked else 0.0,
        }
        if spec.destructive or spec.confirm:
            entry["requires_confirm"] = True
        if spec.long_running:
            entry["long_running"] = True
        if not ranked:
            entry["reason"] = (f"requires state {spec.states}, current is {state}"
                               if not state_ok else "no runnable layer")
        elif not state_ok:
            entry["note"] = f"state is {state}, not {spec.states}: only headless layers {[l for l, _ in ranked]}"
        return entry

    def get_capabilities(self, kind: str | None = None, only_available: bool = False) -> dict[str, Any]:
        self._ensure()
        st = self.session.detect_state()
        actions = [self._action_entry(s, st["state"]) for s in self.manifest.actions.values()
                   if kind is None or s.kind == kind]
        if only_available:
            actions = [a for a in actions if a["available_now"]]
        return {
            "tool": self.manifest.name,
            "version": self.tool_version(),
            "manifest": self.manifest.digest,
            "session": self.session.id,
            "state": st["state"],
            "state_via": st["via"],
            "backends": self.backends(),
            "actions": actions,
            "lowlevel": self.lowlevel_support(),
        }

    def available_now(self) -> dict[str, Any]:
        caps = self.get_capabilities(only_available=True)
        return {"state": caps["state"], "actions": [a["name"] for a in caps["actions"]], "lowlevel": caps["lowlevel"]}

    def describe(self, action: str) -> dict[str, Any]:
        spec = self._spec(action)
        d = spec.summary()
        d["layers"] = {
            l: {"support": self.support(spec, l).support.value, "note": self.support(spec, l).note,
                "impl": spec.backends.get(l, {}).get("impl"), "stats": dict(self.stats[(action, l)])}
            for l in LAYERS
        }
        d["verify"] = spec.verify
        return d

    def explain(self, action: str) -> dict[str, Any]:
        spec = self._spec(action)
        st = self.session.detect_state()["state"]
        entry = self._action_entry(spec, st)
        chain = [l for l, _ in self.ranked_layers(spec, st)]
        blocked = {l: {"support": p.support.value, "note": p.note}
                   for l in LAYERS if not (p := self.support(spec, l)).support.runnable}
        out = {"action": action, "available_now": entry["available_now"], "state": st,
               "fallback_chain": chain, "blocked_layers": blocked}
        if "reason" in entry:
            out["reason"] = entry["reason"]
        if entry.get("note"):
            out["note"] = entry["note"]
        if not chain:
            out["hint"] = "No layer can run this; use low-level tools (get_ui_tree / screenshot + click) or fix the probe notes."
        return out

    def lowlevel_support(self) -> dict[str, list[str]]:
        """Which layers can serve each generic low-level operation right now."""
        up = {l for l, be in self.session.backends.items() if be.health.status != "unavailable"}
        return {
            "get_ui_tree": [l for l in ("L2",) if l in up],
            "screenshot": [l for l in ("L3",) if l in up],
            "click": [l for l in ("L2", "L3") if l in up],
            "type": [l for l in ("L2", "L3") if l in up],
            "key": [l for l in ("L2", "L3") if l in up],
        }

    def _spec(self, action: str) -> ActionSpec:
        try:
            return self.manifest.actions[action]
        except KeyError:
            raise KeyError(f"unknown action '{action}'; see get_capabilities") from None
