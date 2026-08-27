"""Generate synthetic waveform data for end-to-end skill testing.

Default scenario: the 2024 Salcha (Alaska) M5.3 earthquake sequence.

  Mainshock : 2024-01-19 21:34:35 UTC, Mw 5.3, 64.4909N -146.9641E, 10.5 km
              (USGS event ak024vrx24o, AK network)
  Aftershocks: ~100 events M1.5-4.1 within ~6 km of the mainshock over
              2024-01-19 .. 2024-02-28 (USGS ComCat, AK catalog)
  Stations  : real coordinates of AK-network stations operating in Jan-Feb
              2024 around Fairbanks/Salcha (HDA, PS08, CCB, WRH, FA*, GIB,
              TAPE, UAFE, MDM) plus IU COLA.

Waveforms are synthetic (deterministic seed): sharp-onset P burst + emergent
S coda computed with obspy.taup iasp91 travel times, written as miniSEED
(one file per event) plus a StationXML inventory.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "seismic-workflow" / "scripts"))

OUT = Path(__file__).parent / "tmp_data"
RAW = OUT / "raw"

# ---------------------------------------------------------------------------
# Default dataset parameters — 2024 Salcha, Alaska M5.3 sequence
# (recorded here so tests are reproducible; see docs/verification.md)
# ---------------------------------------------------------------------------
MAINSHOCK = {
    "event_id": "ak024vrx24o",
    "time": "2024-01-19T21:34:35.071",   # UTC origin time
    "latitude": 64.4909,
    "longitude": -146.9641,
    "depth_km": 10.5,
    "magnitude": 5.3,
    "magnitude_type": "mww",
}

# Aftershock subset used for the synthetic test set (real USGS parameters).
# (event_id, time_offset_s from mainshock, lat, lon, depth_km, magnitude)
AFTERSHOCKS = [
    ("ak024vrxr2h",   204.7,   64.4764, -146.9235,  9.8, 3.7),
    ("ak024vt1bb3",  6461.2,   64.4880, -146.8929, 11.6, 3.4),
    ("ak024vskl4e",  4193.7,   64.4915, -146.9510,  7.3, 3.1),
    ("ak024140gtrp", 437326.4, 64.4811, -146.9401, 10.2, 3.1),
    ("ak024x4qe39",  21543.4,  64.4841, -146.9384,  9.0, 2.8),
    ("ak024x7qum2",  39903.5,  64.4974, -146.9259,  9.2, 2.7),
    ("ak0241237wwz", 298672.9, 64.4969, -146.9130,  9.3, 2.7),
    ("ak024vt0he3",  6233.7,   64.4880, -146.9510,  9.0, 2.6),
    ("ak024yrpzso", 111609.0,  64.4729, -146.9534,  9.3, 2.6),
    ("ak024vs0xvj",  1090.9,   64.4890, -146.9111, 10.0, 2.5),
]

# Real station coordinates (AK network, active Jan-Feb 2024; COLA is IU).
STATIONS = [
    # net, code,      lat,       lon, elev_m, site
    ("AK", "HDA",  64.4091, -146.9482, 437.0, "Harding Lake"),
    ("AK", "PS08", 64.5422, -146.8221, 320.0, "TAPS Pump Station 8"),
    ("AK", "CCB",  64.6453, -147.8053, 233.0, "Clear Creek Butte"),
    ("AK", "WRH",  64.4716, -148.0918, 329.0, "Wood River Hill"),
    ("AK", "FA01", 64.8348, -147.9371, 263.0, "Chena-GS Fire Station"),
    ("AK", "FA05", 64.8393, -147.7516, 135.0, "Denali Elem. School"),
    ("AK", "FA06", 64.8461, -147.6957, 136.0, "Nordale Education Center"),
    ("AK", "FA07", 64.8267, -147.6946, 135.0, "Arctic Light Elem. School"),
    ("AK", "FA09", 64.8377, -147.8172, 135.0, "DNR Fairbanks"),
    ("AK", "FA10", 64.8186, -147.7784, 133.0, "Bus Barn"),
    ("AK", "GIB",  64.8598, -147.8491, 180.0, "Geophysical Institute"),
    ("AK", "TAPE", 64.8928, -147.7890, 320.0, "Goshawk Ln"),
    ("AK", "UAFE", 64.8553, -147.8181, 184.0, "UAF Engineering Array"),
    ("IU", "COLA", 64.8736, -147.8618, 200.0, "College Outpost"),
]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return r * 2 * np.arcsin(np.sqrt(a))


def synth_event(model, rng, ev_lat, ev_lon, ev_dep, ev_mag,
                origin_base, fs=100.0, dur=120.0):
    """Return a Stream for one synthetic event (all stations, common start)."""
    from obspy import Stream, Trace

    npts = int(dur * fs)
    tt = np.arange(npts) / fs

    # amplitude scales with magnitude relative to the M5.3 reference
    amp = 10 ** (ev_mag - 5.3)
    stream = Stream()
    for net, sta, sta_lat, sta_lon, _elev, _site in STATIONS:
        dist_km = haversine_km(ev_lat, ev_lon, sta_lat, sta_lon)
        arrs = model.get_travel_times(
            source_depth_in_km=ev_dep,
            distance_in_degree=dist_km / 111.19,
            phase_list=["p", "s", "P", "S"])
        tp = min((a.time for a in arrs if a.name in ("P", "p")), default=None)
        ts = min((a.time for a in arrs if a.name in ("S", "s")), default=None)

        data = rng.normal(0, 1e-3, npts)  # noise floor

        if tp is not None:
            t_arr = tp
            env_p = np.exp(-((tt - t_arr) ** 2) / (2 * 0.15 ** 2))
            data += 1.2 * amp * np.sin(2 * np.pi * 15 * tt) * env_p
        if ts is not None and ts < dur:
            env_s = (np.exp(-np.maximum(tt - ts, 0) / 3.0)
                     * (1 - np.exp(-np.maximum(tt - ts, 0) / 0.4)))
            data += 1.5 * amp * np.sin(2 * np.pi * 6 * tt) * env_s

        tr = Trace(data=data.astype(np.float32))
        tr.stats.network = net
        tr.stats.station = sta
        tr.stats.channel = "HHZ"
        tr.stats.sampling_rate = fs
        tr.stats.starttime = origin_base
        stream += tr
    return stream


def main():
    from obspy import UTCDateTime
    from obspy.core.inventory import (Channel, Inventory, Network, Site,
                                      Station as InvStation)

    RAW.mkdir(parents=True, exist_ok=True)

    # ---- inventory -------------------------------------------------------
    inv_stations = []
    for net, sta, lat, lon, elev, site in STATIONS:
        ch = Channel(code="HHZ", location_code="", latitude=lat,
                     longitude=lon, elevation=elev, depth=0.0,
                     sample_rate=100.0)
        st = InvStation(code=sta, latitude=lat, longitude=lon,
                        elevation=elev, site=Site(name=site),
                        creation_date=datetime.now(UTC).replace(tzinfo=None),
                        channels=[ch])
        inv_stations.append(st)

    networks = []
    for net_code in sorted({s[0] for s in STATIONS}):
        codes = {c for n, c, *_ in STATIONS if n == net_code}
        networks.append(Network(
            code=net_code,
            stations=[s for s in inv_stations if s.code in codes]))
    inv = Inventory(networks=networks, source="salcha-synthetic-test")
    OUT.mkdir(parents=True, exist_ok=True)
    inv.write(str(OUT / "stations.xml"), format="STATIONXML")

    # ---- waveforms -------------------------------------------------------
    from obspy.taup import TauPyModel
    model = TauPyModel(model="iasp91")

    rng = np.random.default_rng(42)
    origin0 = UTCDateTime(MAINSHOCK["time"])
    origin_base = origin0 - 30.0  # traces start 30 s before the mainshock

    events = [(MAINSHOCK["latitude"], MAINSHOCK["longitude"],
               MAINSHOCK["depth_km"], MAINSHOCK["magnitude"], 0.0)]
    for _eid, dt, lat, lon, dep, mag in AFTERSHOCKS:
        events.append((lat, lon, dep, mag, dt))

    for k, (ev_lat, ev_lon, ev_dep, ev_mag, dt) in enumerate(events):
        stream = synth_event(model, rng, ev_lat, ev_lon, ev_dep, ev_mag,
                             origin_base)
        stream.write(str(RAW / f"event_{k:02d}.mseed"), format="MSEED")
        print(f"wrote event_{k:02d}  M{ev_mag}  +{dt:.0f}s  "
              f"{ev_lat:.4f},{ev_lon:.4f} d={ev_dep}")

    print(f"OK: {len(events)} events x {len(STATIONS)} stations -> {RAW}")
    print(f"Scenario: 2024 Salcha M{MAINSHOCK['magnitude']} sequence "
          f"(mainshock {MAINSHOCK['time']}, {MAINSHOCK['latitude']},"
          f"{MAINSHOCK['longitude']}, depth {MAINSHOCK['depth_km']} km)")


if __name__ == "__main__":
    main()
