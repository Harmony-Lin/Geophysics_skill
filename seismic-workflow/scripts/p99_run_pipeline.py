#!/usr/bin/env python3
"""One-shot pipeline runner: chains P2-P8 with checkpoint validation.

Stops at the first failing phase and emits a JSON summary of completed
stages. Designed for deterministic execution without LLM mid-decisions.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, log, run_main, warn  # noqa: E402

SCRIPTS = Path(__file__).parent


def parse_last_json(text: str) -> dict:
    """Extract the last complete JSON object from mixed stdout.

    Some libraries (e.g. GaMMA) print progress lines to stdout, polluting
    the JSON contract; find the OUTERMOST/LONGEST {...} block that parses.
    """
    if not text or not text.strip():
        return {}
    decoder = json.JSONDecoder()
    candidates = []
    
    # Find all valid JSON objects starting from each "{" position
    for idx in range(len(text)):
        if text[idx] != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[idx:])
            # Track (position, length, object) to find the outermost one
            obj_text = text[idx:idx + end]
            candidates.append((idx, end, obj))
        except json.JSONDecodeError:
            continue
    
    if not candidates:
        return {}
    
    # Return the object with the longest span (outermost/topmost)
    return max(candidates, key=lambda x: x[1])[2]


def run_step(name: str, cmd: list[str]) -> dict:
    log(f"=== Phase {name}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (Path("data") / "pipeline_logs").mkdir(parents=True, exist_ok=True)
    (Path("data") / "pipeline_logs" / f"{name}.log").write_text(
        (proc.stdout or "") + "\n---STDERR---\n" + (proc.stderr or ""))
    result = parse_last_json(proc.stdout)
    if not result:
        result = {"status": "error", "error": "non-JSON stdout",
                  "stdout_tail": (proc.stdout or "")[-1000:]}
    if proc.returncode != 0 or result.get("status") not in ("ok", "warning"):
        result.setdefault("status", "error")
        result["phase"] = name
        result["failed_phase"] = name
        result["completed"] = COMPLETED
        emit(result, exit_code=1)
    COMPLETED.append(name)
    return result


COMPLETED: list[str] = []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full seismic workflow pipeline (P2-P8)")
    parser.add_argument("--input", default="data/raw", help="raw waveform dir")
    parser.add_argument("--workdir", default="data", help="data workdir")
    parser.add_argument("--skip", default="hypodd",
                        choices=["none", "hypodd", "locate"],
                        help="skip relative location or all locating")
    args = parser.parse_args()

    w = args.workdir
    results = {}

    # Phase 2 — remove instrument response so later phases see physical units
    stations_xml = f"{w}/stations/stations.xml"
    results["preprocess"] = run_step("preprocess", [
        sys.executable, str(SCRIPTS / "p02_preprocess.py"),
        "--input", args.input, "--output", f"{w}/preprocessed",
        "--stations", stations_xml, "--remove-response",
        "--filter", "0.5-20", "--resample", "100"])

    # Phase 3
    results["pick"] = run_step("pick", [
        sys.executable, str(SCRIPTS / "p03_pick_phases.py"),
        "--input", f"{w}/preprocessed",
        "--output", f"{w}/picks/phasenet_picks.csv"])

    # Phase 4
    stations_csv = f"{w}/stations/stations.csv"
    results["associate"] = run_step("associate", [
        sys.executable, str(SCRIPTS / "p04_run_association.py"),
        "--picks", f"{w}/picks/phasenet_picks.csv",
        "--stations", stations_csv,
        "--output", f"{w}/assoc/events.csv"])

    if args.skip != "locate":
        # Phase 5 — pluggable backend
        import shutil
        if shutil.which("NLLoc"):
            results["locate"] = run_step("locate", [
                str(SCRIPTS / "p05_run_nll_locate.sh"),
                "--events", f"{w}/assoc/events.csv",
                "--picks", f"{w}/picks/phasenet_picks.csv",
                "--stations", stations_csv,
                "--output-dir", f"{w}/locate/nll"])
            final_catalog = f"{w}/locate/nll/catalog_abs.csv"
        else:
            results["locate"] = run_step("locate", [
                sys.executable, str(SCRIPTS / "p05_grid_locate.py"),
                "--events", f"{w}/assoc/events.csv",
                "--picks", f"{w}/picks/phasenet_picks.csv",
                "--stations", stations_csv,
                "--output", f"{w}/locate/catalog_abs.csv"])
            final_catalog = f"{w}/locate/catalog_abs.csv"

        # Phase 6 — optional
        if args.skip != "hypodd" and shutil.which("hypoDD"):
            results["hypodd"] = run_step("hypodd", [
                str(SCRIPTS / "p06_run_hypodd.sh"),
                "--catalog", final_catalog,
                "--waveforms", f"{w}/preprocessed",
                "--stations", stations_csv,
                "--output-dir", f"{w}/locate/hypodd"])
            dd_catalog = Path(f"{w}/locate/hypodd/catalog_dd.csv")
            if dd_catalog.exists():
                final_catalog = str(dd_catalog)
    else:
        final_catalog = f"{w}/assoc/events.csv"

    # Phase 7
    results["magnitude"] = run_step("magnitude", [
        sys.executable, str(SCRIPTS / "p07_compute_magnitude.py"),
        "--catalog", final_catalog,
        "--waveforms", f"{w}/preprocessed",
        "--stations", f"{w}/stations/stations.xml",
        "--output", f"{w}/locate/catalog_final.csv"])

    # Phase 8
    results["qc"] = run_step("qc", [
        sys.executable, str(SCRIPTS / "p08_qc_and_plot.py"),
        "--catalog", f"{w}/locate/catalog_final.csv",
        "--picks", f"{w}/picks/phasenet_picks.csv",
        "--waveforms", f"{w}/preprocessed",
        "--stations", stations_csv,
        "--output-dir", f"{w}/qc"])

    emit({
        "status": "ok",
        "completed_phases": COMPLETED,
        "final_catalog": f"{w}/locate/catalog_final.csv",
        "report": f"{w}/qc/report.md",
        "results": {k: {kk: vv for kk, vv in v.items() if kk != "status"}
                    for k, v in results.items()},
    })


if __name__ == "__main__":
    run_main(main)
