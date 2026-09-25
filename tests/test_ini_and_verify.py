# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

import copy

from legacy_bridge import SessionManager, load_manifest
from legacy_bridge.backends.l0_native import IniFile

from conftest import SIM


def test_inifile_preserves_wx_style(tmp_path):
    f = tmp_path / "a.cfg"
    f.write_text("PrefsVersion=1.1.1r1\n[GUI]\nShowSplashScreen=1\nPath=C:\\\\x%y\n[Module]\nmod-ogg=1\n", encoding="utf-8")
    ini = IniFile(str(f))
    ini.set("Module", "mod-script-pipe", "1")
    ini.set("GUI", "showsplashscreen", "0")        # case-insensitive match, keeps original spelling
    ini.set("Locale", "Language", "en")
    ini.save()
    text = f.read_text(encoding="utf-8")
    assert text.startswith("PrefsVersion=1.1.1r1\n")  # top-level key survives
    assert "ShowSplashScreen=0" in text and "Path=C:\\\\x%y" in text
    assert IniFile(str(f)).get("module", "MOD-SCRIPT-PIPE") == "1"
    assert "[Locale]\nLanguage=en" in text


def test_increases_by_postcondition(tool_home, manifest_path):
    m = load_manifest(manifest_path)
    m.actions["set_baudrate"].verify = {"observe": "get_baudrate", "increases_by": 1}
    s = SessionManager(m).open(sim=copy.deepcopy(SIM))
    l2 = s.backends["L2"]
    delta = {"v": 1}
    orig = l2.execute

    def bump(spec, cfg, params, ctx):
        if spec.name == "set_baudrate":
            s.sim_world["values"]["get_baudrate"] += delta["v"]
            return {"sim": "bumped"}
        return orig(spec, cfg, params, ctx)

    l2.execute = bump
    assert s.router.run("set_baudrate", {"value": 57600}, force="L2").verified is True
    delta["v"] = 0  # action "succeeds" but changes nothing -> relative postcondition must fail
    r = s.router.run("set_baudrate", {"value": 57600}, force="L2")
    assert not r.ok and r.fallback_chain[0]["error"] == "postcondition failed"
