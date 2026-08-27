#!/usr/bin/env python3
"""Phase 6 (backend A): HypoDD wrapper.

Pipeline: catalog + waveforms -> ph2dt (cross-correlation) -> hypoDD
(inversion) -> parse .reloc output -> CSV. LLM never touches native
HypoDD input formats.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, log, run_main, warn  # noqa: E402


def find_bins() -> tuple[str, str]:
    ph2dt = shutil.which("ph2dt")
    hypodd = shutil.which("hypoDD")
    if not (ph2dt and hypodd):
        fail("ph2dt/hypoDD binaries not found on PATH",
             hint="build HypoDD or use backend B: scripts/simple_dd.py")
    return ph2dt, hypodd


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6 backend A: HypoDD (ph2dt + hypoDD) wrapper")
    parser.add_argument("--catalog", required=True, help="absolute-located catalog CSV")
    parser.add_argument("--waveforms", required=True, help="preprocessed waveform dir")
    parser.add_argument("--stations", required=True, help="stations CSV")
    parser.add_argument("--inp-template", dest="inp_template",
                        default=str(Path(__file__).parent.parent / "assets" /
                                    "hypodd_inp_template.txt"))
    parser.add_argument("--output-dir", dest="output_dir", required=True)
    args = parser.parse_args()

    import pandas as pd

    ph2dt_bin, hypodd_bin = find_bins()
    workdir = ensure_dir(args.output_dir)

    # --- Step 1: ph2dt cross-correlation ---
    log("Step 1/3: running ph2dt cross-correlation...")
    proc = subprocess.run([ph2dt_bin], cwd=str(workdir),
                          capture_output=True, text=True, timeout=7200)
    (workdir / "ph2dt.log").write_text((proc.stdout or "") + (proc.stderr or ""))
    dt_ct = workdir / "dt.ct"
    if not dt_ct.exists():
        fail("ph2dt produced no dt.ct",
             hint="check waveform naming/station metadata; see "
                  "references/hypodd_workflow.md",
             stderr_tail=(proc.stderr or "")[-2000:])
        return

    # --- Step 2: prepare hypoDD input files ---
    log("Step 2/3: preparing hypoDD inputs...")
    inp_src = Path(args.inp_template)
    inp_dst = workdir / "hypoDD.inp"
    shutil.copy(inp_src, inp_dst)
    shutil.copy(dt_ct, workdir / "dt.ct")

    catalog = pd.read_csv(args.catalog)
    # station.dat: sta lat lon elev
    from common import load_stations
    stations = load_stations(args.stations)
    with open(workdir / "station.dat", "w") as fh:
        for _, s in stations.iterrows():
            code = str(s["station"]).split(".")[-1]
            fh.write(f"{code:<8}{s['latitude']:>10.4f}{s['longitude']:>10.4f}"
                     f" {s.get('elevation_m', 0.0) / 1000.0:.3f}\n")
    # event.dat: id lat lon depth
    with open(workdir / "event.dat", "w") as fh:
        for _, ev in catalog.iterrows():
            eid = str(ev["event_id"]).replace("ev", "").lstrip("0") or "0"
            fh.write(f"{eid:>8}{ev['latitude']:>11.4f}{ev['longitude']:>11.4f}"
                     f"{ev['depth_km']:>9.3f}\n")

    # --- Step 3: run hypoDD ---
    log("Step 3/3: running hypoDD inversion...")
    proc = subprocess.run([hypodd_bin, "hypoDD.inp"], cwd=str(workdir),
                          capture_output=True, text=True, timeout=7200)
    (workdir / "hypodd.log").write_text((proc.stdout or "") + (proc.stderr or ""))

    reloc = workdir / "hypoDD.reloc"
    if not reloc.exists():
        fail("hypoDD produced no .reloc output",
             stderr_tail=(proc.stderr or "")[-2000:])
        return

    rows = []
    for line in reloc.read_text().splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        eid = f"ev{int(parts[0]):06d}"
        rows.append({
            "event_id": eid,
            "latitude": float(parts[1]),
            "longitude": float(parts[2]),
            "depth_km": float(parts[3]),
            "rms_s": float(parts[9]) if len(parts) > 9 else "",
        })
    out = workdir / "catalog_dd.csv"
    pd.DataFrame(rows).to_csv(out, index=False)

    rms_before = float(catalog["rms_s"].fillna(0).mean() or 0)
    rms_after = sum(r["rms_s"] for r in rows if r["rms_s"] != "") / max(len(rows), 1)
    emit({
        "status": "ok",
        "backend": "hypodd",
        "n_events_input": len(catalog),
        "n_relocated": len(rows),
        "rms_before_s": round(rms_before, 4),
        "rms_after_s": round(rms_after, 4),
        "rms_reduction": round(rms_before - rms_after, 4),
        "catalog_file": str(out),
        "run_dir": str(workdir),
    })


if __name__ == "__main__":
    run_main(main)
