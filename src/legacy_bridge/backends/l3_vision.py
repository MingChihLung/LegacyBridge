# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""L3 - screenshots + input injection. Works on anything that draws pixels.

Target location strategy (first that succeeds):
  1. template image match (OpenCV)         - deterministic, cached
  2. OCR text match (rapidocr)             - for labelled buttons/fields
  3. server-side VLM grounding on a SoM     - only if LB_VLM_MODEL + API key set
     screenshot (direct provider API; MCP Sampling is deprecated in 2026-07-28)
  4. give up -> BackendError telling the agent to use screenshot(som)+click itself

Needs `pip install legacy-bridge[windows]` (mss, pyautogui, opencv, pillow, rapidocr).
"""
from __future__ import annotations

import base64
import io
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from ..models import BackendError, BackendHealth, Probe, Support
from .base import Backend, ExecContext, cast


def _set_dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2: screenshot px == input px
        except Exception:
            pass


class VisionBackend(Backend):
    layer = "L3"

    def __init__(self, session):  # type: ignore[no-untyped-def]
        super().__init__(session)
        _set_dpi_aware()
        self._ocr = None
        self._tmpl_cache: dict[str, tuple[int, int, int, int]] = {}
        self._last_click: tuple[int, int] | None = None

    # ---- deps -----------------------------------------------------------------
    def _deps(self):  # type: ignore[no-untyped-def]
        try:
            import mss  # noqa: F401
            import numpy  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as e:
            raise BackendError(f"vision deps missing: {e.name} (pip install legacy-bridge[windows])") from e

    def probe_health(self) -> BackendHealth:
        try:
            self._deps()
            rect = self.window_rect()
        except BackendError as e:
            return BackendHealth("L3", "unavailable", str(e))
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        want = self.manifest.tool.get("window_size")
        details = {"window": [w, h], "ocr": self._ocr_engine() is not None, "vlm": bool(os.environ.get("LB_VLM_MODEL"))}
        if want and [w, h] != list(want):
            return BackendHealth("L3", "partial", f"window is {w}x{h}, templates recorded at {want}", details)
        return BackendHealth("L3", "ok", f"window {w}x{h}", details)

    # ---- geometry ---------------------------------------------------------------
    def window_rect(self) -> tuple[int, int, int, int]:
        """Screen rect of the tool window (L2 if available, else Win32 by title)."""
        if not self.manifest.tool.get("window_title_re"):
            raise BackendError("manifest declares no window (tool.window_title_re)")
        l2 = self.session.backends.get("L2")
        if l2 is not None and l2.health.status != "unavailable":
            r = l2.window().rectangle()
            return (r.left, r.top, r.right, r.bottom)
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as wt

            title_re = re.compile(self.manifest.tool.get("window_title_re", ".*"))
            found: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def cb(hwnd, _):  # type: ignore[no-untyped-def]
                buf = ctypes.create_unicode_buffer(512)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
                if ctypes.windll.user32.IsWindowVisible(hwnd) and title_re.search(buf.value):
                    found.append(hwnd)
                return True

            ctypes.windll.user32.EnumWindows(cb, 0)
            if found:
                r = wt.RECT()
                ctypes.windll.user32.GetWindowRect(found[0], ctypes.byref(r))
                return (r.left, r.top, r.right, r.bottom)
        raise BackendError("tool window not found for screenshot")

    def focus(self) -> None:
        """L3 sees and types into whatever is on screen: bring the tool to the front first
        (an occluded window yields someone else's pixels; keys go to the foreground app)."""
        l2 = self.session.backends.get("L2")
        try:
            if l2 is not None and l2.health.status != "unavailable":
                # a modal dialog owns the keyboard: focusing the main window would send
                # keys into the void (and move the dialog behind it)
                main_re = self.manifest.tool.get("window_title_re", "^$")
                dialogs = [w for w in l2.top_windows() if not re.search(main_re, w.window_text())]
                (dialogs[0] if dialogs else l2.window()).set_focus()
                time.sleep(0.2)
        except Exception:
            pass

    def grab(self):  # type: ignore[no-untyped-def]
        """Return (PIL image of the window, (left, top) screen offset)."""
        self._deps()
        import mss
        from PIL import Image

        l, t, r, b = self.window_rect()
        with mss.mss() as sct:
            shot = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        return Image.frombytes("RGB", shot.size, shot.rgb), (l, t)

    # ---- OCR / templates ---------------------------------------------------------
    def _ocr_engine(self):  # type: ignore[no-untyped-def]
        if self._ocr is None:
            try:
                from rapidocr_onnxruntime import RapidOCR

                self._ocr = RapidOCR()
            except Exception:
                self._ocr = False
        return self._ocr or None

    def ocr(self, img) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        eng = self._ocr_engine()
        if eng is None:
            raise BackendError("OCR engine not available")
        import numpy as np

        # UI text is small: OCR a 2x upscale (much more stable on menus/labels), map back.
        k = 2 if max(img.size) < 2400 else 1
        src = img.resize((img.size[0] * k, img.size[1] * k)) if k > 1 else img
        res, _ = eng(np.array(src))
        out = []
        for box, text, score in res or []:
            xs, ys = [p[0] / k for p in box], [p[1] / k for p in box]
            out.append({"text": text, "score": float(score), "rect": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]})
        return out

    def _template(self, path: str, img, min_score: float = 0.8):  # type: ignore[no-untyped-def]
        import cv2
        import numpy as np

        p = Path(path)
        if not p.is_absolute():
            p = self.manifest.path.parent / p
        if not p.exists():
            return None
        tmpl = cv2.imread(str(p))
        hay = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        res = cv2.matchTemplate(hay, tmpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score < min_score:
            return None
        h, w = tmpl.shape[:2]
        return [x, y, x + w, y + h], float(score)

    def locate(self, target: dict[str, Any]) -> tuple[int, int]:
        """Resolve a manifest target to a screen point, retrying while the UI settles
        (menus and dialogs take a moment to paint)."""
        deadline = time.monotonic() + float(target.get("timeout_s", 3))
        while True:
            try:
                return self._locate_once(target)
            except BackendError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)

    def _locate_once(self, target: dict[str, Any]) -> tuple[int, int]:
        """Resolve a manifest target {template?, text?, hint?} to a screen point."""
        img, (ox, oy) = self.grab()
        if target.get("template"):
            hit = self._template(target["template"], img, target.get("min_score", 0.8))
            if hit:
                (l, t, r, b), _ = hit
                return ox + (l + r) // 2, oy + (t + b) // 2
        text = target.get("text")
        if text and self._ocr_engine():
            # OCR is the slow part (~seconds per full window on CPU). After a click, the
            # next target (menu item, dropdown entry) is almost always just below/right of
            # it: OCR that neighbourhood first, the whole window only as a fallback.
            regions = []
            if self._last_click and target.get("near_last", True):
                lx, ly = self._last_click[0] - ox, self._last_click[1] - oy
                box = (max(0, lx - 60), max(0, ly - 20), min(img.size[0], lx + 480), min(img.size[1], ly + 560))
                regions.append(box)
            regions.append((0, 0, img.size[0], img.size[1]))
            boxes = []
            for (rx, ry, rx2, ry2) in regions:
                crop = img.crop((rx, ry, rx2, ry2))
                boxes = [{**b, "rect": [b["rect"][0] + rx, b["rect"][1] + ry, b["rect"][2] + rx, b["rect"][3] + ry]}
                         for b in self.ocr(crop) if text.lower() in b["text"].lower()]
                if boxes:
                    break
            boxes.sort(key=lambda b: len(b["text"]))  # tightest match first
            for box in boxes:
                l, t, r, b = box["rect"]
                full = box["text"]
                i = full.lower().index(text.lower())
                # OCR often merges a row (e.g. a whole menu bar) into one box: interpolate
                # the target's x position from its character offset inside the box text.
                cx = l + (r - l) * (i + len(text) / 2) / max(len(full), 1)
                return ox + int(cx), oy + (t + b) // 2
        if target.get("hint") and os.environ.get("LB_VLM_MODEL"):
            png, marks = self.screenshot(som=True)
            mark_id = self._vlm_pick(png, marks, target["hint"])
            m = next((m for m in marks if m["id"] == mark_id), None)
            if m:
                l, t, r, b = m["rect"]
                return (l + r) // 2, (t + b) // 2
        raise BackendError(
            f"L3 could not locate {target} autonomously; agent should call screenshot(som=true) and click('#n')"
        )

    def _vlm_pick(self, png: bytes, marks: list[dict[str, Any]], hint: str) -> str | None:
        """Ask a vision model (Anthropic Messages API) which mark matches the hint."""
        import httpx

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        body = {
            "model": os.environ["LB_VLM_MODEL"],
            "max_tokens": 20,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
                {"type": "text", "text": f"Numbered boxes mark UI elements. Which box is: {hint}? Reply only like #7, or NONE."},
            ]}],
        }
        r = httpx.post("https://api.anthropic.com/v1/messages", json=body, timeout=30,
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        m = re.search(r"#\d+", r.json()["content"][0]["text"])
        return m.group(0) if m else None

    # ---- probes ----------------------------------------------------------------
    def _probe_target(self, target: dict[str, Any]) -> Probe:
        if target.get("template"):
            p = Path(target["template"])
            p = p if p.is_absolute() else self.manifest.path.parent / p
            if p.exists():
                return Probe(Support.OK, "template")
        if target.get("text") and self.health.details.get("ocr"):
            return Probe(Support.OK, "ocr")
        if target.get("hint") and self.health.details.get("vlm"):
            return Probe(Support.DEGRADED, "vlm grounding", confidence=0.7)
        return Probe(Support.DEGRADED, "no template/OCR/VLM: agent-assisted only (screenshot+click)", confidence=0.4)

    def probe_vision_select(self, cfg: dict[str, Any]) -> Probe:
        return self._probe_target(cfg)

    def probe_vision_sequence(self, cfg: dict[str, Any]) -> Probe:
        worst = Probe(Support.OK)
        for step in cfg.get("steps", []):
            (op, arg), = step.items()
            if op == "click":
                p = self._probe_target(arg)
                if p.confidence < worst.confidence:
                    worst = p
        return worst

    def probe_ocr_region(self, cfg: dict[str, Any]) -> Probe:
        return Probe(Support.OK) if self.health.details.get("ocr") else Probe(Support.PROBE_FAILED, "no OCR engine")

    # ---- impls -----------------------------------------------------------------
    def impl_ocr_region(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        img, _ = self.grab()
        if "region" in cfg:
            reg = (self.manifest.tool.get("regions") or {}).get(cfg["region"])
            if reg:
                img = img.crop(tuple(reg))
        elif "anchor_text" in cfg:
            anchor = next((b for b in self.ocr(img) if cfg["anchor_text"].lower() in b["text"].lower()), None)
            if anchor is None:
                raise BackendError(f"anchor '{cfg['anchor_text']}' not found")
            dx, dy, w, h = cfg.get("offset", [0, 0, 200, 30])
            x, y = anchor["rect"][0] + dx, anchor["rect"][1] + dy
            img = img.crop((x, y, x + w, y + h))
        text = "\n".join(b["text"] for b in self.ocr(img))
        if cfg.get("cast"):
            m = re.search(r"-?\d+(\.\d+)?", text)
            if not m:
                raise BackendError(f"no number in OCR text {text!r}")
            text = m.group(0)
        return cast(text, cfg.get("cast"))

    def impl_vision_select(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        self.focus()
        self._click_point(self.locate(cfg))
        time.sleep(0.3)
        self._click_point(self.locate({"text": str(cfg["option"])}))
        return {"selected": cfg["option"]}

    def impl_vision_sequence(self, cfg, params, ctx: ExecContext):  # type: ignore[no-untyped-def]
        try:
            return self._vision_steps(cfg.get("steps", []), ctx)
        except Exception as e:
            try:  # keep evidence of what L3 saw when it gave up
                self.grab()[0].save(self.manifest.path.parent / "l3_last_failure.png")
            except Exception:
                pass
            for step in cfg.get("cleanup", [{"key": "escape"}, {"key": "escape"}]):
                try:
                    self._vision_steps([step], ctx)
                except Exception:
                    pass
            raise BackendError(f"{e} (cleanup ran)") from e

    def _vision_steps(self, steps, ctx: ExecContext):  # type: ignore[no-untyped-def]
        self.focus()
        self._last_click = None
        for i, step in enumerate(steps):
            (op, arg), = step.items()
            if ctx.cancel.is_set():
                raise BackendError("cancelled")
            if op == "click":
                self._click_point(self.locate(arg))
            elif op == "type":
                self.type_text(str(arg))
            elif op == "key":
                self.key(str(arg))
            elif op == "wait_for":
                self.session.wait_for(arg, ctx)
            else:
                raise BackendError(f"unknown step {op}")
            ctx.progress(100.0 * (i + 1) / len(steps), f"step {op}")
        return {"steps": len(steps)}

    # ---- state -----------------------------------------------------------------
    def detect_state(self, rules: dict[str, dict[str, Any]]) -> str | None:
        try:
            img, _ = self.grab()
        except BackendError:
            return None
        texts = None
        for state, rule in rules.items():
            if "template" in rule and self._template(rule["template"], img, rule.get("min_score", 0.8)):
                return state
            if "ocr_contains" in rule and self._ocr_engine():
                texts = texts if texts is not None else " ".join(b["text"] for b in self.ocr(img))
                if rule["ocr_contains"].lower() in texts.lower():
                    return state
        return None

    # ---- low-level -------------------------------------------------------------
    def screenshot(self, som: bool = False) -> tuple[bytes, list[dict[str, Any]]]:
        self.focus()
        img, (ox, oy) = self.grab()
        marks: list[dict[str, Any]] = []
        if som:
            marks = self._som_candidates(img, ox, oy)
            img = self._draw_marks(img, marks, ox, oy)
            self.session.set_marks(marks, source="L3")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), marks

    def _som_candidates(self, img, ox: int, oy: int) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        """Candidate clickable regions: UIA leaves if L2 works, plus OCR boxes and contour boxes."""
        rects: list[tuple[list[int], str, str]] = []
        l2 = self.session.backends.get("L2")
        if l2 is not None and l2.health.status == "ok":
            for el in l2.ui_tree():
                if el["depth"] > 0 and el["type"] in ("Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "ListItem", "MenuItem", "TabItem"):
                    rects.append((el["rect"], "uia", el["name"] or el["auto_id"]))
        try:
            for b in self.ocr(img):
                l, t, r, bt = b["rect"]
                rects.append(([l + ox, t + oy, r + ox, bt + oy], "ocr", b["text"]))
        except BackendError:
            pass
        try:
            import cv2
            import numpy as np

            g = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
            edges = cv2.Canny(g, 50, 150)
            cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                x, y, w, h = cv2.boundingRect(c)
                if 16 <= w <= 400 and 12 <= h <= 80:
                    rects.append(([x + ox, y + oy, x + w + ox, y + h + oy], "contour", ""))
        except ImportError:
            pass
        marks: list[dict[str, Any]] = []
        for rect, src, label in rects:  # de-duplicate overlapping boxes (IoU > 0.6)
            if any(_iou(rect, m["rect"]) > 0.6 for m in marks):
                continue
            marks.append({"id": f"#{len(marks) + 1}", "rect": rect, "via": src, "label": label})
        return marks

    def _draw_marks(self, img, marks, ox: int, oy: int):  # type: ignore[no-untyped-def]
        from PIL import ImageDraw

        img = img.copy()
        d = ImageDraw.Draw(img)
        for m in marks:
            l, t, r, b = m["rect"]
            d.rectangle([l - ox, t - oy, r - ox, b - oy], outline=(255, 0, 80), width=2)
            d.rectangle([l - ox, t - oy - 14, l - ox + 8 * len(m["id"]) + 4, t - oy], fill=(255, 0, 80))
            d.text((l - ox + 2, t - oy - 13), m["id"], fill=(255, 255, 255))
        return img

    def _click_point(self, pt: tuple[int, int]) -> None:
        import pyautogui

        pyautogui.click(*pt)
        self._last_click = pt

    def click(self, target: str) -> None:
        self.focus()
        if m := re.fullmatch(r"(\d+),(\d+)", target):  # window-relative pixel coordinates
            l, t, *_ = self.window_rect()
            self._click_point((l + int(m.group(1)), t + int(m.group(2))))
            return
        if target.startswith("text:"):
            self._click_point(self.locate({"text": target[5:]}))
            return
        mark = self.session.mark(target)
        l, t, r, b = mark["rect"]
        self._click_point(((l + r) // 2, (t + b) // 2))

    def type_text(self, text: str) -> None:
        self.focus()
        import pyautogui

        pyautogui.write(text, interval=0.01) if text.isascii() else self._paste(text)

    def _paste(self, text: str) -> None:  # non-ASCII (e.g. 中文) via clipboard
        import pyautogui
        import subprocess

        subprocess.run(["clip"], input=text.encode("utf-16le"), check=False)
        pyautogui.hotkey("ctrl", "v")

    def key(self, combo: str) -> None:
        self.focus()
        import pyautogui

        pyautogui.hotkey(*combo.lower().split("+"))


def _iou(a: list[int], b: list[int]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0
