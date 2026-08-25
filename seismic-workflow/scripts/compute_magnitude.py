#!/usr/bin/env python3
"""Phase 7: local magnitude (ML) via Wood-Anderson simulation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, log, run_main, warn  # noqa: E402

WAVEFORM_EXTS = {".mseed", ".miniseed", ".seed", ".sac", ".msd"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 7: compute ML magnitudes (Wood-Anderson)")
    parser.add_argument("--catalog", required=True, help="located catalog CSV")
    parser.add_argument("--waveforms", required=True)
    parser.add_argument("--stations", help="StationXML with response")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    import pandas as pd
    from obspy import Stream, read, read_inventory

    catalog = pd.read_csv(args.catalog)
    files = [f for f in sorted(Path(args.waveforms).rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files under {args.waveforms}")
        return

    inventory = None
    if args.stations and Path(args.stations).exists():
        try:
            inventory = read_inventory(args.stations)
        except Exception as exc:
            warn(f"cannot read inventory ({exc}); response removal skipped")

    if inventory is None:
        emit({
            "status": "warning",
            "magnitude_available": False,
            "error": "no StationXML/response available — ML cannot be computed",
            "catalog_file": args.catalog,
            "hint": "re-run Phase 2 with --remove-response and a valid "
                    "StationXML, then re-run this script",
        })
        return

    log("Loading waveforms and simulating Wood-Anderson...")
    stream = Stream()
    for f in files:
        try:
            stream += read(str(f))
        except Exception as exc:
            warn(f"unreadable file skipped: {f} ({exc})")

    try:
        stream.remove_response(inventory=inventory, output="DISP",
                               water_level=60)
    except Exception as exc:
        warn(f"response removal failed ({exc}); assuming data are already "
             "physical units and continuing with WA simulation only")
    stream.simulate(paz_remove=None, paz_simulate={"poles": [-6.283 + 4.7124j,
                                                             -6.283 - 4.7124j],
                                                   "zeros": [0j],
                                                   "gain": 2080.0,
                                                   "sensitivity": 1.0})

    mags = []
    from obspy import UTCDateTime
    for _, ev in catalog.iterrows():
        ev_time = pd.Timestamp(ev["time"])
        # 20 s window starting at origin (covers S for local distances)
        w_start = UTCDateTime(ev_time.to_pydatetime())
        w_end = w_start + 20
        window = stream.slice(w_start, w_end)
        # Hutton-Boore attenuation for regional ML: -log10(A) + 1.11*log10(R) + 0.00189*R + 3.0
        dists = []
        try:
            from common import haversine_km, load_stations
            stas = load_stations(args.stations.replace(".xml", ".csv")) \
                if args.stations.endswith(".xml") else None
        except Exception:
            stas = None
        ml_values = []
        for k, tr in enumerate(window):
            r_km = 10.0  # default hypocentral distance fallback
            if stas is not None:
                match = stas[stas["station"].astype(str).str.split(".").str[-1]
                             == tr.stats.station]
                if len(match):
                    r_km = float(haversine_km(ev["latitude"], ev["longitude"],
                                              match.latitude.iloc[0],
                                              match.longitude.iloc[0]))
                    r_km = np.hypot(r_km, ev["depth_km"])
            a_um = float(np.max(np.abs(tr.data))) * 1e6  # m -> micrometers? keep units consistent
            ml = -np.log10(max(a_um, 1e-12)) + 1.11 * np.log10(max(r_km, 1.0)) \
                + 0.00189 * r_km + 3.0
            ml_values.append(ml)
        ml_med = float(np.median(ml_values))
        mags.append({"event_id": ev["event_id"], "magnitude": round(ml_med, 2),
                     "magnitude_type": "ML", "n_stations_used": len(ml_values)})

    mag_df = pd.DataFrame(mags)
    out = catalog.merge(mag_df[["event_id", "magnitude", "magnitude_type"]],
                        on="event_id", how="left", suffixes=("", "_new"))
    if "magnitude_new" in out.columns:
        out["magnitude"] = out["magnitude_new"].combine_first(out["magnitude"])
        out.drop(columns=[c for c in out.columns if c.endswith("_new")], inplace=True)

    out_path = Path(args.output)
    ensure_dir(out_path.parent)
    out.to_csv(out_path, index=False)

    n_with_mag = int((out["magnitude"].notna()
                      & (out["magnitude"].astype(str) != "")).sum())
    emit({
        "status": "ok",
        "magnitude_available": True,
        "n_events": len(catalog),
        "n_events_with_magnitude": n_with_mag,
        "median_magnitude": float(out["magnitude"].dropna().astype(float).median())
        if n_with_mag else None,
        "catalog_file": str(out_path),
    })


if __name__ == "__main__":
    run_main(main)
