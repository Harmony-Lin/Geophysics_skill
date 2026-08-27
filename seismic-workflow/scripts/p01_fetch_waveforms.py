#!/usr/bin/env python3
"""Phase 1: waveform acquisition.

Two modes:
  A) FDSN download: --network/--station/--channel + time range
  B) Local scan:    --from-local <dir> (miniSEED/SAC)

Also converts StationXML to stations.csv via --xml-to-csv.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (emit, ensure_dir, fail, log, run_main, save_stations_csv,  # noqa: E402
                    stations_from_xml, warn)

WAVEFORM_EXTS = {".mseed", ".miniseed", ".seed", ".sac", ".msd"}


def xml_to_csv(args) -> None:
    rows = stations_from_xml(args.xml_to_csv)
    save_stations_csv(rows, args.out)
    emit({"status": "ok", "n_stations": len(rows), "stations_csv": str(args.out)})


def local_scan(args) -> None:
    src = Path(args.from_local)
    if not src.is_dir():
        fail(f"--from-local directory not found: {src}")
    out = ensure_dir(args.out)
    stations_out = ensure_dir(args.stations_out)

    files = [f for f in sorted(src.rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files ({', '.join(sorted(WAVEFORM_EXTS))}) under {src}")

    from obspy import Stream, read

    n_ok = 0
    net_sta = {}
    for f in files:
        try:
            st = read(str(f), headonly=True)
            for tr in st:
                key = (tr.stats.network, tr.stats.station)
                net_sta.setdefault(key, None)
            n_ok += 1
            # copy waveform into output dir
            dest = out / f.name
            if Path(f).resolve() != dest.resolve():
                shutil.copy2(f, dest)
        except Exception as exc:
            warn(f"unreadable file skipped: {f} ({exc})")

    # Build station metadata from inventory embedded in files when possible.
    inv_rows = []
    try:
        from obspy import read_inventory
        inv_files = list(src.rglob("*.xml")) + list(src.rglob("*.stationxml"))
        for xf in inv_files:
            try:
                inv_rows.extend(stations_from_xml(xf))
            except Exception as exc:
                warn(f"bad station XML skipped: {xf} ({exc})")
    except ImportError:
        pass

    if not inv_rows:
        # Fall back: derive station codes from filenames/traces without coords.
        inv_rows = [{
            "station": f"{net}.{sta}.",
            "network": net,
            "station_code": sta,
            "latitude": float("nan"),
            "longitude": float("nan"),
            "elevation_m": 0.0,
        } for net, sta in sorted(net_sta)]
        warn("no StationXML found; station coordinates are missing — "
             "provide a stations CSV before Phase 4/5")

    stations_csv = Path(stations_out) / "stations.csv"
    save_stations_csv(inv_rows, stations_csv)
    log(f"Copied/scanned {n_ok} waveform files into {out}")

    emit({
        "status": "ok",
        "mode": "local",
        "n_files": n_ok,
        "n_files_failed": len(files) - n_ok,
        "n_stations": len(inv_rows),
        "waveform_dir": str(out),
        "stations_csv": str(stations_csv),
    })


def fdsn_download(args) -> None:
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client
    from obspy.clients.fdsn.header import FDSNException

    client = Client(args.datacenter)
    t0, t1 = UTCDateTime(args.start), UTCDateTime(args.end)
    bulk = [(args.network, args.station, "*", args.channel, t0, t1)]

    out = ensure_dir(args.out)
    stations_out = ensure_dir(args.stations_out)

    log(f"Downloading waveforms from {args.datacenter} "
        f"({args.network}.{args.station}.{args.channel}, {t0} → {t1})...")
    try:
        st = client.get_waveforms_bulk(bulk, attach_response=True)
    except FDSNException as exc:
        fail(f"FDSN request failed: {exc}", hint="no data / network issue")
        return

    if len(st) == 0:
        fail("FDSN returned an empty stream",
             hint="no data: narrow the time window or station pattern")
        return

    # Split per-day chunks to keep files manageable
    n_files = 0
    merged = {}
    for tr in st:
        key = ".".join([tr.stats.network, tr.stats.station, tr.stats.channel])
        merged.setdefault(key, []).append(tr)
    for key, traces in merged.items():
        sub = type(st)(traces)
        path = out / f"{key.replace('.', '_')}_{t0.strftime('%Y%m%dT%H%M%S')}.mseed"
        sub.write(str(path), format="MSEED")
        n_files += 1

    log("Downloading station metadata...")
    try:
        inv = client.get_stations(network=args.network, station=args.station,
                                  channel=args.channel, starttime=t0, endtime=t1,
                                  level="response")
        inv_path = Path(stations_out) / "stations.xml"
        inv.write(str(inv_path), format="STATIONXML")
        rows = stations_from_xml(inv_path)
        save_stations_csv(rows, Path(stations_out) / "stations.csv")
        n_stations = len(rows)
    except Exception as exc:
        warn(f"station metadata download failed: {exc}")
        inv_path = None
        n_stations = 0

    emit({
        "status": "ok",
        "mode": "fdsn",
        "datacenter": args.datacenter,
        "n_traces": len(st),
        "n_files": n_files,
        "n_stations": n_stations,
        "waveform_dir": str(out),
        "stations_xml": str(inv_path) if inv_path else None,
        "stations_csv": str(Path(stations_out) / "stations.csv") if inv_path else None,
    })


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1: fetch waveforms (FDSN or local) and station metadata")
    parser.add_argument("--network", help="network code (FDSN mode)")
    parser.add_argument("--station", help="station code/pattern (FDSN mode)")
    parser.add_argument("--channel", default="HH*", help="channel pattern")
    parser.add_argument("--start", help="start time ISO8601")
    parser.add_argument("--end", help="end time ISO8601")
    parser.add_argument("--datacenter", default="IRIS", help="FDSN service name or URL")
    parser.add_argument("--from-local", dest="from_local", help="scan local waveform dir")
    parser.add_argument("--xml-to-csv", dest="xml_to_csv",
                        help="convert a StationXML file to stations.csv and exit")
    parser.add_argument("--out", default="data/raw", help="waveform output dir")
    parser.add_argument("--stations-out", dest="stations_out",
                        default="data/stations", help="station metadata output dir")
    args = parser.parse_args()

    if args.xml_to_csv:
        xml_to_csv(args)
    elif args.from_local:
        local_scan(args)
    elif args.network and args.start and args.end:
        fdsn_download(args)
    else:
        parser.error("specify either --network+--start+--end (FDSN), "
                     "--from-local <dir>, or --xml-to-csv <file>")


if __name__ == "__main__":
    run_main(main)
