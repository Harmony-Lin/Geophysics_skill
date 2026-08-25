#!/usr/bin/env python3
"""Phase 6 (backend A) shell wrapper — thin shim calling run_hypodd.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PY = sys.executable or "python3"
SCRIPT = Path(__file__).parent / "run_hypodd.py"

if __name__ == "__main__":
    raise SystemExit(subprocess.call([PY, str(SCRIPT)] + sys.argv[1:]))
