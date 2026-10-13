#!/usr/bin/env python3
"""ML (Phase 7) regression test on REAL Templeton (California) data.

Why this test exists
--------------------
台站响应处理是 ML 计算中最主要的误差来源。历史版本用 Hutton & Boore (1987)
标定式，并把 WA "静态放大倍率" 2800 放进 paz 的 gain（sensitivity=1），对
nc75265691 只给出 ML 0.74 —— 公开目录值是 **ML 3.19**，偏低约 2.5 级，而
流水线依然报告 status=ok。本测试用真实 FDSN 波形 + 真实 StationXML 复现该
事件的 ML，任何回归（标定式、WA 增益、counts/速度/位移单位链、结果合并）
都会让它失败。

Dataset (real FDSN data, staged by tests/fetch_templeton.py)
    tests/tmp_data/raw/<waveform_file>          miniSEED waveforms
    tests/tmp_data/stations.xml                 StationXML with responses
    tests/tmp_data/stations/stations.csv        station coordinates
    tests/tmp_data/templeton_catalog.csv        published reference magnitudes

Chain exercised
    p02_preprocess.py --remove-response        counts -> m/s (真实仪器响应)
    p07_compute_magnitude.py --assume-units VEL  WA 仿真 + Bakun & Joyner (1984)

Checks
    CP1 p02 缺少 StationXML 时 fail-fast（不再"警告后静默跳过"）
    CP2 p02 逐 trace 去响应成功，并如实上报 response_removed / output_units
    CP3 p07 的 WA 极零点/增益/灵敏度与 obspy.signal.invsim.WOODANDERSON 一致
    CP4 p07 的 WA 仿真标定与 obspy 解析振幅因子一致（合成 1 Hz 正弦）
    CP5 p07 对该事件算出的 ML 落在公开目录值 ±tolerance 内
    CP6 p07 用自己的结果覆盖上游 magnitude，且 magnitude_type 非空

Usage
    python tests/verify_ml.py [--event 2] [--tolerance 0.5] [--keep]

Exit code 0 = all checks pass; prints a JSON summary.
Every run is archived to tests/tmp_data/runs/<YYYYmmdd_HHMMSS>_ml/.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "seismic-workflow" / "scripts"
TMP = Path(__file__).resolve().parent / "tmp_data"
RAW = TMP / "raw"
CATALOG = TMP / "templeton_catalog.csv"
STATIONS_XML = TMP / "stations.xml"
STATIONS_CSV = TMP / "stations" / "stations.csv"
WS = TMP / "ml_ws"                      # scratch workspace (kept if --keep)
RUN_DIR = TMP / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_ml")

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": str(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def run(cmd, **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def parse_last_json(text: str) -> dict:
    """Extract the last top-level JSON object from mixed stdout.

    Phase scripts print their result with ``indent=2``, so the opening brace of
    the payload starts a line; nested objects do not.
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


def phase(name: str, cmd, cwd: Path) -> dict:
    """Run one phase in cwd; archive its log; return the parsed JSON payload."""
    proc = run(cmd, cwd=str(cwd))
    log_dir = cwd / "data" / "pipeline_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{name}.log").write_text(
        proc.stdout + "\n---STDERR---\n" + proc.stderr, encoding="utf-8")
    result = parse_last_json(proc.stdout)
    if not result:
        result = {"status": "error", "error": "non-JSON stdout",
                  "stdout_tail": proc.stdout[-1500:],
                  "stderr_tail": proc.stderr[-1500:]}
    result["_returncode"] = proc.returncode
    return result


def load_p07():
    """Import the phase-7 module so its constants/helpers can be unit-checked."""
    spec = importlib.util.spec_from_file_location(
        "p07_compute_magnitude", SCRIPTS / "p07_compute_magnitude.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_row(event_index: int) -> dict:
    with open(CATALOG, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if int(row["event_index"]) == event_index:
                return row
    raise SystemExit(f"event_index={event_index} not in {CATALOG}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phase-7 ML regression test (real Templeton, CA data)")
    ap.add_argument("--event", type=int, default=2,
                    help="event_index in templeton_catalog.csv (default 2 = "
                         "nc75265691, the published ML anchor)")
    ap.add_argument("--tolerance", type=float, default=0.5,
                    help="allowed |ML - published| (default 0.5 magnitude units)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the scratch workspace tests/tmp_data/ml_ws/")
    args = ap.parse_args()

    for p in (RAW, CATALOG, STATIONS_XML, STATIONS_CSV):
        if not p.exists():
            print(f"[FAIL] missing test data: {p}\n"
                  "       run:  python tests/fetch_templeton.py")
            return 1

    eidx = args.event
    ref = read_row(eidx)
    mseed = RAW / ref["waveform_file"]
    published, ptype = float(ref["magnitude"]), ref["mag_type"].strip().lower()
    print(f"\n=== Reference event: {ref['event_id']}  "
          f"{ref['time']}  ({ref['latitude']}, {ref['longitude']})  "
          f"depth={ref['depth_km']} km  published M{published} {ptype}")

    # ── CP1: p02 must fail fast when the StationXML is missing ──────────
    if WS.exists():
        shutil.rmtree(WS)
    (WS / "raw").mkdir(parents=True)
    (WS / "stations").mkdir(parents=True)
    shutil.copy2(mseed, WS / "raw" / mseed.name)
    shutil.copy2(STATIONS_XML, WS / "stations" / "stations.xml")
    shutil.copy2(STATIONS_CSV, WS / "stations" / "stations.csv")
    stations_xml = WS / "stations" / "stations.xml"

    py = sys.executable
    bad = phase("cp1_no_inventory", [
        py, SCRIPTS / "p02_preprocess.py",
        "--input", "raw", "--output", "preprocessed_should_not_exist",
        "--stations", "stations/does_not_exist.xml", "--remove-response"], WS)
    check("CP1 p02 fail-fast on missing StationXML",
          bad.get("status") == "error" and not (WS / "preprocessed_should_not_exist").exists(),
          f"status={bad.get('status')} rc={bad.get('_returncode')} "
          f"error={str(bad.get('error'))[:90]}")

    # ── CP2: p02 real response removal (counts -> m/s) ─────────────────
    pre = phase("preprocess", [
        py, SCRIPTS / "p02_preprocess.py",
        "--input", "raw", "--output", "preprocessed",
        "--stations", "stations/stations.xml", "--remove-response",
        "--filter", "0.5-20", "--resample", "100"], WS)
    n_in = pre.get("n_traces_input") or 0
    n_rem = pre.get("n_traces_response_removed") or 0
    check("CP2 p02 succeeded", pre.get("status") in ("ok", "warning")
          and pre.get("_returncode") == 0, f"status={pre.get('status')}")
    check("CP2 response removed on every trace",
          pre.get("response_removed") is True and n_rem > 0 and n_rem == pre.get("n_traces_processed"),
          f"{n_rem}/{n_in} traces, output_units={pre.get('output_units')}")
    check("CP2 preprocessed file written",
          (WS / "preprocessed" / mseed.name).stat().st_size > 0
          if (WS / "preprocessed" / mseed.name).exists() else False,
          str(WS / "preprocessed" / mseed.name))

    # ── CP3: WA constants must match obspy's canonical Wood-Anderson ────
    p07 = load_p07()
    import numpy as np
    from obspy.signal.invsim import (WOODANDERSON,
                                     paz_2_amplitude_value_of_freq_resp)
    poles_ok = np.allclose(np.sort_complex(np.asarray(p07.WA_POLES)),
                           np.sort_complex(np.asarray(WOODANDERSON["poles"])))
    zeros_ok = np.allclose(np.sort_complex(np.asarray(p07.WA_ZEROS)),
                           np.sort_complex(np.asarray(WOODANDERSON["zeros"])))
    gain_ok = (float(p07.WA_GAIN) == float(WOODANDERSON["gain"])
               and float(p07.WA_SENSITIVITY) == float(WOODANDERSON["sensitivity"]))
    check("CP3 WA PAZ matches obspy WOODANDERSON", poles_ok and zeros_ok,
          f"poles={p07.WA_POLES} zeros={p07.WA_ZEROS}")
    check("CP3 WA gain/sensitivity match obspy",
          gain_ok, f"p07 gain={p07.WA_GAIN} sens={p07.WA_SENSITIVITY} "
                   f"obspy gain={WOODANDERSON['gain']} sens={WOODANDERSON['sensitivity']}")

    # ── CP4: WA simulation scaling == obspy analytic amplitude factor ───
    # 合成 1 Hz、零到峰 1.0 m/s 的速度正弦；p07 的 WA 仿真给出的振幅必须等于
    # obspy 的 |H_WA(f)| * sensitivity（estimate_magnitude 内部使用的因子）。
    from obspy import Stream, Trace
    fs, nsec = 100.0, 200.0
    t = np.arange(0.0, nsec, 1.0 / fs)
    tr = Trace(data=np.sin(2 * np.pi * 1.0 * t))
    tr.stats.sampling_rate = fs
    st = Stream([tr])
    st.simulate(paz_remove=None, paz_simulate={
        "poles": p07.WA_POLES, "zeros": p07.WA_ZEROS,
        "gain": p07.WA_GAIN, "sensitivity": p07.WA_SENSITIVITY})
    got = p07._wa_amplitude_mm(st[0])
    expect = (paz_2_amplitude_value_of_freq_resp(WOODANDERSON, 1.0)
              * WOODANDERSON["sensitivity"] * 1000.0)
    rel = abs(got - expect) / expect
    check("CP4 WA scaling matches obspy amplitude factor", rel < 0.01,
          f"p07={got:.1f} mm vs obspy={expect:.1f} mm (rel={rel:.2e})")

    # ── CP5/CP6: end-to-end ML for the reference event ─────────────────
    # 输入 catalog 故意带上公开的 magnitude / magnitude_type，
    # 用来验证 p07 会用自己算出的结果覆盖上游值（合并阶段的回归点）。
    cat_in = WS / "catalog_in.csv"
    with open(cat_in, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["event_id", "time", "latitude",
                                           "longitude", "depth_km",
                                           "magnitude", "magnitude_type"])
        w.writeheader()
        w.writerow({"event_id": ref["event_id"], "time": ref["time"],
                    "latitude": ref["latitude"], "longitude": ref["longitude"],
                    "depth_km": ref["depth_km"], "magnitude": published,
                    "magnitude_type": ptype})

    mag = phase("magnitude", [
        py, SCRIPTS / "p07_compute_magnitude.py",
        "--catalog", str(cat_in), "--waveforms", "preprocessed",
        "--stations", str(stations_xml),
        "--output", "catalog_final.csv", "--assume-units", "VEL"], WS)
    check("CP5 p07 succeeded", mag.get("status") in ("ok", "warning")
          and mag.get("_returncode") == 0, f"status={mag.get('status')}")
    check("CP5 p07 used Bakun & Joyner (1984)",
          "Bakun" in str(mag.get("ml_relation", "")), str(mag.get("ml_relation")))

    final_csv = WS / "catalog_final.csv"
    ml = None
    row_out = {}
    if final_csv.exists():
        with open(final_csv, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        row_out = rows[0] if rows else {}
        if row_out.get("magnitude") not in (None, ""):
            ml = float(row_out["magnitude"])

    # 公开目录中的 ML 值本身有 ±0.3 左右的不确定度，±0.5 是合理回归界限。
    # 历史缺陷版本在此处给出 0.74（偏低 2.45 级）。
    if ptype != "ml":
        print(f"[WARN] reference type is '{ptype.upper()}', not 'ml'; "
              "the ML anchor is only approximate for this event")
    check(f"CP5 ML within {args.tolerance} of published M{published} {ptype.upper()}",
          ml is not None and abs(ml - published) <= args.tolerance,
          f"ML={ml} vs published={published}  (delta="
          f"{None if ml is None else round(ml - published, 2)})")
    check("CP6 magnitude_type non-empty", str(row_out.get("magnitude_type", "")).strip() == "ML",
          f"magnitude_type={row_out.get('magnitude_type')!r}")
    check("CP6 computed ML overrides upstream magnitude",
          ml is not None and str(row_out.get("magnitude")) != str(published),
          f"input={published} output={row_out.get('magnitude')}")

    # ── archive the run ────────────────────────────────────────────────
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if final_csv.exists():
        shutil.copy2(final_csv, RUN_DIR / "catalog_final.csv")
    diag = WS / "catalog_final.magnitude_diag.csv"
    if diag.exists():
        shutil.copy2(diag, RUN_DIR / diag.name)

    if not args.keep:
        shutil.rmtree(WS, ignore_errors=True)

    ok = all(c["ok"] for c in CHECKS)
    summary = {"status": "ok" if ok else "error",
               "event": ref["event_id"], "published_magnitude": published,
               "published_type": ptype, "computed_magnitude": ml,
               "checks_passed": sum(c["ok"] for c in CHECKS),
               "checks_total": len(CHECKS), "checks": CHECKS,
               "run_dir": str(RUN_DIR)}
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "verify_ml_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "checks"},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
