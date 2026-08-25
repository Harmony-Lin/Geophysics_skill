#!/usr/bin/env python3
"""Phase 5 (backend A) shell wrapper — thin shim that calls the Python
wrapper run_nll_locate.py. Kept for SKILL.md interface compatibility."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PY = sys.executable or "python3"
SCRIPT = Path(__file__).parent / "run_nll_locate.py"

if __name__ == "__main__":
    cmd = [PY, str(SCRIPT)] + sys.argv[1:]
    raise SystemExit(subprocess.call(cmd))
