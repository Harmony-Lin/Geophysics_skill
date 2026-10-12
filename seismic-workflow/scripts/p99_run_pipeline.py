#!/usr/bin/env python3
"""One-shot pipeline runner: chains P2-P8 with checkpoint validation.

Stops at the first failing phase and emits a JSON summary of completed
stages. Designed for deterministic execution without LLM mid-decisions.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (emit, ensure_dir, fail, log, run_main,  # noqa: E402
                    save_stations_csv, stations_from_xml, warn)

SCRIPTS = Path(__file__).parent


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


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
    parser.add_argument("--skip", default="none",
                        choices=["none", "hypodd", "locate"],
                        help="skip relative location or all locating")
    parser.add_argument("--stations-dir",
                        help="directory holding stations.xml + stations.csv "
                             "(default: <workdir>/stations)")
    parser.add_argument("--no-stations", action="store_true",
                        help="run without instrument responses: skips Phase 2 "
                             "response removal and Phase 7 ML (the catalog "
                             "keeps its upstream magnitude, if any). Without "
                             "responses ML cannot be computed, so this mode "
                             "is for picking/locating only.")
    args = parser.parse_args()

    w = args.workdir
    results = {}

    # ── 台站元数据：P2 去响应与 P7 计算 ML 都强依赖响应文件 ──
    stations_dir = Path(args.stations_dir or f"{w}/stations")
    stations_xml = stations_dir / "stations.xml"
    stations_csv = stations_dir / "stations.csv"
    stations_xml_opt = None
    if not stations_xml.exists():
        if not args.no_stations:
            fail(f"StationXML not found: {stations_xml}; Phase 2 (response "
                 "removal) and Phase 7 (ML magnitude) cannot run without "
                 "instrument responses. Fetch them (e.g. "
                 "tests/fetch_templeton.py), pass --stations-dir pointing at a "
                 "directory containing stations.xml, or pass --no-stations to "
                 "skip magnitude computation entirely.")
            return
        warn(f"StationXML not found ({stations_xml}) and --no-stations given: "
             "response removal and ML are skipped")
    else:
        stations_xml_opt = str(stations_xml)
        if not stations_csv.exists():
            # 关联/定位阶段需要台站坐标表；可直接由 StationXML 导出，避免手工准备
            save_stations_csv(stations_from_xml(stations_xml), stations_csv)
            log(f"Derived {stations_csv} from {stations_xml}")

    if not stations_csv.exists() and args.no_stations:
        fail(f"station coordinates not found: {stations_csv}; Phase 4 "
             "(association) and Phase 5 (location) need them even in "
             "--no-stations mode")
        return
    stations_csv = str(stations_csv)

    # Phase 2 — remove instrument response so later phases see physical units
    preprocess_cmd = [
        sys.executable, str(SCRIPTS / "p02_preprocess.py"),
        "--input", args.input, "--output", f"{w}/preprocessed",
        "--filter", "0.5-20", "--resample", "100"]
    if stations_xml_opt:
        preprocess_cmd += ["--stations", stations_xml_opt, "--remove-response"]
    results["preprocess"] = run_step("preprocess", preprocess_cmd)

    # Phase 3
    results["pick"] = run_step("pick", [
        sys.executable, str(SCRIPTS / "p03_pick_phases.py"),
        "--input", f"{w}/preprocessed",
        "--output", f"{w}/picks/phasenet_picks.csv"])

    # Phase 4
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

        # Phase 6 — optional (relocDD-py, installed from git at install time)
        relocdd_ok = _has_module("ph2dt") and _has_module("hypoDD")
        if args.skip != "hypodd" and relocdd_ok:
            results["hypodd"] = run_step("hypodd", [
                sys.executable, str(SCRIPTS / "p06_run_hypodd.py"),
                "--catalog", final_catalog,
                "--waveforms", f"{w}/preprocessed",
                "--picks", f"{w}/picks/phasenet_picks.csv",
                "--stations", stations_csv,
                "--output-dir", f"{w}/locate/hypodd"])
            dd_catalog = Path(f"{w}/locate/hypodd/catalog_dd.csv")
            if dd_catalog.exists():
                final_catalog = str(dd_catalog)
    else:
        final_catalog = f"{w}/assoc/events.csv"

    # Phase 7 — --assume-units VEL 必须与 Phase 2 的 output="VEL" 对应；
    # MSEED 不保存 units，只能显式声明
    magnitude_cmd = [
        sys.executable, str(SCRIPTS / "p07_compute_magnitude.py"),
        "--catalog", final_catalog,
        "--waveforms", f"{w}/preprocessed",
        "--output", f"{w}/locate/catalog_final.csv"]
    if stations_xml_opt:
        magnitude_cmd += ["--stations", stations_xml_opt, "--assume-units", "VEL"]
    # 无响应文件时不给 --stations，p07 会输出 status=warning +
    # magnitude_available=false（明确表示"没有震级"），而不是拿 counts
    # 当作物理量算出一个错误的震级。
    results["magnitude"] = run_step("magnitude", magnitude_cmd)

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
