# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""L0 - talk to the tool's backend directly: files, INI, DB, DLL exports."""
from __future__ import annotations

import ctypes
import os
import re
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any

from ..models import BackendError, BackendHealth, Probe, Support
from .base import Backend, ExecContext, cast


class NativeBackend(Backend):
    layer = "L0"

    def probe_health(self) -> BackendHealth:
        home = self.manifest.vars.get("home")
        if home and Path(home).is_dir():
            return BackendHealth("L0", "ok", f"tool home {home} reachable", {"home": home})
        return BackendHealth("L0", "partial", "tool home not found; only absolute-path impls may work")

    # ---- INI (line-preserving: keeps case, comments, top-level keys, % signs) ----
    def probe_ini_read(self, cfg: dict[str, Any]) -> Probe:
        f = Path(cfg["file"])
        if not f.exists():
            return Probe(Support.PROBE_FAILED, f"{f} missing")
        return Probe(Support.OK)

    def impl_ini_read(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        if "path" in cfg:  # "Section/Sub/Key" -> section "Section/Sub", key "Key"
            sec, key = str(cfg["path"]).rsplit("/", 1)
        else:
            sec, key = cfg["section"], cfg["key"]
        v = IniFile(cfg["file"]).get(sec, key)
        if v is None:
            raise BackendError(f"ini key missing: [{sec}] {key}")
        return cast(v, cfg.get("cast"))

    def probe_ini_write(self, cfg: dict[str, Any]) -> Probe:
        f = Path(cfg["file"])
        target = f if f.exists() else f.parent
        if not target.exists() or not os.access(target, os.W_OK):
            return Probe(Support.PROBE_FAILED, f"{target} not writable")
        if self.session.gui_running():
            return Probe(Support.NEEDS_RESTART, cfg.get("note", "GUI is running; INI change applies on restart"))
        return Probe(Support.OK)

    def impl_ini_write(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        """Single key (section/key/value) or many: values: {"Section/Key": value | {mtime_iso: path}}."""
        ini = IniFile(cfg["file"])
        items = dict(cfg.get("values") or {})
        if "key" in cfg:
            items[f"{cfg['section']}/{cfg['key']}"] = cfg.get("value", params.get("value"))
        for path, val in items.items():
            section, key = path.rsplit("/", 1)
            if isinstance(val, dict) and "mtime_iso" in val:
                import datetime as _dt
                val = _dt.datetime.fromtimestamp(os.path.getmtime(val["mtime_iso"])).strftime("%Y-%m-%dT%H:%M:%S")
            ini.set(section, key, str(val))
        ini.save(spaces=cfg.get("spaces", False))
        return {"written": sorted(items)}

    # ---- file facts ---------------------------------------------------------
    def impl_file_exists(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        p = Path(cfg["file"])
        if cfg.get("bool"):
            return p.exists() and p.stat().st_size > int(cfg.get("min_bytes", 0))
        return {"exists": p.exists(), "bytes": p.stat().st_size if p.exists() else 0}

    def probe_file_version(self, cfg: dict[str, Any]) -> Probe:
        if os.name != "nt":
            return Probe(Support.PROBE_FAILED, "file version resources need Windows")
        return Probe(Support.OK) if Path(cfg["file"]).exists() else Probe(Support.PROBE_FAILED, f"{cfg['file']} missing")

    def impl_file_version(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        """Read VS_FIXEDFILEINFO from an exe/dll (Windows)."""
        ver = ctypes.windll.version  # type: ignore[attr-defined]
        f = str(cfg["file"])
        size = ver.GetFileVersionInfoSizeW(f, None)
        if not size:
            raise BackendError(f"no version resource in {f}")
        buf = ctypes.create_string_buffer(size)
        ver.GetFileVersionInfoW(f, 0, size, buf)
        p, n = ctypes.c_void_p(), ctypes.c_uint()
        ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n))
        ms, ls = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32))[2:4]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"

    # ---- plain files ------------------------------------------------------
    def probe_file_tail(self, cfg: dict[str, Any]) -> Probe:
        return Probe(Support.OK) if Path(cfg["file"]).exists() else Probe(Support.PROBE_FAILED, f"{cfg['file']} missing")

    def impl_file_tail(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        n = int(params.get("lines") or cfg.get("lines") or 20)
        try:
            with open(cfg["file"], encoding=cfg.get("encoding", "utf-8"), errors="replace") as fh:
                return "".join(deque(fh, maxlen=n))
        except OSError as e:
            raise BackendError(str(e)) from e

    # ---- SQLite -----------------------------------------------------------
    def impl_sqlite_query(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        with sqlite3.connect(f"file:{cfg['file']}?mode={'rw' if cfg.get('write') else 'ro'}", uri=True) as db:
            cur = db.execute(cfg["sql"], cfg.get("args", []))
            cols = [c[0] for c in cur.description or []]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---- DLL export ---------------------------------------------------------
    def probe_ctypes_call(self, cfg: dict[str, Any]) -> Probe:
        try:
            lib = ctypes.CDLL(cfg["dll"]) if cfg.get("abi", "cdecl") == "cdecl" else ctypes.WinDLL(cfg["dll"])  # type: ignore[attr-defined]
            getattr(lib, cfg["symbol"])
            return Probe(Support.OK)
        except (OSError, AttributeError) as e:
            return Probe(Support.PROBE_FAILED, f"{cfg.get('dll')}!{cfg.get('symbol')}: {e}")

    def impl_ctypes_call(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        """cfg: dll, symbol, argtypes: [c_int, c_char_p...], restype, args: [...]"""
        lib = ctypes.CDLL(cfg["dll"]) if cfg.get("abi", "cdecl") == "cdecl" else ctypes.WinDLL(cfg["dll"])  # type: ignore[attr-defined]
        fn = getattr(lib, cfg["symbol"])
        fn.argtypes = [getattr(ctypes, t) for t in cfg.get("argtypes", [])]
        fn.restype = getattr(ctypes, cfg["restype"]) if cfg.get("restype") else ctypes.c_int
        args = [a.encode() if isinstance(a, str) else a for a in cfg.get("args", [])]
        rc = fn(*args)
        if "ok_values" in cfg and rc not in cfg["ok_values"]:
            raise BackendError(f"{cfg['symbol']} returned {rc}")
        return rc

    # ---- state -----------------------------------------------------------
    def detect_state(self, rules: dict[str, dict[str, Any]]) -> str | None:
        for state, rule in rules.items():
            if "log_last_line_re" in rule:
                log = Path(self.manifest.vars.get("home", ".")) / rule.get("log", "logs/lf.log")
                try:
                    last = log.read_text(encoding="utf-8", errors="replace").rstrip().splitlines()[-1]
                except (OSError, IndexError):
                    continue
                if re.search(rule["log_last_line_re"], last):
                    return state
        return None


class IniFile:
    """Minimal INI editor that preserves the file (wxFileConfig / Win32 INI friendly)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        try:
            self.lines = self.path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except FileNotFoundError:
            self.lines = []

    def _find(self, section: str, key: str) -> tuple[int | None, int | None]:
        """(index of key line, index after the section's last line)."""
        cur, sec_end, hit = "", None, None
        for i, line in enumerate(self.lines):
            t = line.strip()
            if t.startswith("[") and t.endswith("]"):
                if cur.lower() == section.lower() and sec_end is None:
                    sec_end = i
                cur = t[1:-1]
                continue
            if cur.lower() == section.lower() and "=" in t and not t.startswith((";", "#")):
                if t.split("=", 1)[0].strip().lower() == key.lower():
                    hit = i
        if cur.lower() == section.lower() and sec_end is None:
            sec_end = len(self.lines)
        return hit, sec_end

    def get(self, section: str, key: str) -> str | None:
        i, _ = self._find(section, key)
        return None if i is None else self.lines[i].split("=", 1)[1].strip()

    def set(self, section: str, key: str, value: str) -> None:
        i, end = self._find(section, key)
        line = f"{key}={value}"
        if i is not None:  # keep the key's original spelling
            self.lines[i] = self.lines[i].split("=", 1)[0].rstrip() + "=" + value
        elif end is not None:
            while end > 0 and not self.lines[end - 1].strip():
                end -= 1
            self.lines.insert(end, line)
        else:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines += [f"[{section}]", line]

    def save(self, spaces: bool = False) -> None:
        if spaces:
            self.lines = [l.replace("=", " = ", 1) if "=" in l and not l.strip().startswith(("[", ";", "#")) and " = " not in l
                          else l for l in self.lines]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
