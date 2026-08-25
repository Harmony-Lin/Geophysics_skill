"""Generate synthetic waveform data for end-to-end skill testing.

Creates a small network of 4 stations, 10 synthetic events, computes
theoretical arrivals with obspy.taup, and writes miniSEED + StationXML
into tests/tmp_data/raw.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "seismic-workflow" / "scripts"))

OUT = Path(__file__).parent / "tmp_data"
RAW = OUT / "raw"

STATIONS = [
    ("XX", "STA1", 30.60, 117.00),
    ("XX", "STA2", 30.70, 117.15),
    ("XX", "STA3", 30.55, 117.20),
    ("XX", "STA4", 30.65, 116.90),
]

EVENTS = [
    (30.62, 117.05, 8.0, 2.0),   # lat lon depth mag
    (30.66, 117.12, 12.0, 2.5),
    (30.58, 117.00, 6.0, 1.8),
    (30.64, 117.18, 10.0, 3.0),
    (30.55, 117.10, 9.0, 2.2),
    (30.68, 117.02, 14.0, 2.7),
    (30.61, 117.22, 7.0, 1.9),
    (30.59, 116.95, 11.0, 2.4),
    (30.67, 117.08, 5.0, 2.1),
    (30.63, 117.16, 13.0, 2.9),
]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return r * 2 * np.arcsin(np.sqrt(a))


def main():
    from obspy import Stream, Trace, UTCDateTime
    from obspy.core.inventory import (Channel, Inventory, Network, Site,
                                      Station as InvStation)

    RAW.mkdir(parents=True, exist_ok=True)

    # inventory
    inv_stations = []
    for net, sta, lat, lon in STATIONS:
        ch = Channel(code="HHZ", location_code="", latitude=lat,
                     longitude=lon, elevation=100.0, depth=0.0,
                     sample_rate=100.0)
        st = InvStation(code=sta, latitude=lat, longitude=lon,
                        elevation=100.0, site=Site(name="synthetic"),
                        creation_date=datetime.now(UTC).replace(tzinfo=None),
                        channels=[ch])
        inv_stations.append(st)
    inv = Inventory(networks=[Network(code="XX", stations=inv_stations)],
                    source="synthetic-test")
    (OUT / "stations.xml").parent.mkdir(parents=True, exist_ok=True)
    inv.write(str(OUT / "stations.xml"), format="STATIONXML")

    from obspy.taup import TauPyModel
    model = TauPyModel(model="iasp91")

    rng = np.random.default_rng(42)
    fs = 100.0
    dur = 120.0
    npts = int(dur * fs)

    base = UTCDateTime(2024, 1, 1, 0, 0, 0)
    for k, (ev_lat, ev_lon, ev_dep, ev_mag) in enumerate(EVENTS):
        origin = base + k * 3600.0
        stream = Stream()
        for net, sta, sta_lat, sta_lon in STATIONS:
            dist_km = haversine_km(ev_lat, ev_lon, sta_lat, sta_lon)
            arrs = model.get_travel_times(
                source_depth_in_km=ev_dep,
                distance_in_degree=dist_km / 111.19,
                phase_list=["p", "s", "P", "S"])
            tp = min((a.time for a in arrs if a.name in ("P", "p")), default=None)
            ts = min((a.time for a in arrs if a.name in ("S", "s")), default=None)
            t_arr = origin - base + (tp or 0)
            data = rng.normal(0, 1e-3, npts)  # noise floor
            tt = np.arange(npts) / fs
            if tp is not None:
                # sharp-onset P: 15 Hz burst with short coda
                env_p = np.exp(-((tt - t_arr) ** 2) / (2 * 0.15 ** 2))
                data += 1.2 * np.sin(2 * np.pi * 15 * tt) * env_p
            if ts is not None:
                # emergent S: 6 Hz with decaying coda after onset
                t_s = origin - base + ts
                env_s = (np.exp(-np.maximum(tt - t_s, 0) / 3.0)
                         * (1 - np.exp(-np.maximum(tt - t_s, 0) / 0.4)))
                data += 1.5 * np.sin(2 * np.pi * 6 * tt) * env_s
            tr = Trace(data=data.astype(np.float32))
            tr.stats.network = net
            tr.stats.station = sta
            tr.stats.channel = "HHZ"
            tr.stats.sampling_rate = fs
            tr.stats.starttime = base
            stream += tr
        stream.write(str(RAW / f"event_{k:02d}.mseed"), format="MSEED")
        print(f"wrote event {k} at {origin}")

    print(f"OK: {len(EVENTS)} events x {len(STATIONS)} stations -> {RAW}")


if __name__ == "__main__":
    main()
