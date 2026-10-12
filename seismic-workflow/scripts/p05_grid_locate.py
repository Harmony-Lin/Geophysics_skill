#!/usr/bin/env python3
"""Phase 5 (backend B): pure-Python absolute location via obspy.taup
travel times + Geiger iterative inversion. No compiled binaries required.

The event origin theta = (t0, lat, lon, depth) is obtained with the standard
Geiger scheme (see obs_taup_scipy_Geiger定位器方法论详解.md):

1. forward model t_cal_i = t0 + T_i(lat, lon, depth) via obspy.taup;
2. travel-time residual r_i = t_obs_i - t_cal_i;
3. numerical Jacobian A = d t_cal / d theta (column for t0 is analytic, = 1;
   lat/lon/depth columns use finite differences - md §4.2);
4. damped weighted normal equations delta = (A^T W A + eps I)^-1 A^T W r
   with a relaxation factor alpha applied to the update (md §7.4);
5. weights: phase type (P = 1.0, S = 0.6, md §5.1) times Tukey biweight
   robust weights from the MAD-scaled residuals (md §5.2);
6. iterate until RMS < 0.1 s or the parameter change < 1e-4 (md §7.3), plus a
   stagnation guard that stops once the RMS stops improving appreciably.

Known limitations (see instructions.md §7.3): 1-D velocity model only,
no PDF uncertainty output, depth error typically 2-5 km.
"""
from __future__ import annotations

import argparse
import os
import sys
from functools import lru_cache
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
# Regional/local arrivals must be considered explicitly: with only ["P", "S"]
# IASP91 returns no Pc/Pn/Pg before ~2 deg, and the first arrival at a few
# hundred km is Pn/Sn (or Pg/Sg closer in) rather than the teleseismic branch.
_PHASE_CANDIDATES = {"P": ("p", "P", "Pn", "Pg"), "S": ("s", "S", "Sn", "Sg")}
_TAUP_FAIL_WARNED = False


def _get_taup():
    """Singleton TauPyModel — building it is expensive (~seconds)."""
    global _TAUP_MODEL
    if _TAUP_MODEL is None:
        from obspy.taup import TauPyModel
        _TAUP_MODEL = TauPyModel(model="iasp91")
    return _TAUP_MODEL


@lru_cache(maxsize=262144)
def _taup_travel_times(depth_km: float, dist_deg: float):
    """First-arrival P and S travel times (s) for one path, or NaN if absent."""
    arrivals = _get_taup().get_travel_times(
        source_depth_in_km=depth_km, distance_in_degree=dist_deg,
        phase_list=list(_PHASE_CANDIDATES["P"]) + list(_PHASE_CANDIDATES["S"]))
    times = []
    for family in ("P", "S"):
        candidates = [a.time for a in arrivals
                      if a.name in _PHASE_CANDIDATES[family]]
        times.append(min(candidates) if candidates else float("nan"))
    return float(times[0]), float(times[1])


# A tau-p search costs ~20-30 ms, and the Geiger Jacobian evaluates 4 forward
# models per iteration over every source-station path, so calling taup directly
# makes one event take minutes. Instead the (depth, distance) surface is
# sampled once on a grid and interpolated; the grid is cached on disk.
_TT_DEPTH_NODES = np.arange(0.0, 50.0 + 1e-9, 1.0)
_TT_DIST_NODES = np.arange(0.05, 8.0 + 1e-9, 0.1)


def _tt_cache_path() -> Path:
    override = os.environ.get("SEISMIC_TT_CACHE")
    base = Path(override) if override else Path(__file__).resolve().parent.parent / ".cache"
    return base / "iasp91_tt_table.npz"


def _fill_gaps(row):
    """Linearly bridge NaNs along distance so they cannot poison interpolation."""
    ok = np.isfinite(row)
    if ok.all() or not ok.any():
        return row
    return np.interp(_TT_DIST_NODES, _TT_DIST_NODES[ok], row[ok])


@lru_cache(maxsize=1)
def _tt_table():
    """``(depths, distances, P, S)`` IASP91 first-arrival grid.

    Built on first use (about two minutes) and reused from disk afterwards, so
    the cost is paid once per machine rather than per event.
    """
    path = _tt_cache_path()
    if path.is_file():
        try:
            with np.load(path) as data:
                return (data["depth"], data["dist"], data["p"], data["s"])
        except Exception as exc:  # corrupt cache -- rebuild rather than crash
            warn(f"ignoring unreadable travel-time cache {path.name} ({exc})")
    log(f"building IASP91 travel-time table "
        f"({len(_TT_DEPTH_NODES)}x{len(_TT_DIST_NODES)} paths, first run only)")
    model = _get_taup()
    phase_list = list(_PHASE_CANDIDATES["P"]) + list(_PHASE_CANDIDATES["S"])
    times_p = np.empty((len(_TT_DEPTH_NODES), len(_TT_DIST_NODES)))
    times_s = np.empty_like(times_p)
    for i, depth in enumerate(_TT_DEPTH_NODES):
        for j, dist in enumerate(_TT_DIST_NODES):
            arrivals = model.get_travel_times(
                source_depth_in_km=float(depth),
                distance_in_degree=float(dist), phase_list=phase_list)
            p_cand = [a.time for a in arrivals if a.name in _PHASE_CANDIDATES["P"]]
            s_cand = [a.time for a in arrivals if a.name in _PHASE_CANDIDATES["S"]]
            times_p[i, j] = min(p_cand) if p_cand else np.nan
            times_s[i, j] = min(s_cand) if s_cand else np.nan
    times_p = np.vstack([_fill_gaps(row) for row in times_p])
    times_s = np.vstack([_fill_gaps(row) for row in times_s])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, depth=_TT_DEPTH_NODES, dist=_TT_DIST_NODES,
                            p=times_p, s=times_s)
        log(f"cached travel-time table at {path}")
    except OSError as exc:
        warn(f"could not write travel-time cache ({exc}); rebuilding each run")
    return _TT_DEPTH_NODES, _TT_DIST_NODES, times_p, times_s


def _interp_tt(depth_km: float, dist_deg: float):
    """Interpolate the cached grid: linear in distance, quadratic in depth.

    The depth dimension must stay smooth — the Jacobian differentiates travel
    time with respect to depth with a 0.01 km step, and a piecewise-linear grid
    would make that derivative a staircase and stall the depth update.
    """
    depths, distances, times_p, times_s = _tt_table()
    i = int(np.clip(np.searchsorted(depths, depth_km), 1, len(depths) - 2))
    z = (depths[i - 1], depths[i], depths[i + 1])
    p_vals = [float(np.interp(dist_deg, distances, times_p[k])) for k in (i - 1, i, i + 1)]
    s_vals = [float(np.interp(dist_deg, distances, times_s[k])) for k in (i - 1, i, i + 1)]
    if not all(np.isfinite(p_vals + s_vals)):
        return _taup_travel_times(round(depth_km, 4), round(dist_deg, 4))
    denom = ((z[0] - z[1]) * (z[0] - z[2]),
             (z[1] - z[0]) * (z[1] - z[2]),
             (z[2] - z[0]) * (z[2] - z[1]))
    w = ((depth_km - z[1]) * (depth_km - z[2]) / denom[0],
         (depth_km - z[0]) * (depth_km - z[2]) / denom[1],
         (depth_km - z[0]) * (depth_km - z[1]) / denom[2])
    return (sum(wi * vi for wi, vi in zip(w, p_vals)),
            sum(wi * vi for wi, vi in zip(w, s_vals)))


def travel_times(layers, ev_lat, ev_lon, ev_depth, sta_lat, sta_lon):
    """Theoretical P/S travel times (s) for ONE source→station path (IASP91).

    Takes and returns scalars: ``(t_p, t_s)``. Paths inside the cached grid are
    interpolated; anything outside it falls back to a direct taup call, and if
    taup is unusable the straight-ray estimate is used (warning once).
    """
    global _TAUP_FAIL_WARNED
    dist_deg = float(haversine_km(ev_lat, ev_lon, sta_lat, sta_lon)) / 111.19
    depth_km = float(np.clip(ev_depth, 0.0, 700.0))
    try:
        if (depth_km <= _TT_DEPTH_NODES[-1] and 0.0 <= dist_deg <= _TT_DIST_NODES[-1]):
            return _interp_tt(depth_km, dist_deg)
        return _taup_travel_times(round(depth_km, 4), round(dist_deg, 4))
    except Exception as exc:
        if not _TAUP_FAIL_WARNED:
            _TAUP_FAIL_WARNED = True
            warn(f"taup failed ({exc}); falling back to straight-ray estimate")
        # straight ray through the velocity at the source depth
        vp = layer_velocity(layers, ev_depth, "vp")
        vs = layer_velocity(layers, ev_depth, "vs")
        path = np.hypot(dist_deg * 111.19, depth_km)
        return float(path / vp), float(path / vs)


# --- Geiger iteration controls (obs_taup_scipy_Geiger定位器方法论详解.md §5-§7) --
_TOL_RMS = 0.1            # converged when weighted RMS < this (s)
_TOL_PARAM = 1e-4         # converged when max |delta theta| < this
_MIN_ITER = 3             # never stop before this many iterations
_MAX_ITER = 50
_RELAX = 0.5              # relaxation factor alpha (md §7.4)
_DAMPING = 1e-6           # Tikhonov damping eps (md §7.4)
_STEP_LATLON = 0.01       # finite-difference step, deg (md §4.2)
_STEP_DEPTH = 0.01        # finite-difference step, km
_INIT_DEPTH_KM = 10.0     # crustal default initial depth (md §7.2)
_PHASE_WEIGHT = {"P": 1.0, "S": 0.6}   # md §5.1
_ROBUST_MIN_OBS = 6       # only re-weight when enough observations remain
_ROBUST_AFTER = 0         # robust weights from the very first iteration
_STALL_ITERS = 3          # look back this many iterations for improvements
_STALL_RATIO = 1e-3       # stop when the RMS improves by less than this


def _station_code(raw) -> str:
    """Normalise a SEED-style station id to its bare station code.

    Identifiers follow the ``NET.STA.LOC[.CHAN]`` (ObsPy / SEED) order, so the
    station is the *second* field: ``AZ.KNW.`` -> ``KNW`` and ``BK.PKD.00`` ->
    ``PKD``.  Taking the *last* field would return the location code instead,
    which collapses every ``.00`` station of the network onto a single entry --
    and therefore onto a single, wrong set of coordinates.
    """
    code = str(raw).strip().rstrip(".")
    parts = [part for part in code.split(".") if part]
    if not parts:
        return ""
    return parts[1] if len(parts) >= 2 else parts[0]


def _phase_family(phase):
    """``"P"`` / ``"S"`` for a pick's phase label, else ``None``."""
    label = str(phase).strip().upper()
    for family in ("P", "S"):
        if label.startswith(family):
            return family
    return None


def _build_observations(picks_ev, station_rows):
    """Flatten an event's picks into ``[(station_row, family, time, weight)]``.

    Picks for stations absent from the station table are dropped; ``time`` is
    the pick's ``time_num`` (seconds relative to the global reference time).
    """
    observations = []
    for _, pick in picks_ev.iterrows():
        family = _phase_family(pick["phase"])
        if family is None:
            continue
        row = station_rows.get(_station_code(pick["station"]))
        if row is None:
            continue
        t_obs = float(pick["time_num"])
        if not np.isfinite(t_obs):
            continue
        observations.append((row, family, t_obs, _PHASE_WEIGHT[family]))
    return observations


def _predicted_times(layers, observations, sta_lat, sta_lon, theta):
    """Theoretical arrival times ``t_cal = t0 + T(lat, lon, depth)``.

    One taup call serves every pick of a station (P and S come from the same
    arrival list); unavailable phases yield NaN.
    """
    t_cal = np.full(len(observations), np.nan)
    if not observations:
        return t_cal
    t0, lat, lon, depth = (float(v) for v in theta)
    per_station = {}
    for i, (row, family, _t_obs, _weight) in enumerate(observations):
        if row not in per_station:
            per_station[row] = travel_times(layers, lat, lon, depth,
                                            float(sta_lat[row]),
                                            float(sta_lon[row]))
        travel = per_station[row][0 if family == "P" else 1]
        if np.isfinite(travel):
            t_cal[i] = t0 + travel
    return t_cal


def _tukey_weights(residuals, c: float = 1.345):
    """Tukey biweight weights of residuals, MAD-scaled (md §5.2)."""
    abs_r = np.abs(np.asarray(residuals, dtype=float))
    median_abs = float(np.median(abs_r))
    if median_abs < 1e-10:
        return np.ones_like(abs_r)
    u = abs_r / (c * median_abs * 1.4826)
    return np.where(u < 1.0, (1.0 - u ** 2) ** 2, 0.0)


def locate_event(picks_ev, stations, layers,
                 lat_range, lon_range, dep_range, cell_km=5.0):
    """Geiger location of one event (damped weighted least squares).

    ``cell_km`` is a deprecated positional argument kept for interface
    compatibility and ignored. Returns ``None`` when fewer than 3 P picks can
    be matched to stations, otherwise a dict with ``lat``, ``lon``, ``depth``,
    ``origin`` (seconds relative to the caller's reference time), ``rms``
    (weighted travel-time RMS in s) plus ``n_obs`` / ``n_iter`` diagnostics.
    """
    station_rows = {}
    # ``station_code`` is the pre-computed bare code; fall back to parsing the
    # SEED-style ``station`` name when the column is absent.
    code_column = "station_code" if "station_code" in stations.columns else "station"
    for i, station_id in enumerate(stations[code_column]):
        station_rows.setdefault(_station_code(station_id), i)
    sta_lat = np.atleast_1d(stations["latitude"].to_numpy(dtype=float))
    sta_lon = np.atleast_1d(stations["longitude"].to_numpy(dtype=float))

    observations = _build_observations(picks_ev, station_rows)
    if sum(1 for obs in observations if obs[1] == "P") < 3:
        return None
    t_obs = np.array([obs[2] for obs in observations], dtype=float)
    w_phase = np.array([obs[3] for obs in observations], dtype=float)

    # initial guess (md §7.2): box centre for lat/lon, 10 km depth, and t0 from
    # the earliest P pick minus its theoretical travel time.
    theta = np.array([0.0,
                      0.5 * (lat_range[0] + lat_range[1]),
                      0.5 * (lon_range[0] + lon_range[1]),
                      float(np.clip(_INIT_DEPTH_KM, dep_range[0], dep_range[1]))],
                     dtype=float)
    t_cal = _predicted_times(layers, observations, sta_lat, sta_lon, theta)
    p_usable = [i for i, obs in enumerate(observations)
                if obs[1] == "P" and np.isfinite(t_cal[i])]
    if p_usable:
        # Median rather than the earliest pick: a single spurious early
        # detection would otherwise shift the whole origin time by tens of
        # seconds, because the minimum reproduces that one outlier exactly.
        theta[0] = float(np.median(t_obs[p_usable] - t_cal[p_usable]))

    best = None                     # (rms, theta) of the best model seen
    n_iter = 0
    stall = 0                       # iterations without a meaningful gain
    for iteration in range(_MAX_ITER):
        n_iter = iteration + 1
        t_cal = _predicted_times(layers, observations, sta_lat, sta_lon, theta)
        mask = np.isfinite(t_cal)
        n_valid = int(mask.sum())
        if n_valid < 3:
            break
        freeze_depth = n_valid < 4   # depth cannot be resolved from 3 picks
        r = t_obs[mask] - t_cal[mask]

        weights = w_phase[mask].copy()
        if iteration >= _ROBUST_AFTER and n_valid >= _ROBUST_MIN_OBS:
            weights = weights * _tukey_weights(r)
        if weights.sum() <= 0.0:
            weights = w_phase[mask].copy()
        rms = float(np.sqrt(np.sum(weights * r ** 2) / weights.sum()))
        # Model selection needs a *robust* metric. A phase-weighted squared
        # error RMS is dominated by the picks that belong to another arrival
        # (typically a third of them, with residuals of tens of seconds), which
        # pulls the solution to the edge of the search box. The MAD-scaled
        # median is immune to that fraction and, unlike the Tukey-weighted RMS,
        # cannot be driven down simply by rejecting more data.
        rms_sel = float(1.4826 * np.median(np.abs(r)))
        previous_best = best[0] if best is not None else np.inf
        if rms_sel < previous_best:
            best = (rms_sel, theta.copy())
        stall = 0 if rms_sel < previous_best * (1.0 - _STALL_RATIO) else stall + 1

        # Jacobian of the predicted arrival times (md §3.5 / §4.2)
        jac = np.zeros((n_valid, 4))
        jac[:, 0] = 1.0
        for col, step in ((1, _STEP_LATLON), (2, _STEP_LATLON),
                          (3, _STEP_DEPTH)):
            theta_pert = theta.copy()
            theta_pert[col] += step
            t_pert = _predicted_times(layers, observations, sta_lat, sta_lon,
                                      theta_pert)
            diff = t_pert[mask] - t_cal[mask]
            jac[:, col] = np.where(np.isfinite(diff), diff, 0.0) / step
        if freeze_depth:
            jac[:, 3] = 0.0

        normal = jac.T @ (weights[:, None] * jac) + _DAMPING * np.eye(4)
        rhs = jac.T @ (weights * r)
        try:
            delta = np.linalg.solve(normal, rhs)
        except np.linalg.LinAlgError:
            delta = np.linalg.solve(normal + 1e-3 * np.eye(4), rhs)
        if not np.all(np.isfinite(delta)):
            break
        if freeze_depth:
            delta[3] = 0.0
        max_delta = float(np.max(np.abs(delta)))

        theta = theta + _RELAX * delta
        theta[1] = np.clip(theta[1], *lat_range)
        theta[2] = np.clip(theta[2], *lon_range)
        theta[3] = np.clip(theta[3], *dep_range)

        if (n_iter >= _MIN_ITER
                and (max_delta < _TOL_PARAM or rms < _TOL_RMS
                     or stall >= _STALL_ITERS)):
            break

    if best is None:
        return None
    t_cal = _predicted_times(layers, observations, sta_lat, sta_lon, best[1])
    mask = np.isfinite(t_cal)
    if int(mask.sum()) < 3:
        return None
    r = t_obs[mask] - t_cal[mask]
    weights = w_phase[mask]
    rms = float(np.sqrt(np.sum(weights * r ** 2) / weights.sum()))
    return {"lat": float(best[1][1]), "lon": float(best[1][2]),
            "depth": float(best[1][3]), "origin": float(best[1][0]),
            "rms": rms, "n_obs": int(mask.sum()), "n_iter": n_iter}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5 backend B: Geiger iterative absolute location "
                    "(pure Python, obspy.taup)")
    parser.add_argument("--events", required=True, help="associated events CSV")
    parser.add_argument("--picks", required=True, help="picks CSV")
    parser.add_argument("--stations", required=True, help="stations CSV")
    parser.add_argument("--velocity-model", dest="velocity_model",
                        default=str(Path(__file__).parent.parent / "assets" / "iasp91.tvel"))
    parser.add_argument("--output", required=True, help="located catalog CSV")
    parser.add_argument("--cell-km", dest="cell_km", type=float, default=5.0,
                        help="DEPRECATED, ignored: the Geiger iteration uses "
                             "no search grid")
    parser.add_argument("--max-depth-km", dest="max_depth_km", type=float, default=40.0)
    args = parser.parse_args()

    if args.cell_km != 5.0:
        warn("--cell-km is deprecated and ignored: the Geiger locator does "
             "not use a search grid")

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

    # Prefer Phase 4's own pick -> event association when it was exported.  A
    # plain time window re-uses the same pick for every event inside it, so an
    # event ends up fitting arrivals that belong to its neighbours.
    assignments = None
    assign_path = Path(args.events).parent / "assignments.csv"
    if assign_path.exists():
        cand = pd.read_csv(assign_path)
        if {"pick_index", "event_id"}.issubset(cand.columns):
            n_bad = int((~cand["pick_index"].between(0, len(picks) - 1)).sum())
            if n_bad:
                warn(f"{assign_path}: {n_bad} assignment rows reference a "
                     f"pick outside the {len(picks)} loaded picks; ignoring "
                     f"them")
                cand = cand[cand["pick_index"].between(0, len(picks) - 1)]
            assignments = cand
            log(f"Using {len(assignments)} GaMMA pick assignments from "
                f"{assign_path}")
        else:
            warn(f"{assign_path} lacks pick_index/event_id columns; "
                 f"falling back to a time window")
    if assignments is None:
        warn("no usable assignments.csv next to the events file; selecting "
             "picks by a +/-60/+120 s time window instead")

    located_rows = []
    n_located = 0
    for _, ev in events.iterrows():
        ev_time = pd.Timestamp(ev["time"])
        if ev_time.tzinfo is None:
            ev_time = ev_time.tz_localize("UTC")
        if assignments is not None:
            idx = assignments.loc[assignments["event_id"] == ev["event_id"],
                                  "pick_index"].astype(int)
            ev_picks = picks.iloc[idx.to_numpy()]
        else:
            ev_picks = picks[
                (picks["time_dt"] >= ev_time - pd.Timedelta(seconds=60))
                & (picks["time_dt"] <= ev_time + pd.Timedelta(seconds=120))]
        if len(ev_picks) < 3:
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
            f"dep={best['depth']:.1f}km rms={best['rms']:.2f}s "
            f"({best['n_obs']} obs, {best['n_iter']} iter)")

    out = Path(args.output)
    ensure_dir(out.parent)
    pd.DataFrame(located_rows).to_csv(out, index=False)

    emit({
        "status": "ok",
        "backend": "python-geiger",
        "n_events_input": len(events),
        "n_located": n_located,
        "catalog_file": str(out),
        "velocity_model": args.velocity_model,
        "cell_km": args.cell_km,
        "note": "1-D model Geiger iteration (damped weighted least squares); "
                "depth error typically 2-5 km",
    })


if __name__ == "__main__":
    run_main(main)
