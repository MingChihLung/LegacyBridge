# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""LegacyFlasher - a stand-in "old tool" used to exercise legacy-bridge.

It deliberately has the entry points a typical legacy tool has:
  * L0: an INI settings file and a plain-text log file next to the exe
  * L1: a batch-mode command line (/? /baud /flash /silent /version)
  * L2/L3: a Tk GUI (Tk exposes little to UIA on Windows, which is realistic:
    it forces the router to fall back to vision for some controls)

Run `python legacy_flasher.py` for the GUI, or pass arguments for batch mode.
"""
from __future__ import annotations

import configparser
import datetime as _dt
import os
import sys
import time
from pathlib import Path

VERSION = "3.2.1"
HOME = Path(os.environ.get("LEGACY_FLASHER_HOME", Path(__file__).resolve().parent))
INI = HOME / "lf.ini"
LOG = HOME / "logs" / "lf.log"
BAUDS = [9600, 57600, 115200]


def _cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(INI, encoding="utf-8")
    if not cfg.has_section("COM"):
        cfg["COM"] = {"Port": "COM3", "Baud": "9600"}
    return cfg


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def set_baud(value: int) -> None:
    if value not in BAUDS:
        raise SystemExit(f"ERR unsupported baud {value}")
    cfg = _cfg()
    cfg["COM"]["Baud"] = str(value)
    INI.parent.mkdir(parents=True, exist_ok=True)
    with INI.open("w", encoding="utf-8") as f:
        cfg.write(f)
    log(f"baud set to {value}")


def flash(path: str, step_delay: float = 0.3, on_step=None) -> int:
    p = Path(path)
    if not p.exists():
        log(f"ERROR image not found: {path}")
        return 2
    log(f"flash start {p.name} @ {_cfg()['COM']['Baud']}")
    for pct in (10, 30, 50, 70, 90, 100):
        time.sleep(step_delay)
        log(f"progress {pct}%")
        if on_step:
            on_step(pct)
    log("flash done OK")
    return 0


def cli(argv: list[str]) -> int:
    # Hand-rolled like many old tools: "/flag value", case-insensitive, values may start with "/".
    class A:
        help = version = silent = False
        baud = flash = None
    a, it = A(), iter(argv)
    for tok in it:
        t = tok.lower()
        if t in ("/?", "--help"):
            a.help = True
        elif t in ("/version", "--version"):
            a.version = True
        elif t in ("/silent", "--silent"):
            a.silent = True
        elif t in ("/baud", "--baud"):
            a.baud = int(next(it, "0"))
        elif t in ("/flash", "--flash"):
            a.flash = next(it, None)
        else:
            print(f"ERR unknown argument {tok}")
            return 1
    if a.help:
        print("LegacyFlasher batch mode\n  /baud <n>\n  /flash <image> [/silent]\n  /version\n  /?")
        return 0
    if a.version:
        print(VERSION)
        return 0
    rc = 0
    if a.baud is not None:
        set_baud(a.baud)
        print(f"OK baud={a.baud}")
    if a.flash:
        rc = flash(a.flash, on_step=None if a.silent else (lambda p: print(f"progress {p}%", flush=True)))
        print("OK flash" if rc == 0 else f"ERR rc={rc}")
    return rc


def gui() -> None:  # pragma: no cover - interactive
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"LegacyFlasher {VERSION}")
    root.geometry("640x420")
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Baud rate").grid(row=0, column=0, sticky="w")
    baud = ttk.Combobox(frm, values=BAUDS, state="readonly", name="cmbBaud")
    baud.set(_cfg()["COM"]["Baud"])
    baud.grid(row=0, column=1, sticky="we")
    baud.bind("<<ComboboxSelected>>", lambda e: set_baud(int(baud.get())))

    ttk.Label(frm, text="Firmware").grid(row=1, column=0, sticky="w")
    path = ttk.Entry(frm, name="txtPath")
    path.grid(row=1, column=1, sticky="we")

    txt = tk.Text(frm, height=14, name="txtLog")
    txt.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=8)

    def refresh_log():
        txt.delete("1.0", "end")
        if LOG.exists():
            txt.insert("end", LOG.read_text(encoding="utf-8")[-4000:])
        txt.see("end")

    def do_flash():
        btn.state(["disabled"])
        root.update()
        flash(path.get(), on_step=lambda p: (refresh_log(), root.update()))
        btn.state(["!disabled"])
        refresh_log()

    btn = ttk.Button(frm, text="Flash", command=do_flash, name="btnFlash")
    btn.grid(row=2, column=1, sticky="e")
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(3, weight=1)
    refresh_log()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    gui()
