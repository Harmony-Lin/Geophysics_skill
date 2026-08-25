#!/usr/bin/env python3
"""Phase 5 (backend B): pure-Python absolute location via obspy.taup
travel times + numpy grid search. No compiled binaries required.

Known limitations (see instructions.md §7.3): 1-D velocity model only,
no PDF uncertainty output, depth error typically 2-5 km.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import (emit, ensure_dir, fail, haversine_km, load_stations,  # noqa: E402
                    log, parse_tvel, read_picks, run_main, warn)


def layer_velocity(layers, depth_km: float, key: str) -> float:
    """Velocity at a given depth from layered model (half-space below last)."""
    value = layers[-1][key]
    for lay in layers:
        if depth_km >= lay["top_km"]:
            value = lay[key]
        else:
            break
    return value


_TAUP_MODEL = None


def _get_taup():
    """Singleton TauPyModel — building it is expensive (~seconds)."""
    global _TAUP_MODEL
    if _TAUP_MODEL is None:
        from obspy.taup import TauPyModel
        _TAUP_MODEL = TauPyModel(model="iasp91")
    return _TAUP_MODEL


def travel_times(layers, ev_lat, ev_lon, ev_depth, sta_lat, sta_lon):
    """Approximate theoretical P/S travel times using obspy.taup (IASP91);
    falls back to straight-ray estimate if taup fails."""
    dist_deg = np.asarray(haversine_km(ev_lat, ev_lon, sta_lat, sta_lon)) / 111.19

    try:
        model = _get_taup()
        times_p = np.empty_like(dist_deg)
        times_s = np.empty_like(dist_deg)
        for i, d in enumerate(dist_deg):
            arrivals = model.get_travel_times(
                source_depth_in_km=float(np.clip(ev_depth, 0, 700)),
                distance_in_degree=float(d),
                phase_list=["p", "s", "P", "S"])
            tp = [a.time for a in arrivals if a.name in ("P", "p")]
            ts = [a.time for a in arrivals if a.name in ("S", "s")]
            times_p[i] = min(tp) if tp else np.nan
            times_s[i] = min(ts) if ts else np.nan
        return times_p, times_s
    except Exception as exc:
        warn(f"taup failed ({exc}); falling back to straight-ray estimate")
        # straight ray through average velocity above source depth
        vp = layer_velocity(layers, ev_depth, "vp")
        vs = layer_velocity(layers, ev_depth, "vs")
        epi = np.asarray(dist_deg) * 111.19
        path = np.sqrt(epi ** 2 + float(ev_depth) ** 2)
        return path / vp, path / vs


def locate_event(picks_ev, stations, layers,
                 lat_range, lon_range, dep_range, cell_km=5.0):
    """Grid search over (lat, lon, depth). Returns best location + rms."""
    p_picks = picks_ev[picks_ev["phase"].str.upper().str.startswith("P")]
    s_picks = picks_ev[picks_ev["phase"].str.upper().str.startswith("S")]
    if len(p_picks) < 3:
        return None

    lats = np.arange(lat_range[0], lat_range[1] + 1e-9,
                     cell_km / 111.19)
    lons = np.arange(lon_range[0], lon_range[1] + 1e-9,
                     cell_km / (111.19 * max(
                         np.cos(np.radians((lat_range[0] + lat_range[1]) / 2)),
                         0.1)))
    deps = np.arange(dep_range[0], dep_range[1] + 1e-9, max(cell_km, 2.0))

    best = None
    for dep in deps:
        for lat in lats:
            for lon in lons:
                stas = stations
                tp, ts = travel_times(layers, lat, lon, dep,
                                      stas["latitude"].values,
                                      stas["longitude"].values)
                # station lookup for predicted times
                t_pred = {}
                for idx, sta_id in enumerate(stas["station"]):
                    code = str(sta_id).strip().rstrip(".")
                    code = code.split(".")[-1] if "." in code else code
                    t_pred[code] = (tp[idx], ts[idx])

                residuals = []
                origins = []
                for _, pk in p_picks.iterrows():
                    raw = str(pk["station"]).strip().rstrip(".")
                    code = raw.split(".")[-1] if "." in raw else raw
                    if code not in t_pred or np.isnan(t_pred[code][0]):
                        continue
                    origins.append(pk["time_num"] - t_pred[code][0])
                for _, pk in s_picks.iterrows():
                    raw = str(pk["station"]).strip().rstrip(".")
                    code = raw.split(".")[-1] if "." in raw else raw
                    if code not in t_pred or np.isnan(t_pred[code][1]):
                        continue
                    origins.append(pk["time_num"] - t_pred[code][1])
                if len(origins) < 4:
                    continue
                origins = np.array(origins)
                o = np.median(origins)
                rms = float(np.sqrt(np.mean((origins - o) ** 2)))
                if best is None or rms < best["rms"]:
                    best = {"lat": float(lat), "lon": float(lon),
                            "depth": float(dep), "origin": float(o), "rms": rms}
    return best


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5 backend B: grid-search absolute location "
                    "(pure Python, obspy.taup)")
    parser.add_argument("--events", required=True, help="associated events CSV")
    parser.add_argument("--picks", required=True, help="picks CSV")
    parser.add_argument("--stations", required=True, help="stations CSV")
    parser.add_argument("--velocity-model", dest="velocity_model",
                        default=str(Path(__file__).parent.parent / "assets" / "iasp91.tvel"))
    parser.add_argument("--output", required=True, help="located catalog CSV")
    parser.add_argument("--cell-km", dest="cell_km", type=float, default=5.0,
                        help="grid spacing km")
    parser.add_argument("--max-depth-km", dest="max_depth_km", type=float, default=40.0)
    args = parser.parse_args()

    import pandas as pd

    events = pd.read_csv(args.events)
    picks = read_picks(args.picks)
    stations = load_stations(args.stations)
    layers = parse_tvel(args.velocity_model)
    log(f"Loaded {len(events)} events, {len(picks)} picks, "
        f"{len(stations)} stations, {len(layers)} velocity layers")

    picks["time_dt"] = pd.to_datetime(picks["time"], utc=True)
    t0 = picks["time_dt"].min()
    picks["time_num"] = (picks["time_dt"] - t0).dt.total_seconds()

    located_rows = []
    n_located = 0
    for _, ev in events.iterrows():
        ev_time = pd.Timestamp(ev["time"])
        if ev_time.tzinfo is None:
            ev_time = ev_time.tz_localize("UTC")
        ev_picks = picks[
            (picks["time_dt"] >= ev_time - pd.Timedelta(seconds=60))
            & (picks["time_dt"] <= ev_time + pd.Timedelta(seconds=120))]
        if len(ev_picks) < 4:
            warn(f"{ev['event_id']}: too few picks ({len(ev_picks)}), skipped")
            continue
        # coarse search box around GaMMA initial location ±0.5 deg / ±20 km
        lat_box = (ev["latitude"] - 0.5, ev["latitude"] + 0.5)
        lon_box = (ev["longitude"] - 0.5, ev["longitude"] + 0.5)
        dep_box = (0.0, args.max_depth_km)
        best = locate_event(ev_picks, stations, layers,
                            lat_box, lon_box, dep_box, cell_km=args.cell_km)
        if best is None:
            warn(f"{ev['event_id']}: location failed")
            continue
        origin_time = t0 + pd.Timedelta(seconds=float(best["origin"]))
        located_rows.append({
            "event_id": ev["event_id"],
            "time": str(origin_time),
            "latitude": round(best["lat"], 4),
            "longitude": round(best["lon"], 4),
            "depth_km": round(best["depth"], 2),
            "magnitude": ev.get("magnitude", ""),
            "magnitude_type": ev.get("magnitude_type", ""),
            "n_picks": int(len(ev_picks)),
            "rms_s": round(best["rms"], 3),
        })
        n_located += 1
        log(f"{ev['event_id']}: lat={best['lat']:.3f} lon={best['lon']:.3f} "
            f"dep={best['depth']:.1f}km rms={best['rms']:.2f}s")

    out = Path(args.output)
    ensure_dir(out.parent)
    pd.DataFrame(located_rows).to_csv(out, index=False)

    emit({
        "status": "ok",
        "backend": "python-grid",
        "n_events_input": len(events),
        "n_located": n_located,
        "catalog_file": str(out),
        "velocity_model": args.velocity_model,
        "cell_km": args.cell_km,
        "note": "1-D model grid search; depth error typically 2-5 km",
    })


if __name__ == "__main__":
    run_main(main)
