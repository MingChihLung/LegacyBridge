# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""L2 - Windows UI Automation via pywinauto (backend="uia").

Everything here needs Windows + `pip install legacy-bridge[windows]`.
On other platforms the layer reports `unavailable` and the router skips it.
"""
from __future__ import annotations

import re
import sys
from typing import Any

import time

from ..models import BackendError, BackendHealth, Probe, Support
from .base import Backend, ExecContext, cast


class UIABackend(Backend):
    layer = "L2"

    def __init__(self, session):  # type: ignore[no-untyped-def]
        super().__init__(session)
        self._win = None

    # ---- plumbing -------------------------------------------------------------
    def _pywinauto(self):  # type: ignore[no-untyped-def]
        if sys.platform != "win32":
            raise BackendError("UIA requires Windows")
        try:
            import pywinauto  # noqa: F401
            from pywinauto import Desktop
        except ImportError as e:
            raise BackendError("pywinauto not installed (pip install legacy-bridge[windows])") from e
        return Desktop

    def window(self):  # type: ignore[no-untyped-def]
        Desktop = self._pywinauto()
        if self._win is None or not self._win.exists(timeout=0):
            if not (self.manifest.tool.get("window_title_re") or self.manifest.tool.get("process_name")):
                raise BackendError("manifest declares no window (tool.window_title_re / process_name)")
            title_re = self.manifest.tool.get("window_title_re", ".*")
            pid = self.session.pid or (self.session.find_pid() if self.session.gui_running() else None)
            kw: dict[str, Any] = {"title_re": title_re}
            if pid:
                kw["process"] = pid
            if self.manifest.tool.get("window_class"):
                kw["class_name"] = self.manifest.tool["window_class"]
            self._win = Desktop(backend="uia").window(**kw)
            if not self._win.exists(timeout=5):
                raise BackendError(f"window not found ({kw})")
        return self._win

    def _ctl(self, cfg: dict[str, Any]):  # type: ignore[no-untyped-def]
        w = self.window()
        crit: dict[str, Any] = {}
        if "auto_id" in cfg:
            crit["auto_id"] = cfg["auto_id"]
        if "name" in cfg:
            crit["title"] = cfg["name"]
        if "control_type" in cfg:
            crit["control_type"] = cfg["control_type"]
        ctl = w.child_window(**crit)
        if not ctl.exists(timeout=float(cfg.get("find_timeout_s", 2))):
            raise BackendError(f"control not found in UIA tree: {crit}")
        return ctl

    # ---- health / probes ------------------------------------------------------
    def probe_health(self) -> BackendHealth:
        try:
            w = self.window()
            n = len(w.descendants())
            named = sum(1 for d in w.descendants() if d.element_info.automation_id or d.element_info.name)
            return BackendHealth("L2", "ok", f"{n} UIA elements, {named} identifiable",
                                 {"elements": n, "identifiable": named})
        except BackendError as e:
            return BackendHealth("L2", "unavailable", str(e))

    def _probe_ctl(self, cfg: dict[str, Any]) -> Probe:
        try:
            self._ctl({**cfg, "find_timeout_s": 0.5})
            return Probe(Support.OK)
        except BackendError as e:
            return Probe(Support.PROBE_FAILED, str(e))

    probe_uia_value = probe_uia_select = probe_uia_text = probe_uia_click = probe_uia_set_text = _probe_ctl

    def probe_uia_count(self, cfg: dict[str, Any]) -> Probe:
        return Probe(Support.OK) if self.health.status == "ok" else Probe(Support.PROBE_FAILED, self.health.note)

    def impl_uia_count(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        """Count descendants matching control_type and/or name regex."""
        n = 0
        for d in self.window().descendants(control_type=cfg.get("control_type")):
            if re.search(cfg.get("name_re", ".*"), d.element_info.name or ""):
                n += 1
        return n

    def probe_uia_sequence(self, cfg: dict[str, Any]) -> Probe:
        for step in cfg.get("steps", []):
            (op, arg), = step.items()
            if op in ("click", "set_text", "select") and (p := self._probe_ctl(arg)).support != Support.OK:
                return p
        return Probe(Support.OK)

    # ---- impls ----------------------------------------------------------------
    def impl_uia_value(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        ctl = self._ctl(cfg)
        try:
            v = ctl.get_value() if hasattr(ctl, "get_value") else None
        except Exception:
            v = None
        if v in (None, ""):
            v = ctl.window_text() or ctl.selected_text()
        return cast(v, cfg.get("cast"))

    def impl_uia_select(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        ctl = self._ctl(cfg)
        ctl.select(str(cfg["value"]))
        return {"selected": cfg["value"]}

    def impl_uia_text(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        text = self._ctl(cfg).window_text()
        if cfg.get("tail"):
            n = int(params.get("lines") or 20)
            text = "\n".join(text.splitlines()[-n:])
        return text

    def impl_uia_click(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        self._ctl(cfg).click_input()
        return {"clicked": cfg}

    def impl_uia_set_text(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        self._ctl(cfg).set_edit_text(str(cfg["text"]))
        return {"set": cfg.get("auto_id")}

    def menu_select(self, path: str) -> None:
        """Select `A->B->C` from the app's native (Win32) menu.

        UIA does not expose Win32 popup menus (#32768) reliably, and the title bar's
        system menu bar gets a localized name. So: resolve the path on the native menu
        with the win32 backend and send WM_COMMAND (no clicks, no focus needed);
        fall back to clicking through popups if the app doesn't use native menus."""
        from pywinauto import Desktop
        w = self.window()
        try:
            Desktop(backend="win32").window(handle=w.handle).menu_select(path.replace("->", "->"))
            return
        except Exception as e:  # not a native menu, or item text differs
            first_err = e
        w.set_focus()
        parts = [p.strip() for p in path.split("->")]

        def norm(x: str) -> str:
            return x.replace("&", "").replace("\u2026", "...").split("\t")[0].strip().lower()

        bars = [b for b in w.wrapper_object().descendants(control_type="MenuBar")
                if b.element_info.parent is not None and b.element_info.parent.control_type != "TitleBar"]
        top = next((i for b in bars for i in b.children() if norm(i.element_info.name or "") == norm(parts[0])), None)
        if top is None:
            raise BackendError(f"menu '{parts[0]}' not found ({first_err})")
        top.click_input()
        for part in parts[1:]:
            t0, hit = time.monotonic(), None
            while hit is None and time.monotonic() - t0 < 3:
                time.sleep(0.2)
                for pop in Desktop(backend="win32").windows(class_name="#32768"):
                    for it in pop.menu().items():
                        if norm(it.text()).startswith(norm(part)):
                            hit = it
                            break
            if hit is None:
                from pywinauto.keyboard import send_keys
                send_keys("{ESC}{ESC}")
                raise BackendError(f"menu item '{part}' not found under {parts[0]}")
            hit.click_input()

    def top_windows(self) -> list[Any]:
        """All visible top-level windows of the tool's process (main + dialogs)."""
        Desktop = self._pywinauto()
        pid = self.session.pid or self.session.find_pid()
        if not pid:
            return []
        # Enumerate with win32 (owned dialogs are top-level there, UIA may nest or hide them),
        # then hand back UIA WindowSpecifications so child_window()/exists() work uniformly.
        from pywinauto import Desktop as _D
        uia = Desktop(backend="uia")
        out = []
        for w in _D(backend="win32").windows(process=pid, visible_only=True):
            if w.window_text() or w.class_name() == "#32770":
                out.append(uia.window(handle=w.handle))
        return out

    def handle_popups(self, rules: list[dict[str, Any]]) -> list[str]:
        """Dismiss known popups: rules = [{title_re, press: button name | auto_id}]."""
        done: list[str] = []
        try:
            wins = self.top_windows()
        except BackendError:
            return done
        cands = []
        for w in wins:  # top-level windows plus dialogs owned by (nested under) them
            cands.append(w)
            try:
                cands += [w.child_window(handle=c.handle) for c in w.descendants(control_type="Window")]
            except Exception:
                pass
        for w in reversed(cands):
            try:  # windows come and go during startup (splash etc.): stale handles are normal
                title = w.window_text()
            except Exception:
                continue
            for r in rules:
                if re.search(r["title_re"], title):
                    target = str(r["press"])
                    for crit in ({"auto_id": target}, {"title": target}, {"title_re": target}):
                        try:
                            b = w.child_window(control_type="Button", **crit)
                            if b.exists(timeout=0.3):
                                try:
                                    b.wrapper_object().invoke()
                                except Exception:
                                    b.click_input()
                                done.append(f"{title} -> {target}")
                                break
                        except Exception:
                            continue
        return done

    def probe_uia_close(self, cfg: dict[str, Any]) -> Probe:
        return Probe(Support.OK) if self.health.status == "ok" else Probe(Support.PROBE_FAILED, self.health.note)

    def impl_uia_close(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        """Close the main window. Success = the process is gone, not 'close() returned'.
        A save prompt is answered only if cfg.discard is true."""
        self.window().close()
        self._win = None
        prompt = cfg.get("save_prompt_re")
        discard = str(cfg.get("discard")).lower() in ("true", "1")
        deadline = time.monotonic() + float(cfg.get("close_timeout_s", 10))
        discarded = False
        while time.monotonic() < deadline:
            time.sleep(0.4)
            self.session.pid = None
            if not self.session.gui_running():
                return {"closed": True, "discarded_changes": discarded}
            if prompt:
                try:
                    dlg = self.dialog(prompt, 0.3)
                except BackendError:
                    continue
                if not discard:
                    raise BackendError("save prompt is open; call again with discard=true or save first")
                b = dlg.child_window(title_re=cfg.get("discard_button_re", "^(No|否)"), control_type="Button")
                try:
                    b.wrapper_object().invoke()
                except Exception:
                    b.click_input()
                discarded = True
        raise BackendError("main window still open after close (unexpected dialog?)")

    def dialog(self, title_re: str | None = None, timeout: float = 5.0):  # type: ignore[no-untyped-def]
        """Wait for a non-main top-level window (optionally matching title_re)."""
        main_re = self.manifest.tool.get("window_title_re", "^$")
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            cands = []
            for top in self.top_windows():  # top-level + owned dialogs nested under them
                cands.append(top)
                try:
                    cands += [top.child_window(handle=c.handle) for c in top.descendants(control_type="Window")]
                except Exception:
                    pass
            for w in cands:
                try:
                    t = w.window_text()
                except Exception:
                    continue
                if (title_re and re.search(title_re, t)) or (not title_re and not re.search(main_re, t)):
                    return w
            time.sleep(0.2)
        raise BackendError(f"dialog {title_re or '(any)'} did not appear")

    def impl_uia_sequence(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        """Run steps; on failure run `cleanup` steps (e.g. press Cancel) so a half-done
        sequence doesn't leave a modal dialog blocking every later action."""
        try:
            return self._run_steps(cfg.get("steps", []), params, ctx)
        except Exception as e:
            if cfg.get("cleanup"):
                try:
                    self._run_steps(cfg["cleanup"], params, ctx)
                except Exception as ce:
                    raise BackendError(f"{e}; cleanup also failed: {ce}") from e
                raise BackendError(f"{type(e).__name__}: {e} (cleanup ran)") from e
            raise

    def _run_steps(self, steps, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        cfg = {"steps": steps}
        scope = None  # current dialog, if a step switched into one
        for i, step in enumerate(cfg.get("steps", [])):
            (op, arg), = step.items()
            if ctx.cancel.is_set():
                raise BackendError("cancelled")
            if op == "menu":  # "Generate->Tone..."
                self.menu_select(str(arg))
            elif op == "key":
                from pywinauto.keyboard import send_keys
                (scope or self.window()).set_focus()
                send_keys(str(arg))
            elif op == "dialog":  # {title_re, timeout_s}
                scope = self.dialog(arg.get("title_re"), float(arg.get("timeout_s", 5)))
            elif op == "main":
                scope = None
            elif op == "set_field":  # {name|auto_id, text, control_type?} inside current scope
                root = scope or self.window()
                crit = {k2: v2 for k2, v2 in (("title", arg.get("name")), ("auto_id", arg.get("auto_id")),
                                               ("control_type", arg.get("control_type", "Edit"))) if v2}
                ed = root.child_window(**crit)
                if not ed.exists(timeout=2):
                    raise BackendError(f"field not found: {crit}")
                try:
                    ed.set_edit_text(str(arg["text"]))
                except Exception:  # some toolkits (wx) reject ValuePattern.SetValue -> type it
                    from pywinauto.keyboard import send_keys
                    ed.click_input()
                    send_keys("^a{BACKSPACE}" + str(arg["text"]), with_spaces=True)
            elif op == "press":  # button by name inside current scope
                root = scope or self.window()
                b = root.child_window(title=str(arg), control_type="Button")
                if not b.exists(timeout=2):
                    raise BackendError(f"button not found: {arg}")
                b.invoke() if hasattr(b, "invoke") else b.click_input()
            elif op == "sleep":
                time.sleep(float(arg))
            elif op == "click":
                self.impl_uia_click(arg, params, ctx)
            elif op == "set_text":
                self.impl_uia_set_text(arg, params, ctx)
            elif op == "select":
                self.impl_uia_select(arg, params, ctx)
            elif op == "wait_for":
                self.session.wait_for(arg, ctx)
            else:
                raise BackendError(f"unknown step {op}")
            ctx.progress(100.0 * (i + 1) / len(cfg["steps"]), f"step {op}")
        return {"steps": len(cfg["steps"])}

    # ---- state ----------------------------------------------------------------
    def detect_state(self, rules: dict[str, dict[str, Any]]) -> str | None:
        try:
            Desktop = self._pywinauto()
        except BackendError:
            return None
        # any extra top-level window of the process (modal dialog)
        for state, rule in rules.items():
            if rule.get("dialog_open"):
                try:
                    main_re = self.manifest.tool.get("window_title_re", "^$")
                    if any(not re.search(main_re, w.window_text()) for w in self.top_windows()):
                        return state
                except Exception:
                    pass
        # popups first: they are separate top-level windows
        for state, rule in rules.items():
            if "window_title_re" in rule and state != "MainWindow":
                if Desktop(backend="uia").window(title_re=rule["window_title_re"]).exists(timeout=0):
                    return state
        for state, rule in rules.items():
            if "control" in rule:
                try:
                    ctl = self._ctl({"auto_id": rule["control"], "find_timeout_s": 0.2})
                    if ctl.is_enabled() == rule.get("enabled", True):
                        return state
                except BackendError:
                    pass
        for state, rule in rules.items():
            if "window_title_re" in rule:
                try:
                    if re.search(rule["window_title_re"], self.window().window_text()):
                        return state
                except BackendError:
                    pass
        return None

    # ---- low-level ------------------------------------------------------------
    def ui_tree(self, max_depth: int = 4) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        root = self.window()

        def walk(el, depth):  # type: ignore[no-untyped-def]
            if depth > max_depth:
                return
            info = el.element_info
            r = info.rectangle
            out.append({
                "id": f"#{len(out) + 1}",
                "depth": depth,
                "type": info.control_type,
                "name": info.name,
                "auto_id": info.automation_id,
                "enabled": info.enabled,
                "rect": [r.left, r.top, r.right, r.bottom],
            })
            for c in el.children():
                walk(c, depth + 1)

        walk(root.wrapper_object(), 0)
        self.session.set_marks(out, source="L2")
        return out

    def click(self, target: str) -> None:
        if target.startswith(("auto_id:", "name:")):
            kind, val = target.split(":", 1)
            crit = {"auto_id": val} if kind == "auto_id" else {"title": val}
            for w in self.top_windows() or [self.window()]:  # dialogs first-class, not just the main window
                c = w.child_window(**crit)
                if c.exists(timeout=0.3):
                    try:  # InvokePattern works even if the window isn't foreground
                        c.wrapper_object().invoke()
                    except Exception:
                        w.set_focus()
                        c.click_input()
                    return
            raise BackendError(f"no element {target} in any window of the tool")
        mark = self.session.mark(target)
        if mark.get("source") != "L2":
            raise BackendError(f"{target} is not a UIA element")
        from pywinauto import mouse

        l, t, r, b = mark["rect"]
        mouse.click(coords=((l + r) // 2, (t + b) // 2))

    def type_text(self, text: str) -> None:
        from pywinauto.keyboard import send_keys

        self.window().set_focus()
        send_keys(text, with_spaces=True, pause=0.01)

    def key(self, combo: str) -> None:
        from pywinauto.keyboard import send_keys

        self.window().set_focus()
        send_keys(combo)
