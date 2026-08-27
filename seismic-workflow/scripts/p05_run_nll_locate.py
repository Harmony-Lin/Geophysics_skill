#!/usr/bin/env python3
"""Phase 5 (backend A): NonLinLoc wrapper.

JSON/CSV in -> generate NLL control files -> run NLLoc binary ->
parse .hyp output -> CSV + JSON out. The LLM never touches native
NLL formats directly.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, load_stations, log, read_picks, warn  # noqa: E402


def find_nll() -> str:
    for name in ("NLLoc", "nll"):
        path = shutil.which(name)
        if path:
            return path
    fail("NLLoc binary not found on PATH",
         hint="build/install NonLinLoc or use backend B: scripts/grid_locate.py")
    return ""


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%d") + "  " + dt.strftime("%H%M%S.%f")[:-4]


def write_phase_file(picks, events, stations, out_path: Path) -> None:
    """Write NLLOC_OBS phase picks grouped by event."""
    lines = []
    stations_by_code = {str(s["station"]).split(".")[-1]: s
                        for _, s in stations.iterrows()}
    for _, ev in events.iterrows():
        ev_time = datetime.fromisoformat(str(ev["time"]))
        ev_picks = picks[
            (picks["time"] >= str(ev_time - timedelta(seconds=60)))
            & (picks["time"] <= str(ev_time + timedelta(seconds=120)))]
        lines.append(f"# {ev['event_id']}  {fmt_time(ev_time)}")
        for _, pk in ev_picks.iterrows():
            code = str(pk["station"]).split(".")[-1]
            sta = stations_by_code.get(code)
            if sta is None:
                continue
            ph = "P" if str(pk["phase"]).upper().startswith("P") else "S"
            qual = "I"  # impulsive
            weight = "4"
            pick_dt = datetime.fromisoformat(str(pk["time"]))
            lines.append(
                f"{code:<6}{sta.get('network', 'XX')[:2]:<2}{' ':3}"
                f"{pick_dt.strftime('%Y%m%d')}  {pick_dt.strftime('%H%M%S.%f')[:-4]}  "
                f"{qual} {ph} {weight}")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def render_control(template: str, **kw) -> str:
    text = template
    for key, value in kw.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def parse_hyp(hyp_path: Path):
    """Minimal .hyp scatter/final-result parser -> dict per event."""
    results = []
    current = None
    in_signature = False
    with open(hyp_path) as fh:
        for line in fh:
            if line.startswith("SIGNATURE"):
                in_signature = True
                continue
            if line.startswith("PHASE"):
                in_signature = False
                continue
            if line.startswith("END_NLLOC"):
                if current:
                    results.append(current)
                    current = None
                continue
            if line.startswith("HYPOCENTER"):
                parts = line.split()
                vals = dict(zip(parts[1::2], parts[2::2]))
                current = {
                    "event_id": vals.get("SD", None) or current.get("event_id"),
                    "latitude": float(vals["Lat"]),
                    "longitude": float(vals["Lon"]),
                    "depth_km": float(vals["Z"]),
                    "rms_s": float(vals["RMS"]),
                    "gap": float(vals["Gap"]),
                }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5 backend A: NonLinLoc wrapper")
    parser.add_argument("--events", required=True)
    parser.add_argument("--picks", required=True)
    parser.add_argument("--stations", required=True)
    parser.add_argument("--velocity-model", dest="velocity_model",
                        default=str(Path(__file__).parent.parent / "assets" / "iasp91.tvel"))
    parser.add_argument("--control-template", dest="control_template",
                        default=str(Path(__file__).parent.parent / "assets" /
                                    "nll_control_template.conf"))
    parser.add_argument("--output-dir", dest="output_dir", required=True)
    args = parser.parse_args()

    import pandas as pd

    nll_bin = find_nll()
    workdir = ensure_dir(args.output_dir)
    run_dir = ensure_dir(workdir / "run")

    events = pd.read_csv(args.events)
    picks = read_picks(args.picks)
    stations = load_stations(args.stations)

    # copy velocity model into run dir with expected name
    vm_src = Path(args.velocity_model)
    vm_dst = run_dir / vm_src.name
    shutil.copy(vm_src, vm_dst)
    model_name = vm_src.stem

    log("Writing NLLOC_OBS phase file...")
    obs_path = run_dir / "observations.obs"
    write_phase_file(picks, events, stations, obs_path)

    template = Path(args.control_template).read_text(encoding="utf-8")
    control = render_control(
        template,
        RUNBASE=str(run_dir / model_name),
        OBS=str(obs_path),
        MODEL=model_name,
        TTIME=str(run_dir / "time"),
        OUTPUT=str(run_dir / "loc"),
    )
    control_path = run_dir / "nll_control.in"
    control_path.write_text(control, encoding="utf-8")

    log(f"Running NLLoc: {nll_bin} {control_path}")
    proc = subprocess.run([nll_bin, str(control_path)],
                          capture_output=True, text=True, timeout=3600)
    (run_dir / "nlloc_stdout.log").write_text(proc.stdout or "")
    (run_dir / "nlloc_stderr.log").write_text(proc.stderr or "")
    if proc.returncode != 0:
        fail(f"NLLoc exited with code {proc.returncode}",
             stderr_tail=(proc.stderr or "")[-2000:])
        return

    hyp_files = sorted(run_dir.glob("*.hyp"))
    if not hyp_files:
        fail("no .hyp output produced by NLLoc",
             stderr_tail=(proc.stderr or "")[-2000:])
        return

    rows = []
    for hyp in hyp_files:
        try:
            rows.extend(parse_hyp(hyp))
        except Exception as exc:
            warn(f"failed to parse {hyp.name}: {exc}")

    catalog = workdir / "catalog_abs.csv"
    pd.DataFrame(rows).to_csv(catalog, index=False)

    emit({
        "status": "ok",
        "backend": "nonlinloc",
        "n_events_input": len(events),
        "n_located": len(rows),
        "catalog_file": str(catalog),
        "run_dir": str(run_dir),
    })


if __name__ == "__main__":
    run_main(main)
