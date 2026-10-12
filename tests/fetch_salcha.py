"""Fetch an alternative test dataset: real waveforms of the 2024 Salcha
(Alaska) M5.3 earthquake sequence from an FDSN data center.

The Salcha sequence was the default test dataset before 2025; the default is
now the 2025 M4.1 Templeton (California) sequence, see
tests/fetch_templeton.py.  This script is kept for the larger-regional-network
scenario and writes the same outputs, so it can be run instead.

Scenario (real USGS/AK catalog parameters):

  Mainshock : 2024-01-19 21:34:35 UTC, Mw 5.3, 64.4909N -146.9641E, 10.5 km
              (USGS event ak024vrx24o)
  Aftershocks: ~100 events M1.5-4.1 within ~6 km of the mainshock over
              2024-01-19 .. 2024-02-28; a representative subset is fetched.
  Stations  : AK-network stations operating around Fairbanks/Salcha in
              Jan-Feb 2024 (HDA, PS08, CCB, WRH, FA*, GIB, TAPE, UAFE)
              plus IU COLA.

For each event, ~3.5 minutes of 3-component waveform data around the origin
time are downloaded from EarthScope (IRIS) and written as one miniSEED file
per event, together with a StationXML inventory (with responses) and a
stations.csv.

Usage:
    python tests/fetch_salcha.py [--datacenter IRIS] [--n-events 6]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

OUT = Path(__file__).parent / "tmp_data"
RAW = OUT / "raw"

# ---------------------------------------------------------------------------
# Scenario parameters — 2024 Salcha, Alaska M5.3 sequence
# (recorded here so the run is reproducible)
# ---------------------------------------------------------------------------
# (event_id, origin UTC ISO, lat, lon, depth_km, magnitude)
EVENTS = [
    ("ak024vrx24o", "2024-01-19T21:34:35.071", 64.4909, -146.9641, 10.5, 5.3),
    ("ak024vrxr2h", "2024-01-19T21:37:59.753", 64.4764, -146.9235,  9.8, 3.7),
    ("ak024vt1bb3", "2024-01-19T23:22:16.286", 64.4880, -146.8929, 11.6, 3.4),
    ("ak024vskl4e", "2024-01-19T22:44:28.748", 64.4915, -146.9510,  7.3, 3.1),
    ("ak024x4qe39", "2024-01-20T03:33:38.464", 64.4841, -146.9384,  9.0, 2.8),
    ("ak024vs0xvj", "2024-01-19T21:52:45.997", 64.4890, -146.9111, 10.0, 2.5),
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

PRE_SECONDS = 30.0    # window start relative to origin
POST_SECONDS = 180.0  # window end relative to origin


def _dedupe_sensors(st):
    """Keep one 3-component sensor set per station.

    Some stations (e.g. IU.COLA) carry multiple sensor generations: BH* at
    location code 00 and HH* at location code 10 (and both again at 00).
    Requesting HH*,BH*,EH* then returns 6-12 channels per station instead
    of 3.  Keep a single 3-component set per station, preferring the
    primary location code (00) and the BH band (matches the AK stations).
    """
    from collections import defaultdict
    by_sta = defaultdict(list)
    for tr in st:
        by_sta[tr.stats.station].append(tr)

    def _score(tr):
        # prefer location 00, then BH band, then HH, then EH
        loc = 0 if tr.stats.location == "00" else 1
        band = {"B": 0, "H": 1, "E": 2}.get(tr.stats.channel[0], 3)
        return (loc, band)

    kept = []
    for sta, traces in by_sta.items():
        # group by (location, band) -> one 3-component set
        sets = defaultdict(list)
        for tr in traces:
            sets[(tr.stats.location, tr.stats.channel[0])].append(tr)
        best = min(sets, key=lambda k: _score(sets[k][0]))
        kept.extend(sets[best])
    return type(st)(kept)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--datacenter", default="IRIS",
                    help="FDSN service name (default IRIS / EarthScope)")
    ap.add_argument("--n-events", dest="n_events", type=int, default=6,
                    help=f"number of events to fetch (default 6 of {len(EVENTS)})")
    args = ap.parse_args()

    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client

    RAW.mkdir(parents=True, exist_ok=True)
    client = Client(args.datacenter)

    events = EVENTS[:max(1, args.n_events)]

    # ---- station metadata (with responses, needed for ML magnitudes) ------
    print("Downloading station metadata...")
    inv = client.get_stations(
        network=",".join(sorted({s[0] for s in STATIONS})),
        station=",".join(s[1] for s in STATIONS),
        channel="HH*,BH*,EH*",
        starttime=UTCDateTime(events[0][1]) - PRE_SECONDS,
        endtime=UTCDateTime(events[-1][1]) + POST_SECONDS,
        level="response")
    inv.write(str(OUT / "stations.xml"), format="STATIONXML")
    n_stations = sum(len(net) for net in inv)

    # stations.csv (same schema as fetch_waveforms.py --xml-to-csv)
    sys.path.insert(0, str(Path(__file__).parent.parent
                           / "seismic-workflow" / "scripts"))
    from common import save_stations_csv, stations_from_xml
    rows = stations_from_xml(OUT / "stations.xml")
    save_stations_csv(rows, OUT / "stations" / "stations.csv")

    # ---- event waveforms ---------------------------------------------------
    import time
    n_ok = 0
    catalog_rows = []
    for k, (eid, t_iso, lat, lon, dep, mag) in enumerate(events):
        t0 = UTCDateTime(t_iso) - PRE_SECONDS
        t1 = UTCDateTime(t_iso) + POST_SECONDS
        bulk = []
        for net, sta, *_ in STATIONS:
            # One combined pattern per station: "HH*,BH*,EH*" matches any of
            # the three families.  Requesting them as separate patterns would
            # duplicate channels for stations that carry multiple sensor
            # generations (e.g. IU.COLA has BH* at loc 00 and HH* at loc 10).
            bulk.append((net, sta, "*", "HH*,BH*,EH*", t0, t1))
        st = None
        for attempt in range(1, 4):  # retry up to 3 times (server hiccups)
            try:
                st = client.get_waveforms_bulk(bulk, attach_response=False)
                break
            except Exception as exc:
                print(f"[WARN] event {k} ({eid}): attempt {attempt} failed: "
                      f"{str(exc).splitlines()[0][:120]}")
                if attempt < 3:
                    time.sleep(5 * attempt)
                st = None
        if st is None or len(st) == 0:
            print(f"[WARN] event {k} ({eid}): no data after retries")
            continue
        st = _dedupe_sensors(st)
        st.merge(method=1, fill_value="interpolate")
        st.trim(starttime=t0, endtime=t1)
        path = RAW / f"event_{k:02d}.mseed"
        st.write(str(path), format="MSEED")
        n_ok += 1
        catalog_rows.append({
            "event_id": eid, "time": t_iso, "latitude": lat,
            "longitude": lon, "depth_km": dep, "magnitude": mag,
            "waveform_file": path.name, "n_traces": len(st)})
        print(f"wrote event_{k:02d}  M{mag}  {t_iso}  "
              f"{len(st)} traces ({eid})")

    # ---- reference catalog -------------------------------------------------
    import pandas as pd
    pd.DataFrame(catalog_rows).to_csv(OUT / "salcha_catalog.csv", index=False)

    print(f"OK: {n_ok}/{len(events)} events x {n_stations} stations -> {RAW}")
    print("Scenario: 2024 Salcha M5.3 sequence (real FDSN waveforms)")


if __name__ == "__main__":
    main()
