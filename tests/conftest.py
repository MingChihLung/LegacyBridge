# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = str(ROOT / "manifests" / "legacy_flasher.yaml")


@pytest.fixture
def tool_home(tmp_path, monkeypatch):
    """Isolated LegacyFlasher home (INI + logs) per test; L0/L1 run for real against it."""
    monkeypatch.setenv("LEGACY_FLASHER_HOME", str(tmp_path))
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"\x00" * 16)
    return tmp_path


@pytest.fixture
def manifest_path():
    return MANIFEST


SIM = {"L2": {"step_delay": 0.01}, "L3": {"step_delay": 0.01}, "values": {"get_baudrate": 9600}}
