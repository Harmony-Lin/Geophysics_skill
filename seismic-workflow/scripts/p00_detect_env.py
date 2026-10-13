#!/usr/bin/env python3
"""Phase 0: environment detection for the seismic workflow.

Reports available packages and SeisBench model cache status as structured
JSON on stdout.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, log, run_main  # noqa: E402


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _pygmt_works() -> bool:
    if not _has_module("pygmt"):
        return False
    try:
        import pygmt  # noqa: F401
        return True
    except Exception:
        return False


def _seisbench_cache() -> dict:
    cache = Path.home() / ".cache" / "seisbench"
    models = []
    if cache.is_dir():
        models = [d.name for d in cache.iterdir() if d.is_dir()]
    return {"path": str(cache), "exists": cache.is_dir(), "models": models}


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect environment capabilities")
    parser.parse_args()

    log("Probing Python packages...")
    packages = {
        "obspy": _has_module("obspy"),
        "seisbench": _has_module("seisbench"),
        "torch": _has_module("torch"),
        "gmma": _has_module("gmma"),
        "pygmt": _pygmt_works(),
        "cartopy": _has_module("cartopy"),
        "matplotlib": _has_module("matplotlib"),
        "pandas": _has_module("pandas"),
        "scipy": _has_module("scipy"),
        "numpy": _has_module("numpy"),
    }

    log("Probing location backends (relocDD-py)...")
    # relocDD-py is installed from git (packages/relocdd-py/install.py)
    relocdd_ok = _has_module("ph2dt") and _has_module("hypoDD")
    backends = {
        "hypodd": relocdd_ok,
    }

    log("Checking SeisBench model cache...")
    seisbench_cache = _seisbench_cache()

    torch_device = "cpu"
    if packages["torch"]:
        try:
            import torch
            torch_device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            pass

    missing_required = [k for k in ("obspy", "seisbench", "numpy", "pandas")
                        if not packages[k]]

    emit({
        "status": "ok",
        "packages": packages,
        "backends": backends,
        "seisbench_cache": seisbench_cache,
        "torch_device": torch_device,
        "missing_required": missing_required,
        "ready": len(missing_required) == 0,
    })


if __name__ == "__main__":
    run_main(main)
