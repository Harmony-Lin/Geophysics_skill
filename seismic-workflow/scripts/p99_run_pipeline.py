#!/usr/bin/env python3
"""One-shot pipeline runner: chains P2-P8 with checkpoint validation.

Stops at the first failing phase and emits a JSON summary of completed
stages. Designed for deterministic execution without LLM mid-decisions.

Every phase is timed: the duration is logged to stderr, recorded inside the
phase JSON/log (``<phase>.log``) and aggregated into
``<workdir>/pipeline_logs/timings.json``, which Phase 8 renders into
``report.md``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (emit, fail, format_duration, log,  # noqa: E402
                    phase_label, run_main, save_stations_csv,
                    stations_from_xml, warn)

SCRIPTS = Path(__file__).parent
#: Rebound to ``<workdir>/pipeline_logs`` by ``main()``, so the logs land next
#: to the data they describe (Phase 8 looks for them there).
LOG_DIR = Path("data") / "pipeline_logs"
TIMINGS_FILE = LOG_DIR / "timings.json"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def parse_last_json(text: str) -> dict:
    """Return the last top-level JSON object printed by a phase script.

    Some libraries (e.g. GaMMA) print progress lines to stdout, so the JSON is
    not the whole stream.  ``common.emit`` prints with ``indent=2``, i.e. with
    the opening brace of the result object in column 0, which is what tells it
    apart from nested objects.
    """
    if not text or not text.strip():
        return {}
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"^\{", text, re.MULTILINE))):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def _write_timings() -> Path:
    """Aggregate phase durations into ``pipeline_logs/timings.json``.

    Phase 8 reads this file (``--timings``) to put the per-step breakdown into
    the QC report; writing it after every phase also keeps partial timings on
    disk when the pipeline aborts at a failing phase.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TIMINGS_FILE.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_s": round(sum(t["seconds"] for t in TIMINGS), 3),
        "steps": TIMINGS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return TIMINGS_FILE


def run_step(name: str, cmd: list[str]) -> dict:
    log(f"=== Phase {name}: {' '.join(cmd)}")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{name}.log").write_text(
        (proc.stdout or "") + "\n---STDERR---\n" + (proc.stderr or "")
        + "\n---ELAPSED---\n" + f"{elapsed:.3f} s\n", encoding="utf-8")
    result = parse_last_json(proc.stdout)
    if not result:
        result = {"status": "error", "error": "non-JSON stdout",
                  "stdout_tail": (proc.stdout or "")[-1000:]}
    result["elapsed_s"] = round(elapsed, 3)
    entry = {
        "phase": name,
        "label": phase_label(name),
        "seconds": round(elapsed, 3),
        "status": result.get("status", "ok"),
        "exit_code": proc.returncode,
        "command": " ".join(str(c) for c in cmd),
    }
    if isinstance(result.get("timings"), dict):
        # 阶段脚本自带的分步耗时（如 P2 的去响应/滤波），P8 会展开进报告
        entry["timings"] = result["timings"]
    if isinstance(result.get("response_cache"), dict):
        # 仪器响应缓存命中率：解释 P2 去响应耗时的关键数字
        entry["response_cache"] = result["response_cache"]
    TIMINGS.append(entry)
    _write_timings()
    if proc.returncode != 0 or result.get("status") not in ("ok", "warning"):
        log(f"=== Phase {name} 失败，耗时 {format_duration(elapsed)} "
            f"(exit {proc.returncode})")
        if result.get("status") != "error":
            result["status"] = "error"
            result.setdefault("error", f"phase exited with code {proc.returncode}")
        result["phase"] = name
        result["failed_phase"] = name
        result["completed"] = COMPLETED
        result["phase_timings"] = list(TIMINGS)
        emit(result, exit_code=1)
    COMPLETED.append(name)
    log(f"=== Phase {name} 完成，耗时 {format_duration(elapsed)} "
        f"(exit {proc.returncode})")
    return result


COMPLETED: list[str] = []

#: Per-phase durations in completion order (mirrored to timings.json).
TIMINGS: list[dict] = []


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

    t_pipeline = time.perf_counter()
    w = args.workdir
    results = {}
    global LOG_DIR, TIMINGS_FILE
    LOG_DIR = Path(w) / "pipeline_logs"
    TIMINGS_FILE = LOG_DIR / "timings.json"

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
        # Phase 5 — pure-Python Geiger location (see SKILL.md Phase 5)
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
    # 无响应文件时不给 --stations：p07 会输出 status=warning +
    # magnitude_available=false（明确表示"没有震级"），而不是拿 counts
    # 当作物理量算出一个错误的震级。
    results["magnitude"] = run_step("magnitude", magnitude_cmd)

    # Phase 8 (run_step already persisted P2-P7 timings to disk)
    results["qc"] = run_step("qc", [
        sys.executable, str(SCRIPTS / "p08_qc_and_plot.py"),
        "--catalog", f"{w}/locate/catalog_final.csv",
        "--picks", f"{w}/picks/phasenet_picks.csv",
        "--waveforms", f"{w}/preprocessed",
        "--stations", stations_csv,
        "--output-dir", f"{w}/qc",
        "--timings", str(TIMINGS_FILE.resolve())])

    total = time.perf_counter() - t_pipeline
    detail = "，".join(f"{t['label']} {format_duration(t['seconds'])}"
                       for t in TIMINGS)
    log(f"=== 流水线完成，总计 {format_duration(total)}（{detail}）")
    emit({
        "status": "ok",
        "elapsed_s": round(total, 3),
        "phase_timings": TIMINGS,
        "completed_phases": COMPLETED,
        "final_catalog": f"{w}/locate/catalog_final.csv",
        "report": f"{w}/qc/report.md",
        "results": {k: {kk: vv for kk, vv in v.items() if kk != "status"}
                    for k, v in results.items()},
    })


if __name__ == "__main__":
    run_main(main)
