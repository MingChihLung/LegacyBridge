# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

import json
from legacy_bridge import load_manifest, SessionManager
s = SessionManager(load_manifest("win_smoke.yaml")).open()
caps = s.registry.get_capabilities()
out = {"support": {a["name"]: a["support"] for a in caps["actions"]}}
for name, params in [("screen_width", {}), ("tick_count", {}), ("folder_exists", {"path": "C:\\Windows"}),
                     ("folder_exists", {"path": "C:\\NoSuchDir_xyz"}), ("windows_dir_via_com", {}), ("missing_dll", {})]:
    r = s.router.run(name, params)
    out[f"{name}{params or ''}"] = {"ok": r.ok, "via": r.backend, "value": r.value, "error": r.error}
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
