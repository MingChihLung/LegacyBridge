# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

import json
import subprocess
import sys

from conftest import SIM


def lb(manifest_path, *args, env_home):
    import os

    env = {**os.environ, "LEGACY_FLASHER_HOME": str(env_home)}
    p = subprocess.run([sys.executable, "-m", "legacy_bridge.cli", "--manifest", manifest_path,
                        "--sim", json.dumps(SIM), *args], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, json.loads(p.stdout), p.stderr


def test_cli_caps_and_run(tool_home, manifest_path):
    rc, caps, _ = lb(manifest_path, "caps", env_home=tool_home)
    assert rc == 0 and {a["name"] for a in caps["actions"]} >= {"set_baudrate", "flash_firmware"}
    rc, now, _ = lb(manifest_path, "caps", "--now", env_home=tool_home)
    assert rc == 0 and "set_baudrate" in now["actions"]
    rc, res, _ = lb(manifest_path, "run", "set_baudrate", "value=57600", "--backend", "L1", env_home=tool_home)
    assert rc == 0 and res["backend"] == "L1" and res["verified"] is True


def test_cli_confirmation_exit_code_and_progress(tool_home, manifest_path):
    fw = str(tool_home / "fw.bin")
    rc, res, _ = lb(manifest_path, "run", "flash_firmware", f"path={fw}", env_home=tool_home)
    assert rc == 2 and res["error"] == "confirmation required"
    rc, res, err = lb(manifest_path, "run", "flash_firmware", f"path={fw}", "--confirm", env_home=tool_home)
    assert rc == 0 and res["backend"] == "L1"
    assert '"progress": 100.0' in err


def test_cli_explain_and_ui(tool_home, manifest_path):
    rc, ex, _ = lb(manifest_path, "explain", "get_version", env_home=tool_home)
    assert rc == 0 and ex["fallback_chain"] == ["L1"]
    rc, tree, _ = lb(manifest_path, "ui", "tree", env_home=tool_home)
    assert rc == 0 and tree["elements"][1]["auto_id"] == "cmbBaud"
