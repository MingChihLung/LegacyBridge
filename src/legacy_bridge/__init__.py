# Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)
# SPDX-License-Identifier: MIT

"""legacy-bridge: expose a legacy tool to AI agents (L0-L3 + capability discovery)."""
from .manifest import Manifest, load_manifest
from .session import Session, SessionManager

__all__ = ["Manifest", "Session", "SessionManager", "load_manifest"]
__version__ = "0.1.0"
