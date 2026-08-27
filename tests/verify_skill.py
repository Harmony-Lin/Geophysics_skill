"""Minimal end-to-end verification of the seismic-workflow skill.

Follows SKILL.md checkpoints 0-8 on the default test dataset: REAL waveforms
of the 2024 Salcha (Alaska) M5.3 earthquake sequence downloaded from an FDSN
data center (EarthScope/IRIS) via tests/fetch_salcha.py.

  0. detect_env.py        -> obspy & seisbench available
  1. raw waveforms staged -> n_files > 0 (real FDSN data; fetched if absent)
  2-8. run_pipeline.py    -> checkpoints: preprocessed/, picks CSV,
       assoc events, located catalog, final catalog columns,
       qc report.md + 5 PNGs

Usage:
    python tests/verify_skill.py [--keep] [--synthetic]
        --synthetic : use offline synthetic fallback instead of real FDSN data

Exit code 0 = all checkpoints pass; prints a JSON summary.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "seismic-workflow" / "scripts"
TMP = Path(__file__).resolve().parent / "tmp_data"
WS = TMP / "verify_ws"          # scratch workspace (cwd for the pipeline)

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"check": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="keep the scratch workspace after the run")
    ap.add_argument("--synthetic", action="store_true",
                    help="use synthetic fallback data instead of real FDSN "
                         "download (offline mode)")
    args = ap.parse_args()

    # --- ensure the default dataset exists -----------------------------
    raw_files = sorted((TMP / "raw").glob("*.mseed"))
    if not raw_files or args.synthetic:
        if args.synthetic:
            print("Using synthetic fallback dataset...")
            run([sys.executable, ROOT / "tests" / "make_synthetic.py"])
            # regenerate stations.csv from the synthetic inventory
            run([sys.executable,
                 SCRIPTS / "fetch_waveforms.py",
                 "--xml-to-csv", TMP / "stations.xml",
                 "--out", TMP / "stations" / "stations.csv"])
        else:
            print("Default dataset missing — downloading real Salcha "
                  "waveforms from FDSN...")
            proc = run([sys.executable, ROOT / "tests" / "fetch_salcha.py"])
            if proc.returncode != 0:
                check("P1 FDSN fetch", False,
                      f"download failed:\n{proc.stderr[-600:]}")
                return finish(args.keep)
        raw_files = sorted((TMP / "raw").glob("*.mseed"))

    # --- fresh scratch workspace -------------------------------------
    if WS.exists():
        shutil.rmtree(WS)
    (WS / "data" / "raw").mkdir(parents=True)
    (WS / "data" / "stations").mkdir(parents=True)

    # Checkpoint 1: stage raw waveforms + station metadata
    for f in raw_files:
        shutil.copy2(f, WS / "data" / "raw")
    shutil.copy2(TMP / "stations.xml", WS / "data" / "stations" / "stations.xml")
    stations_csv = WS / "data" / "stations" / "stations.csv"
    if (TMP / "stations" / "stations.csv").exists():
        shutil.copy2(TMP / "stations" / "stations.csv", stations_csv)
    check("P1 raw staged", len(raw_files) > 0 and all(
        f.stat().st_size > 0 for f in raw_files),
        f"{len(raw_files)} miniSEED files")

    py = sys.executable

    # --- Phase 0: environment detection ------------------------------
    proc = run([py, SCRIPTS / "detect_env.py"], cwd=WS)
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        env = {}
    pkgs = env.get("packages", {})
    check("P0 obspy+seisbench", bool(pkgs.get("obspy")) and bool(pkgs.get("seisbench")),
          f"backends={env.get('backends')}")

    # --- Phases 2-8: one-shot pipeline --------------------------------
    proc = run([py, SCRIPTS / "run_pipeline.py",
                "--input", "data/raw", "--workdir", "data",
                "--skip", "hypodd"], cwd=WS)
    summary = {}
    try:
        summary = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log = WS / "data" / "pipeline_logs"
        tail = ""
        if log.exists():
            fails = sorted(log.glob("*.log"))
            if fails:
                tail = fails[-1].read_text(encoding="utf-8", errors="replace")[-800:]
        check("pipeline completed", False, f"non-JSON stdout; last log tail:\n{tail}")
        return finish(args.keep)

    check("pipeline status ok", summary.get("status") == "ok",
          f"completed={summary.get('completed_phases')}")

    d = WS / "data"

    # Checkpoint 2: preprocessed non-empty
    prep = list((d / "preprocessed").glob("*.mseed"))
    check("CP2 preprocessed", len(prep) > 0, f"{len(prep)} files")

    # Checkpoint 3: picks csv rows == n_picks + header
    picks_csv = d / "picks" / "phasenet_picks.csv"
    n_picks = summary.get("results", {}).get("pick", {}).get("n_picks")
    rows = sum(1 for _ in open(picks_csv, encoding="utf-8")) - 1 if picks_csv.exists() else 0
    ok3 = picks_csv.exists() and rows > 0 and (
        n_picks is None or abs(rows - n_picks) <= 1)
    check("CP3 picks", ok3, f"{rows} picks (json n_picks={n_picks})")

    # Checkpoint 4: assoc events consistent
    assoc_csv = d / "assoc" / "events.csv"
    n_events = summary.get("results", {}).get("associate", {}).get("n_events")
    ev_rows = sum(1 for _ in open(assoc_csv, encoding="utf-8")) - 1 if assoc_csv.exists() else 0
    check("CP4 association", assoc_csv.exists() and ev_rows >= 1 and (
        n_events is None or ev_rows == n_events),
        f"{ev_rows} events (json n_events={n_events})")

    # Checkpoint 5: absolute location catalog non-empty
    abs_csv = d / "locate" / "catalog_abs.csv"
    loc_rows = sum(1 for _ in open(abs_csv, encoding="utf-8")) - 1 if abs_csv.exists() else 0
    check("CP5 located >=50%", abs_csv.exists()
          and loc_rows >= 0.5 * max(ev_rows, 1), f"{loc_rows}/{ev_rows} located")

    # Checkpoint 7: final catalog has required columns
    final_csv = d / "locate" / "catalog_final.csv"
    cols = []
    if final_csv.exists():
        with open(final_csv, encoding="utf-8") as fh:
            cols = fh.readline().strip().lower().split(",")
    colset = set(cols)
    have = {"latitude", "longitude", "depth_km"}.issubset(colset) or \
        {"lat", "lon", "depth"}.issubset(colset)
    # magnitude column should exist; ML value may be empty when no response removal
    mag_ok = any(c.startswith("magnitude") or c == "mag" for c in cols)
    check("CP7 final catalog", final_csv.exists() and have and mag_ok,
          f"columns={cols}")

    # Checkpoint 8: qc report + 5 figures (incl. pick_samples.png)
    qc = d / "qc"
    pngs = sorted(qc.glob("*.png"))
    check("CP8 qc artifacts", (qc / "report.md").exists() and len(pngs) >= 5,
          f"{len(pngs)} PNGs + report.md={'yes' if (qc/'report.md').exists() else 'no'}")

    return finish(args.keep)


def finish(keep: bool) -> int:
    n_fail = sum(1 for c in CHECKS if not c["ok"])
    result = {
        "status": "ok" if n_fail == 0 else "failed",
        "passed": len(CHECKS) - n_fail,
        "failed": n_fail,
        "checks": CHECKS,
        "workspace": str(WS) if keep else "(removed)",
    }
    if not keep and WS.exists():
        shutil.rmtree(WS, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
