# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Registry + router: real L0/L1 against the demo tool, simulated L2/L3."""
import copy

from legacy_bridge import SessionManager, load_manifest
from legacy_bridge.backends.base import ExecContext
from legacy_bridge.models import Support

from conftest import SIM


def open_session(manifest_path, sim=None):
    return SessionManager(load_manifest(manifest_path)).open(sim=copy.deepcopy(sim or SIM))


def test_capability_matrix_shape(tool_home, manifest_path):
    s = open_session(manifest_path)
    caps = s.registry.get_capabilities()
    assert caps["tool"] == "LegacyFlasher" and caps["version"] == "3.2.1"
    assert set(caps["backends"]) == {"L0", "L1", "L2", "L3"}
    assert caps["backends"]["L1"]["details"]["cli_flags"] == ["/?", "/baud", "/flash", "/silent", "/version"]
    acts = {a["name"]: a for a in caps["actions"]}
    assert acts["get_version"]["support"] == {"L0": "unsupported", "L1": "ok", "L2": "unsupported", "L3": "unsupported"}
    assert acts["flash_firmware"]["requires_confirm"] and acts["flash_firmware"]["long_running"]
    assert caps["lowlevel"]["click"] == ["L2", "L3"]


def test_needs_restart_is_ranked_last(tool_home, manifest_path):
    s = open_session(manifest_path)
    s.router.run("set_baudrate", {"value": 57600}, force="L1")  # creates the INI
    spec = s.manifest.actions["set_baudrate"]
    assert s.registry.support(spec, "L0").support == Support.NEEDS_RESTART
    assert [l for l, _ in s.registry.ranked_layers(spec)] == ["L2", "L1", "L3", "L0"]


def test_real_l1_then_l0_readback(tool_home, manifest_path):
    s = open_session(manifest_path)
    r = s.router.run("set_baudrate", {"value": 115200}, force="L1")
    assert r.ok and r.backend == "L1" and r.verified is True
    r = s.router.run("get_baudrate", {})
    assert r.ok and r.backend == "L0" and r.value == 115200
    assert "baud set to 115200" in s.router.run("read_log", {"lines": 5}).value


def test_fallback_on_injected_failure_and_degrade(tool_home, manifest_path):
    sim = copy.deepcopy(SIM)
    sim["L2"]["fail"] = ["set_baudrate"]
    s = open_session(manifest_path, sim)
    r = s.router.run("set_baudrate", {"value": 57600})
    assert r.ok and r.backend == "L1"
    assert r.fallback_chain[0]["layer"] == "L2"
    s.router.run("set_baudrate", {"value": 9600})
    spec = s.manifest.actions["set_baudrate"]
    assert s.registry.support(spec, "L2").support == Support.DEGRADED
    assert s.registry.ranked_layers(spec)[0][0] == "L1"  # degraded L2 moved behind L1


def test_confirmation_and_state_gate(tool_home, manifest_path):
    s = open_session(manifest_path)
    r = s.router.run("flash_firmware", {"path": str(tool_home / "fw.bin")})
    assert not r.ok and r.error == "confirmation required" and "fw.bin" in r.explain["confirm"]
    s.sim_world["state"] = "Flashing"
    r = s.router.run("set_baudrate", {"value": 57600})
    assert not r.ok and "requires state" in r.error
    assert "set_baudrate" not in s.registry.available_now()["actions"]
    assert "requires state" in s.registry.explain("set_baudrate")["reason"]


def test_param_validation(tool_home, manifest_path):
    s = open_session(manifest_path)
    assert "not in" in s.router.run("set_baudrate", {"value": 1234}).error
    assert "missing param" in s.router.run("set_baudrate", {}).error
    assert "unknown action" in s.router.run("nope", {}).error


def test_long_running_real_cli_progress_and_verify(tool_home, manifest_path):
    s = open_session(manifest_path)
    prog = []
    r = s.router.run("flash_firmware", {"path": str(tool_home / "fw.bin")}, confirmed=True,
                     ctx=ExecContext(session=s, progress=lambda p, m: prog.append(p)))
    assert r.ok and r.backend == "L1" and r.verified is True
    assert prog == [10, 30, 50, 70, 90, 100]


def test_layer_unavailable_is_reported(tool_home, manifest_path):
    sim = copy.deepcopy(SIM)
    sim["L2"]["available"] = False
    s = open_session(manifest_path, sim)
    caps = s.registry.get_capabilities()
    assert caps["backends"]["L2"]["status"] == "unavailable"
    assert caps["lowlevel"]["get_ui_tree"] == []
    ex = s.registry.explain("set_baudrate")
    assert ex["blocked_layers"]["L2"]["support"] == "probe_failed"


def test_headless_layers_allowed_when_gui_not_running(tool_home, manifest_path):
    sim = copy.deepcopy(SIM)
    sim["gui_running"] = False
    sim["state"] = None  # sim detects nothing -> NotRunning
    s = open_session(manifest_path, sim)
    ent = next(a for a in s.registry.get_capabilities()["actions"] if a["name"] == "set_baudrate")
    assert ent["available_now"] and ent["support"]["L2"] == "needs_state" and ent["support"]["L1"] == "ok"
    r = s.router.run("set_baudrate", {"value": 57600})
    assert r.ok and r.backend in ("L1", "L0")
