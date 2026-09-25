# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""Simulate a human leaving a modal dialog open (Generate > Tone...)."""
from legacy_bridge import load_manifest, SessionManager
s = SessionManager(load_manifest("audacity.yaml")).open()
s.backends["L2"].menu_select("Generate->Tone...")
print("opened")
