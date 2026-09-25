# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Download Audacity 3.7.9 portable, create venv, install legacy-bridge[windows]."""
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "https://github.com/audacity/audacity/releases/download/Audacity-3.7.9/audacity-win-3.7.9-64bit.zip"
AUD = ROOT / "audacity"


def step(msg):
    print(f"== {msg}", flush=True)


if not (AUD / "Audacity.exe").exists():
    z = ROOT / "audacity-3.7.9.zip"
    if not z.exists() or z.stat().st_size < 20_000_000:
        step(f"download {URL}")
        urllib.request.urlretrieve(URL, z)
    step(f"extract {z.stat().st_size} bytes")
    tmp = ROOT / "_aud_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    zipfile.ZipFile(z).extractall(tmp)
    exe = next(tmp.rglob("Audacity.exe"))
    shutil.rmtree(AUD, ignore_errors=True)
    shutil.move(str(exe.parent), AUD)
    shutil.rmtree(tmp, ignore_errors=True)
(AUD / "Portable Settings").mkdir(exist_ok=True)
step("audacity ready: " + str(AUD / "Audacity.exe"))

venv = ROOT / ".venv"
if not (venv / "Scripts" / "python.exe").exists():
    step("create venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
py = str(venv / "Scripts" / "python.exe")
step("pip install legacy-bridge[windows,dev]")
subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], check=False)
r = subprocess.run([py, "-m", "pip", "install", "-e", str(ROOT / "legacy-bridge") + "[windows,dev]"])
step(f"pip rc={r.returncode}")
subprocess.run([py, "-c", "import mcp, pywinauto, mss, cv2, pyautogui; print('mcp', mcp.__file__); "
                "import importlib.metadata as m; print({p: m.version(p) for p in ['mcp','pywinauto','mss','opencv-python','rapidocr-onnxruntime']})"])
