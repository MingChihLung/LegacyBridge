# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Dump UIA info of Audacity's top-level windows (for writing the manifest)."""
import json
import sys

from pywinauto import Desktop

depth = int(sys.argv[1]) if len(sys.argv) > 1 else 3
title_filter = sys.argv[2] if len(sys.argv) > 2 else ""
out = []


def walk(el, d):
    i = el.element_info
    r = i.rectangle
    out.append({"d": d, "type": i.control_type, "name": i.name, "auto_id": i.automation_id,
                "class": i.class_name, "rect": [r.left, r.top, r.right, r.bottom], "enabled": i.enabled})
    if d < depth:
        for c in el.children():
            walk(c, d + 1)


for w in Desktop(backend="uia").windows():
    t = w.window_text()
    if "Audacity" in (w.element_info.class_name or "") or "audacity" in t.lower() or (title_filter and title_filter in t):
        pass
    else:
        try:
            if w.process_id() and "audacity" not in __import__("subprocess").run(
                    ["tasklist", "/FI", f"PID eq {w.process_id()}", "/NH"], capture_output=True, text=True).stdout.lower():
                continue
        except Exception:
            continue
    out.append({"WINDOW": t, "class": w.element_info.class_name, "pid": w.process_id()})
    walk(w, 0)
print(json.dumps(out, ensure_ascii=False, indent=0))
