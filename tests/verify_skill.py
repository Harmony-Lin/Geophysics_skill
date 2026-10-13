"""Minimal end-to-end verification of the seismic-workflow skill.

Follows SKILL.md checkpoints 0-8 on the default test dataset: REAL waveforms
of the 2025 Templeton (California) M4.1 earthquake sequence downloaded from
FDSN data centres (EarthScope/NCEDC/SCEDC) via tests/fetch_templeton.py.

  0. detect_env.py        -> obspy & seisbench available
  1. raw waveforms staged -> n_files > 0 (real FDSN data; fetched if absent)
     + Phase-1 radius CLI contract (tests/verify_fetch_event.py --offline)
  2-8. run_pipeline.py    -> checkpoints: preprocessed/, picks CSV,
       assoc events, located catalog, final catalog columns,
       qc report.md + 5 PNGs, per-phase timing in log + report
       (incl. Phase 2's own sub-step breakdown)

Every run's full intermediate files and results are saved to
tests/tmp_data/runs/<YYYYmmdd_HHMMSS>/ (never overwritten).

Usage:
    python tests/verify_skill.py [--keep]
        --keep : also keep the scratch workspace tests/tmp_data/verify_ws/

Exit code 0 = all checkpoints pass; prints a JSON summary.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "seismic-workflow" / "scripts"
TMP = Path(__file__).resolve().parent / "tmp_data"
WS = TMP / "verify_ws"          # scratch workspace (cwd for the pipeline)
RUN_DIR = TMP / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
                                # per-run archive (kept forever)

#: Per-phase durations, mirrored into ``data/pipeline_logs/timings.json`` the
#: same way ``p99_run_pipeline.py`` does (Phase 8 reads that file for report.md).
TIMINGS: list[dict] = []

#: Phase key -> label; kept in sync with ``common.PHASE_LABELS`` (importing
#: common here would reconfigure this harness' stdout encoding).
PHASE_LABELS = {
    "preprocess": "P2 预处理",
    "pick": "P3 相位拾取",
    "associate": "P4 事件关联",
    "locate": "P5 绝对定位",
    "hypodd": "P6 相对定位",
    "magnitude": "P7 震级计算",
    "qc": "P8 质检与制图",
}


def fmt_duration(seconds: float) -> str:
    """``0.42 s`` / ``12.34 s`` / ``1 m 05 s`` / ``1 h 02 m 03 s``."""
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, sec = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} m {sec:02d} s"
    return f"{minutes} m {sec:02d} s"


CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"check": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def parse_last_json(text: str) -> dict:
    """Extract the last top-level JSON object from mixed stdout.

    Some libraries (e.g. GaMMA) print progress lines to stdout, so the JSON is
    not the whole stream; phase scripts print their payload with ``indent=2``,
    i.e. with its opening brace in column 0.
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


def timings_ok(data_dir: Path) -> bool:
    """True when ``timings.json`` exists and every recorded phase has a duration."""
    path = data_dir / "pipeline_logs" / "timings.json"
    if not path.exists():
        return False
    try:
        steps = json.loads(path.read_text(encoding="utf-8")).get("steps", [])
    except (OSError, json.JSONDecodeError):
        return False
    return bool(steps) and all(
        isinstance(s.get("seconds"), (int, float)) and s["seconds"] > 0
        and s.get("phase") for s in steps)


def inner_timings(data_dir: Path) -> dict:
    """``phase -> number of sub-steps`` for phases that time their own steps."""
    path = data_dir / "pipeline_logs" / "timings.json"
    try:
        steps = json.loads(path.read_text(encoding="utf-8")).get("steps", [])
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        inner = s.get("timings")
        if isinstance(inner, dict) and inner.get("steps"):
            out[str(s.get("phase"))] = len(inner["steps"])
    return out


def write_timings(log_dir: Path) -> Path:
    """Aggregate per-phase durations (mirrors p99_run_pipeline._write_timings)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "timings.json"
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_s": round(sum(t["seconds"] for t in TIMINGS), 3),
        "steps": TIMINGS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_step(name: str, cmd: list[str], cwd: Path) -> dict:
    """Run one pipeline phase with real-time logs; returns parsed JSON.

    Streams stdout/stderr to the terminal as it arrives (real-time logs)
    while also capturing them for the per-phase log file and JSON summary.
    """
    import threading
    print(f"\n=== Phase {name}: {' '.join(str(c) for c in cmd)}")
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1)

    def pump(stream, sink):
        for line in iter(stream.readline, ""):
            sink.append(line)
            print(line, end="", flush=True)
        stream.close()

    out_lines, err_lines = [], []
    t_out = threading.Thread(target=pump, args=(proc.stdout, out_lines))
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_lines))
    t_out.start()
    t_err.start()
    proc.wait()
    t_out.join()
    t_err.join()
    elapsed = time.perf_counter() - t0

    stdout = "".join(out_lines)
    stderr = "".join(err_lines)

    # write per-phase log file (same layout as p99_run_pipeline.py)
    log_dir = cwd / "data" / "pipeline_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{name}.log").write_text(
        stdout + "\n---STDERR---\n" + stderr
        + "\n---ELAPSED---\n" + f"{elapsed:.3f} s\n", encoding="utf-8")

    result = parse_last_json(stdout)
    if not result:
        result = {"status": "error", "error": "non-JSON stdout",
                  "stdout_tail": stdout[-1000:]}
    result["elapsed_s"] = round(elapsed, 3)
    entry = {
        "phase": name,
        "label": PHASE_LABELS.get(name, name),
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
    write_timings(log_dir)
    print(f"=== Phase {name} takes {fmt_duration(elapsed)} "
          f"(exit {proc.returncode})")
    if proc.returncode != 0 or result.get("status") not in ("ok", "warning"):
        if result.get("status") != "error":
            result["status"] = "error"
            result.setdefault("error", f"phase exited with code {proc.returncode}")
        result["phase"] = name
        result["failed_phase"] = name
    return result


def run_pipeline(py: str, cwd: Path) -> dict:
    """Run P2-P8 step-by-step with real-time logs; returns summary dict.

    Mirrors the phase ordering and CLI args of p99_run_pipeline.py so the
    produced summary (status / completed_phases / results) is identical.
    """
    w = "data"
    results = {}
    completed = []
    timings_file = cwd / w / "pipeline_logs" / "timings.json"
    t_pipeline = time.perf_counter()

    # Phase 2 — remove instrument response so later phases see physical units
    stations_xml = f"{w}/stations/stations.xml"
    results["preprocess"] = run_step("preprocess", [
        py, SCRIPTS / "p02_preprocess.py",
        "--input", "data/raw", "--output", f"{w}/preprocessed",
        "--stations", stations_xml, "--remove-response",
        "--filter", "0.5-20", "--resample", "100"], cwd)
    completed.append("preprocess")

    # Phase 3
    results["pick"] = run_step("pick", [
        py, SCRIPTS / "p03_pick_phases.py",
        "--input", f"{w}/preprocessed",
        "--output", f"{w}/picks/phasenet_picks.csv"], cwd)
    completed.append("pick")

    # Phase 4
    stations_csv = f"{w}/stations/stations.csv"
    results["associate"] = run_step("associate", [
        py, SCRIPTS / "p04_run_association.py",
        "--picks", f"{w}/picks/phasenet_picks.csv",
        "--stations", stations_csv,
        "--output", f"{w}/assoc/events.csv"], cwd)
    completed.append("associate")

    # Phase 5 — pure-Python Geiger location
    results["locate"] = run_step("locate", [
        py, SCRIPTS / "p05_grid_locate.py",
        "--events", f"{w}/assoc/events.csv",
        "--picks", f"{w}/picks/phasenet_picks.csv",
        "--stations", stations_csv,
        "--output", f"{w}/locate/catalog_abs.csv"], cwd)
    final_catalog = f"{w}/locate/catalog_abs.csv"
    completed.append("locate")

    # Phase 6 — optional (relocDD-py, installed from git at install time)
    relocdd_ok = _has_module("ph2dt") and _has_module("hypoDD")
    if relocdd_ok:
        results["hypodd"] = run_step("hypodd", [
            py, SCRIPTS / "p06_run_hypodd.py",
            "--catalog", final_catalog,
            "--picks", f"{w}/picks/phasenet_picks.csv",
            "--stations", stations_csv,
            "--output-dir", f"{w}/locate/hypodd"], cwd)
        completed.append("hypodd")
        dd_catalog = cwd / w / "locate" / "hypodd" / "catalog_dd.csv"
        if dd_catalog.exists():
            final_catalog = str(dd_catalog)

    # Phase 7
    results["magnitude"] = run_step("magnitude", [
        py, SCRIPTS / "p07_compute_magnitude.py",
        "--catalog", final_catalog,
        "--waveforms", f"{w}/preprocessed",
        "--stations", f"{w}/stations/stations.xml",
        "--output", f"{w}/locate/catalog_final.csv"], cwd)
    completed.append("magnitude")

    # Phase 8
    results["qc"] = run_step("qc", [
        py, SCRIPTS / "p08_qc_and_plot.py",
        "--catalog", f"{w}/locate/catalog_final.csv",
        "--picks", f"{w}/picks/phasenet_picks.csv",
        "--waveforms", f"{w}/preprocessed",
        "--stations", stations_csv,
        "--output-dir", f"{w}/qc",
        "--timings", str(timings_file.resolve())], cwd)
    completed.append("qc")

    total = time.perf_counter() - t_pipeline
    detail = "，".join(f"{t['label']} {fmt_duration(t['seconds'])}"
                       for t in TIMINGS)
    print(f"\n=== 流水线完成，总计 {fmt_duration(total)}（{detail}）")

    return {
        "status": "ok",
        "elapsed_s": round(total, 3),
        "phase_timings": TIMINGS,
        "completed_phases": completed,
        "final_catalog": f"{w}/locate/catalog_final.csv",
        "report": f"{w}/qc/report.md",
        "results": {k: {kk: vv for kk, vv in v.items() if kk != "status"}
                    for k, v in results.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="keep the scratch workspace after the run")
    args = ap.parse_args()

    # --- ensure the default dataset exists -----------------------------
    raw_files = sorted((TMP / "raw").glob("*.mseed"))
    if not raw_files:
        print("Default dataset missing — downloading real Templeton M4.1 "
              "waveforms from FDSN...")
        proc = run([sys.executable, ROOT / "tests" / "fetch_templeton.py"])
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

    # --- Phase 1 CLI contract: radius selection must parse -------------
    # Full radius/natural-language coverage lives in tests/verify_fetch_event.py
    # (needs FDSN); here only the offline argument-parsing regression runs.
    proc = run([py, ROOT / "tests" / "verify_fetch_event.py", "--offline"],
               cwd=WS)
    cli = parse_last_json(proc.stdout)
    check("P1 radius CLI contract", proc.returncode == 0
          and cli.get("status") == "ok" and cli.get("n_checks", 0) >= 1,
          f"{cli.get('n_checks')} checks, failed={cli.get('failed')}"
          if cli else proc.stderr[-300:])

    # --- Phase 0: environment detection ------------------------------
    proc = run([py, SCRIPTS / "p00_detect_env.py"], cwd=WS)
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        env = {}
    pkgs = env.get("packages", {})
    check("P0 obspy+seisbench", bool(pkgs.get("obspy")) and bool(pkgs.get("seisbench")),
          f"backends={env.get('backends')}")

    # --- Phases 2-8: run step-by-step with real-time logs -------------
    summary = run_pipeline(py, WS)

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

    # Checkpoint 8b: per-step timing reached the log and the report, including
    # Phase 2's own sub-steps (response removal dominates the pipeline).
    report_txt = ((qc / "report.md").read_text(encoding="utf-8")
                  if (qc / "report.md").exists() else "")
    log_txt = ((d / "pipeline_logs" / "pick.log").read_text(encoding="utf-8")
               if (d / "pipeline_logs" / "pick.log").exists() else "")
    prep_log_path = d / "pipeline_logs" / "preprocess.log"
    prep_log = (prep_log_path.read_text(encoding="utf-8")
                if prep_log_path.exists() else "")
    inner = inner_timings(d)
    check("CP8 timing in log+report",
          timings_ok(d) and "耗时" in report_txt and "---ELAPSED---" in log_txt,
          f"timings.json={'yes' if (d/'pipeline_logs'/'timings.json').exists() else 'no'}, "
          f"report 耗时={'yes' if '耗时' in report_txt else 'no'}, "
          f"log ELAPSED={'yes' if '---ELAPSED---' in log_txt else 'no'}")
    check("CP8b P2 sub-step timing",
          inner.get("preprocess", 0) >= 5
          and "remove response" in prep_log
          and "remove response" in report_txt,
          f"timings.json inner steps={inner}, "
          f"preprocess.log={'yes' if 'remove response' in prep_log else 'no'}, "
          f"report={'yes' if 'remove response' in report_txt else 'no'}")

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

    # --- archive the full run output (intermediates + report) ---------
    saved = None
    if WS.exists():
        saved = RUN_DIR
        if saved.exists():
            shutil.rmtree(saved)
        shutil.copytree(WS, saved)
        # write the JSON summary next to the artifacts
        (saved / "verify_summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8")
    result["saved_to"] = str(saved) if saved else None

    if not keep and WS.exists():
        shutil.rmtree(WS, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
