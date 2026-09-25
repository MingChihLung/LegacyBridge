# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Run the legacy-bridge CLI against audacity.yaml:  python lbcli.py caps"""
import os
import sys
from pathlib import Path

os.environ.setdefault("LB_TRACE", str(Path(__file__).with_name("trace.jsonl")))
from legacy_bridge.cli import main  # noqa: E402

sys.exit(main(["--manifest", str(Path(__file__).with_name("audacity.yaml")), *sys.argv[1:]]))
