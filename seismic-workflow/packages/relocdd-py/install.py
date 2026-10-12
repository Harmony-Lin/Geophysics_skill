#!/usr/bin/env python3
"""Install relocDD-py at install time (like GaMMA), instead of vendoring it.

Upstream https://github.com/katie-biegel/relocDD-py ships neither ``setup.py``
nor ``pyproject.toml``, so ``pip install git+https://...`` cannot be used
directly.  This script clones the pinned revision of the upstream repository,
drops the hand-written ``pyproject.toml`` (next to this file) into the checkout
and runs ``pip install -e`` on it.  Afterwards ``import ph2dt`` /
``import hypoDD`` resolve from any working directory.

Usage:
    python seismic-workflow/packages/relocdd-py/install.py [--dir DIR] [--ref REF]

Re-running is safe: an existing checkout is updated and reinstalled.
See docs/install.md for details and for the equivalent manual commands.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM = "https://github.com/katie-biegel/relocDD-py.git"
# Upstream tags no releases; pin the revision this pipeline is tested against.
PINNED_REF = "323071ac720ee0e5423592ede50be87bb8944db1"
DEFAULT_DEST = HERE.parent.parent / ".cache" / "relocdd-py"


def _log(msg: str) -> None:
    print(f"[relocdd-py] {msg}", file=sys.stderr)


def _run(cmd: list[str]) -> None:
    _log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def fetch(dest: Path, ref: str) -> None:
    """Clone (or update) `ref` of the upstream repository into `dest`."""
    if shutil.which("git") is None:
        sys.exit(f"[ERROR] git not found -- clone {UPSTREAM} manually, then copy "
                 f"{HERE / 'pyproject.toml'} into it and run "
                 f"'pip install -e <checkout>'")
    if (dest / ".git").is_dir():
        _log(f"updating existing checkout {dest}")
        _run(["git", "-C", str(dest), "fetch", "--quiet", "origin"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--quiet", UPSTREAM, str(dest)])
    _run(["git", "-C", str(dest), "checkout", "--quiet", ref])


def install(dest: Path) -> None:
    """Install the checkout as an editable package (after adding metadata)."""
    shutil.copy2(HERE / "pyproject.toml", dest / "pyproject.toml")
    _run([sys.executable, "-m", "pip", "install", "-e", str(dest)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=str(DEFAULT_DEST),
                        help=f"checkout directory (default: {DEFAULT_DEST})")
    parser.add_argument("--ref", default=PINNED_REF,
                        help="git revision to check out (default: pinned commit)")
    parser.add_argument("--prepare-only", action="store_true",
                        help="clone + drop in pyproject.toml, skip pip install")
    args = parser.parse_args()

    dest = Path(args.dir).expanduser().resolve()
    fetch(dest, args.ref)
    if args.prepare_only:
        shutil.copy2(HERE / "pyproject.toml", dest / "pyproject.toml")
        _log(f"checkout ready at {dest} (pip install skipped)")
        return
    install(dest)
    _log("relocDD-py installed -- 'import ph2dt' / 'import hypoDD' now work")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[ERROR] command failed ({exc.returncode}): {' '.join(exc.cmd)}")
