# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""lb-test job runner (Windows side).

Runs ONLY Python scripts that live inside this folder, one job at a time:
  jobs/<id>.json    {"script": "lbcli.py", "args": [...], "venv": true, "timeout": 600, "detach": false}
  results/<id>.json {"rc":..., "stdout":..., "stderr":..., "ms":...}
Create jobs/STOP to exit. Start with:  python runner.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS, RESULTS = ROOT / "jobs", ROOT / "results"
JOBS.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def run(job_id: str, job: dict) -> dict:
    script = (ROOT / job["script"]).resolve()
    if ROOT not in script.parents or script.suffix != ".py" or not script.exists():
        return {"rc": -1, "error": f"refused: {job['script']} is not a .py file inside {ROOT}"}
    py = str(VENV_PY) if job.get("venv") and VENV_PY.exists() else sys.executable
    cmd = [py, str(script), *[str(a) for a in job.get("args", [])]]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **job.get("env", {})}
    t0 = time.time()
    if job.get("detach"):
        p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                             creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        return {"rc": 0, "pid": p.pid, "detached": True}
    # stream output live to this console while also capturing it
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines: list[str] = []
    deadline = t0 + job.get("timeout", 600)
    assert p.stdout is not None
    for line in p.stdout:
        lines.append(line)
        print("   | " + line.rstrip()[:200], flush=True)
        if time.time() > deadline:
            p.kill()
            return {"rc": -2, "error": "timeout", "stdout": "".join(lines)[-60000:]}
    rc = p.wait()
    return {"rc": rc, "stdout": "".join(lines)[-60000:], "ms": int((time.time() - t0) * 1000)}


def main() -> None:
    print(f"[runner] watching {JOBS}  (python {sys.version.split()[0]})  - create jobs\\STOP to quit", flush=True)
    while True:
        if (JOBS / "STOP").exists():
            (JOBS / "STOP").unlink()
            print("[runner] stop", flush=True)
            return
        for f in sorted(JOBS.glob("*.json")):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue  # still being written
            f.unlink()
            print(f"[runner] {f.stem}: {job.get('script')} {job.get('args', [])}", flush=True)
            res = {"id": f.stem, "job": job, **run(f.stem, job)}
            tmp = RESULTS / f"{f.stem}.tmp"
            tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(RESULTS / f"{f.stem}.json")
            print(f"[runner] {f.stem}: rc={res.get('rc')}", flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
