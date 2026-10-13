#!/usr/bin/env python3
"""Phase 4: phase association with GaMMA."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, load_stations, log, run_main  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 4: associate picks (GaMMA)")
    parser.add_argument("--picks", required=True, help="picks CSV from Phase 3")
    parser.add_argument("--stations", required=True, help="stations CSV")
    parser.add_argument("--config", default=None,
                        help="GaMMA config JSON (default: assets/gamma_config.json)")
    parser.add_argument("--output", required=True, help="associated events CSV output")
    args = parser.parse_args()

    try:
        from gamma.utils import association
        import numpy as np
        import pandas as pd
        from pyproj import Proj
    except ImportError as exc:
        fail(f"GaMMA unavailable: {exc}",
             hint="install per docs/install.md: pip install --no-deps "
                  "git+https://github.com/wayneweiqiang/GaMMA.git "
                  "&& pip install numba pyproj tqdm")

    picks = pd.read_csv(args.picks)
    stations = load_stations(args.stations)

    if len(picks) == 0:
        fail("no picks to associate (empty input)")
        return

    default_cfg_path = Path(__file__).parent.parent / "assets" / "gamma_config.json"
    cfg_path = Path(args.config) if args.config else default_cfg_path
    if not cfg_path.exists():
        fail(f"config not found: {cfg_path}")
        return
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    # GMMA >=1.2 defaults: legacy dim names are hardcoded in init_centers,
    # and z bounds / BFGS bounds are required keys.
    config.setdefault("dims", ["x(km)", "y(km)", "z(km)"])
    config.setdefault("z(km)", [0.0, 40.0])
    config.setdefault("bfgs_bounds",
                      [[-200, 200], [-200, 200], [0, 40], [-20, 20]])
    log(f"Loaded GaMMA config from {cfg_path}")

    # GaMMA converts pick times to seconds relative to the earliest pick, so the
    # fixed origin-time bounds of the stock config (e.g. [-20, 20] s) pin t0 for
    # events that occur minutes later: every theoretical arrival then misses its
    # pick and the event is filtered away.  Widen the last bound to the data span.
    pick_times = pd.to_datetime(picks["time"], utc=True)
    span_s = (pick_times.max() - pick_times.min()).total_seconds()
    margin_s = max(1.0, span_s * 0.1)
    config["bfgs_bounds"][3] = [-margin_s, span_s + margin_s]
    log(f"Set bfgs time bounds to {config['bfgs_bounds'][3]} "
        f"(data span {span_s:.0f}s)")

    # --- station geometry in local km coordinates (config["dims"]) ---
    proj = Proj(f"+proj=aeqd +lon_0={stations.longitude.mean()} "
                f"+lat_0={stations.latitude.mean()} +units=km")
    stations[["x(km)", "y(km)"]] = stations.apply(
        lambda r: pd.Series(proj(r.longitude, r.latitude)), axis=1)
    # GaMMA station id must match the pick id exactly: both are normalised by
    # stripping the trailing dot ("XX.STA1." == "XX.STA1").
    stations["station"] = stations["station"].astype(str).str.strip(".")
    stations["id"] = stations["station"]
    stations["z(km)"] = -(stations.get("elevation_m", 0.0)).astype(float) / 1000.0

    # --- pick format conversion for GMMA >=1.2 ---
    # required columns: id, timestamp, type, prob, amp (+ station kept for output)
    # strip trailing dots ("XX.STA1." -> "XX.STA1") to match station ids
    picks["station"] = picks["station"].astype(str).str.strip(".")
    known = set(stations["station"])
    unknown = picks[~picks["station"].isin(known)]["station"].unique()
    if len(unknown) > 0:
        log(f"Dropping picks from {len(unknown)} unknown stations: "
            f"{list(unknown)[:10]}...")
        picks = picks[picks["station"].isin(known)]
    if len(picks) == 0:
        fail("all picks dropped: no pick matches a known station")
        return

    gamma_picks = pd.DataFrame({
        "id": picks["station"],
        "station": picks["station"],
        "timestamp": pd.to_datetime(picks["time"], utc=True).dt.tz_convert("UTC"),
        "type": picks["phase"].str.upper().str[0],
        "prob": picks["probability"].astype(float),
        "amp": 1.0,  # unused when config["use_amplitude"]=false
    })
    # Remember the source CSV row numbers: GaMMA reports associations by
    # position in the frame we hand it, and downstream phases need the original
    # row to pick out each event's own arrivals.
    pick_rows = gamma_picks.index.to_numpy()
    gamma_picks = gamma_picks.reset_index(drop=True)

    log(f"Associating {len(gamma_picks)} picks across {len(stations)} stations...")
    events, assignments = association(gamma_picks, stations, config,
                                      method=config.get("method", "BGMM"))
    log(f"Found {len(events)} candidate events")

    rows = []
    for event in events:
        ev_idx = int(event["event_index"])
        n_assign = sum(1 for a in assignments if a[1] == ev_idx)
        # GMMA >=1.2 returns locations keyed by config["dims"], e.g. "x(km)"
        x_km = float(event.get("x(km)", event.get("x_km", np.nan)))
        y_km = float(event.get("y(km)", event.get("y_km", np.nan)))
        z_km = float(event.get("z(km)", event.get("z_km", np.nan)))
        # convert local km (AEQD centered on station centroid) back to lat/lon
        ev_lon, ev_lat = proj(x_km, y_km, inverse=True)
        rows.append({
            "event_id": f"ev{ev_idx:06d}",
            "time": str(event["time"]),
            "latitude": float(ev_lat),
            "longitude": float(ev_lon),
            "depth_km": z_km,
            "magnitude": "",
            "magnitude_type": "",
            "n_picks": int(event.get("num_picks", n_assign)),
            "rms_s": "",
            "gamma_cov": json.dumps({k: float(v) for k, v in event.items()
                                     if k not in ("time", "x(km)", "y(km)",
                                                  "z(km)", "x_km", "y_km",
                                                  "z_km", "event_index")}),
        })

    out = Path(args.output)
    ensure_dir(out.parent)
    pd.DataFrame(rows).to_csv(out, index=False)

    # --- persist the pick -> event association ------------------------------
    # GaMMA reports associations as (pick_index, event_index, prob) tuples, so
    # exporting them lets Phase 5 use each event's own picks instead of a time
    # window, which re-uses one pick for several events.  The prob member is
    # not written: upstream zips a full-length probability array against the
    # shorter filtered pick list, so those values can be misaligned.
    assign_rows = [
        {"pick_index": int(pick_rows[int(a[0])]),
         "event_id": f"ev{int(a[1]):06d}"}
        for a in assignments
        if 0 <= int(a[0]) < len(pick_rows)
    ]
    assign_out = out.parent / "assignments.csv"
    pd.DataFrame(assign_rows, columns=["pick_index", "event_id"]).to_csv(
        assign_out, index=False)
    log(f"Wrote {len(assign_rows)} pick assignments to {assign_out}")

    emit({
        "status": "ok",
        "n_picks_input": int(len(picks)),
        "n_events": len(rows),
        "n_assignments": len(assign_rows),
        "events_file": str(out),
        "assignments_file": str(assign_out),
        "config_used": str(cfg_path),
    })


if __name__ == "__main__":
    run_main(main)
