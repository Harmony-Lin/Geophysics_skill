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
        from gamma.utils import associate
        import numpy as np
        import pandas as pd
        from pyproj import Proj
    except ImportError as exc:
        fail(f"GaMMA unavailable: {exc}",
             hint="install per docs/install.md: pip install git+https://github.com/AI4EPS/gamma.git")

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
    log(f"Loaded GaMMA config from {cfg_path}")

    # --- station geometry in local km coordinates ---
    proj = Proj(f"+proj=aeqd +lon_0={stations.longitude.mean()} "
                f"+lat_0={stations.latitude.mean()} +units=km")
    stations[["x_km", "y_km"]] = stations.apply(
        lambda r: pd.Series(proj(r.longitude, r.latitude)), axis=1)

    # --- pick format conversion for GaMMA ---
    picks["station"] = picks["station"].astype(str).str.split(".").str[-1]
    station_map = {s: i for i, s in enumerate(stations["station"])}
    picks["station_idx"] = picks["station"].map(station_map)
    unknown = picks[picks["station_idx"].isna()]["station"].unique()
    if len(unknown) > 0:
        log(f"Dropping picks from {len(unknown)} unknown stations: "
            f"{list(unknown)[:10]}...")
        picks = picks.dropna(subset=["station_idx"])
    picks["station_idx"] = picks["station_idx"].astype(int)
    picks["time_idx"] = picks["time"].astype("datetime64[ns]")
    picks["time_idx"] = (picks["time_idx"] - picks["time_idx"].min()).dt.total_seconds()
    picks["sigma_t"] = 0.05

    picks_by_station = []
    for _, sta in stations.iterrows():
        p = picks[picks["station_idx"] == station_map.get(sta["station"], -1)]
        picks_by_station.append(p)

    log(f"Associating {len(picks)} picks across {len(stations)} stations...")
    events, assignments = associate(picks_by_station, stations, config)
    log(f"Found {len(events)} candidate events")

    rows = []
    base_time = picks["time"].astype("datetime64[ns]").min()
    for ev_id, (event, assign) in enumerate(zip(events, assignments)):
        pick_rows = [picks_by_station[i].iloc[j] for i, j in assign]
        times = pd.to_datetime([p["time"] for p in pick_rows])
        rows.append({
            "event_id": f"ev{ev_id:06d}",
            "time": str(base_time + pd.Timedelta(seconds=float(event["time"]))),
            "latitude": float(event["latitude"]),
            "longitude": float(event["longitude"]),
            "depth_km": float(event["depth"]),
            "magnitude": "",
            "magnitude_type": "",
            "n_picks": len(pick_rows),
            "rms_s": "",
            "gamma_cov": json.dumps({k: float(v) for k, v in event.items()
                                     if k not in ("time", "latitude",
                                                  "longitude", "depth")}),
        })

    out = Path(args.output)
    ensure_dir(out.parent)
    pd.DataFrame(rows).to_csv(out, index=False)

    emit({
        "status": "ok",
        "n_picks_input": int(len(picks)),
        "n_events": len(rows),
        "events_file": str(out),
        "config_used": str(cfg_path),
    })


if __name__ == "__main__":
    run_main(main)
