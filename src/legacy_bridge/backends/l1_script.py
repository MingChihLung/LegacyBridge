# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""L1 - the tool's own automation surface: batch CLI, COM/OLE, macros."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from typing import Any

from ..models import BackendError, BackendHealth, Probe, Support
from .base import Backend, ExecContext, cast

_FLAG = re.compile(r"(?<![\w/])((?:/|--)[A-Za-z?][\w-]*)")


class ScriptBackend(Backend):
    layer = "L1"

    @property
    def base_cmd(self) -> list[str]:
        return [str(x) for x in self.manifest.tool.get("cli_base") or self.manifest.tool.get("launch") or []]

    def probe_health(self) -> BackendHealth:
        cmd = self.base_cmd
        if not cmd or not (shutil.which(cmd[0]) or __import__("os").path.exists(cmd[0])):
            if self._com_available():  # no CLI, but COM automation can still work
                return BackendHealth("L1", "partial", "no CLI; COM available", {"cli_flags": [], "com": True})
            return BackendHealth("L1", "unavailable", "tool executable not found")
        flags: set[str] = set()
        for help_arg in self.manifest.tool.get("help_args", ["/?", "--help"]):
            try:
                out = subprocess.run(cmd + [help_arg], capture_output=True, text=True, errors="replace", timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            flags |= set(_FLAG.findall(out.stdout + out.stderr))
            if flags:
                break
        com = self._com_available()
        details = {"cli_flags": sorted(flags), "com": com}
        if flags:
            return BackendHealth("L1", "ok", f"CLI exposes {len(flags)} flags", details)
        return BackendHealth("L1", "partial", "CLI runs but help text lists no flags", details)

    # ---- CLI --------------------------------------------------------------
    def probe_cli(self, cfg: dict[str, Any]) -> Probe:
        if self.health.status == "unavailable":
            return Probe(Support.PROBE_FAILED, self.health.note)
        known = set(self.health.details.get("cli_flags") or [])
        wanted = {a for a in cfg.get("args", []) if isinstance(a, str) and _FLAG.fullmatch(a)}
        missing = wanted - known
        if known and missing:
            return Probe(Support.PROBE_FAILED, f"CLI help does not list {sorted(missing)}")
        return Probe(Support.OK if known else Support.DEGRADED, "" if known else "flags unverified")

    def impl_cli(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        cmd = self.base_cmd + [str(a) for a in cfg.get("args", [])]
        prog = re.compile(cfg["progress_re"]) if cfg.get("progress_re") else None
        timeout = float(cfg.get("timeout_s", 60))
        t0 = time.monotonic()
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        except OSError as e:
            raise BackendError(f"cannot start {cmd[0]}: {e}") from e
        lines: list[str] = []
        assert p.stdout is not None
        for line in p.stdout:
            line = line.rstrip("\r\n")
            lines.append(line)
            if prog and (m := prog.search(line)):
                ctx.progress(float(m.group(1)), line)
            if ctx.cancel.is_set():
                p.kill()
                raise BackendError("cancelled")
            if time.monotonic() - t0 > timeout:
                p.kill()
                raise BackendError(f"timeout after {timeout}s")
        rc = p.wait()
        out = "\n".join(lines)
        if rc != cfg.get("ok_rc", 0):
            raise BackendError(f"exit code {rc}: {out[-500:]}")
        if cfg.get("ok_re") and not re.search(cfg["ok_re"], out, re.M):
            raise BackendError(f"output did not match ok_re: {out[-500:]}")
        if cfg.get("parse") == "stdout_strip":
            return cast(out.strip(), cfg.get("cast"))
        return {"stdout": out[-2000:], "rc": rc}

    # ---- named pipe / FIFO command channel (e.g. Audacity mod-script-pipe) ----
    def _pipe_names(self, cfg: dict[str, Any]) -> tuple[str, str, str]:
        if sys.platform == "win32":
            return cfg["to_pipe"], cfg["from_pipe"], cfg.get("eol", "\r\n\0")
        import os as _os
        uid = str(_os.getuid())
        return (cfg.get("to_fifo", "").replace("{uid}", uid), cfg.get("from_fifo", "").replace("{uid}", uid),
                cfg.get("eol_posix", "\n"))

    # One connection per process, kept open: some servers (Audacity's mod-script-pipe on
    # Windows) accept exactly ONE client session per launch and tear the pipe down when
    # that client disconnects. Probing must therefore never open the pipe.
    _pipes: dict[tuple[str, str], tuple[Any, Any]] = {}

    def _pipe_listed(self, to: str, frm: str) -> bool:
        import os as _os
        if sys.platform == "win32":
            try:
                names = set(_os.listdir("\\\\.\\pipe\\"))
            except OSError:
                return False
            return to.rsplit("\\", 1)[-1] in names and frm.rsplit("\\", 1)[-1] in names
        return bool(to) and _os.path.exists(to) and _os.path.exists(frm)

    def probe_pipe_cmd(self, cfg: dict[str, Any]) -> Probe:
        to, frm, _ = self._pipe_names(cfg)
        if (to, frm) in ScriptBackend._pipes:
            return Probe(Support.OK, "pipe connected (held by this process)")
        if self._pipe_listed(to, frm):
            return Probe(Support.OK, "pipe listening")
        return Probe(Support.PROBE_FAILED, cfg.get("unavailable_note", f"pipe {to} not present"))

    def _connect(self, to: str, frm: str) -> tuple[Any, Any]:
        conn = ScriptBackend._pipes.get((to, frm))
        if conn is None:
            tof = open(to, "w", encoding="utf-8")
            fromf = open(frm, encoding="utf-8", errors="replace")
            conn = ScriptBackend._pipes[(to, frm)] = (tof, fromf)
        return conn

    def impl_pipe_cmd(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        """Send one or more commands; each response ends with a blank line.
        cfg: to_pipe, from_pipe, commands: [..] (or command), fail_re, parse, expect_eof."""
        to, frm, eol = self._pipe_names(cfg)
        cmds = cfg.get("commands") or [cfg["command"]]
        outs: list[str] = []
        try:
            tof, fromf = self._connect(to, frm)
            for cmd in cmds:
                if ctx.cancel.is_set():
                    raise BackendError("cancelled")
                tof.write(str(cmd) + eol)
                tof.flush()
                resp, t0 = "", time.monotonic()
                while True:
                    line = fromf.readline()
                    if line == "" and cfg.get("expect_eof"):  # e.g. Exit: the server goes away
                        break
                    if line == "" and time.monotonic() - t0 > float(cfg.get("timeout_s", 30)):
                        raise BackendError(f"pipe timeout on {cmd!r}")
                    if line == "\n" and resp:
                        break
                    resp += line
                outs.append(resp)
                if re.search(cfg.get("fail_re", r"BatchCommand finished: Failed"), resp):
                    raise BackendError(f"{cmd!r} failed: {resp.strip()[-300:]}")
                ctx.progress(100.0 * len(outs) / len(cmds), str(cmd).split(":")[0])
            if cfg.get("expect_eof"):
                ScriptBackend._pipes.pop((to, frm), None)
        except OSError as e:
            ScriptBackend._pipes.pop((to, frm), None)
            raise BackendError(f"pipe error: {e}") from e
        last = outs[-1]
        if cfg.get("parse") == "json_before_status":
            import json as _json
            body = last.rsplit("BatchCommand finished", 1)[0].strip()
            try:
                val: Any = _json.loads(body)
            except ValueError as e:
                raise BackendError(f"bad JSON from pipe: {body[:200]}") from e
            if cfg.get("select") == "len":
                return len(val)
            if cfg.get("select") == "sel_len":  # {"Start": t0, "End": t1}
                return round(float(val.get("End", 0)) - float(val.get("Start", 0)), 6)
            if cfg.get("select") == "text":
                return body
            return val
        if cfg.get("parse") == "text_before_status":
            return cast(last.rsplit("BatchCommand finished", 1)[0].strip(), cfg.get("cast"))
        return {"responses": [o.strip()[-300:] for o in outs]}

    # ---- COM ----------------------------------------------------------------
    def _com_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import win32com.client  # noqa: F401
            return True
        except ImportError:
            return False

    def probe_com_call(self, cfg: dict[str, Any]) -> Probe:
        if not self._com_available():
            return Probe(Support.PROBE_FAILED, "pywin32/COM not available")
        import win32com.client

        try:
            win32com.client.Dispatch(cfg["progid"])
            return Probe(Support.OK)
        except Exception as e:  # pywintypes.com_error
            return Probe(Support.PROBE_FAILED, f"ProgID {cfg['progid']}: {e}")

    def impl_com_call(self, cfg: dict[str, Any], params: dict[str, Any], ctx: ExecContext) -> Any:
        """cfg: progid, then either method + args, or set_property + value, or get_property."""
        import win32com.client

        obj = win32com.client.Dispatch(cfg["progid"])
        try:
            if "method" in cfg:
                return getattr(obj, cfg["method"])(*cfg.get("args", []))
            if "set_property" in cfg:
                setattr(obj, cfg["set_property"], cfg["value"])
                return {"set": cfg["set_property"]}
            return cast(getattr(obj, cfg["get_property"]), cfg.get("cast"))
        except Exception as e:
            raise BackendError(f"COM {cfg['progid']}: {e}") from e
