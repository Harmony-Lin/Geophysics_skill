#!/usr/bin/env python3
"""Phase 1: waveform acquisition.

Modes:
  A) FDSN download: --network/--station/--channel + --start/--end
       - by station pattern:  --network N.AKIH --station '*'
       - by epicentral radius: --latitude/--longitude plus
         --min-radius/--max-radius (km by default, --radius-unit deg for raw
         FDSN degrees)
  B) Local scan:    --from-local <dir> (miniSEED/SAC)

Also converts StationXML to stations.csv via --xml-to-csv.

With --latitude/--longitude the station set is discovered from the station
webservices *first* (--datacenter may list several, comma-separated; first match
wins), and only those stations are requested.  The waveforms and
stations.xml/stations.csv therefore always describe the same stations, and both
stay inside the requested annulus.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (deg2km_factor, emit, ensure_dir, fail, haversine_km, log,  # noqa: E402
                    run_main, save_stations_csv, stations_from_xml, warn)

KM_PER_DEG = deg2km_factor()
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

    from obspy import read

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
    inv_files = list(src.rglob("*.xml")) + list(src.rglob("*.stationxml"))
    for xf in inv_files:
        try:
            inv_rows.extend(stations_from_xml(xf))
        except Exception as exc:
            warn(f"bad station XML skipped: {xf} ({exc})")

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


def _client(datacenter: str, timeout: float, attempts: int = 3):
    """FDSN client, retried a few times: service discovery is flaky in practice."""
    from obspy.clients.fdsn import Client
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return Client(datacenter, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                log(f"{datacenter}: FDSN client init failed ({exc}); retrying "
                    f"({attempt}/{attempts - 1})...")
                time.sleep(2.0 * attempt)
    raise last


def _parse_datacenters(spec) -> list:
    return [d.strip() for d in str(spec).split(",") if d.strip()]


def _km_to_deg(km: float) -> float:
    return float(km) / KM_PER_DEG


def _radius_km(args) -> tuple:
    """Requested annulus in km (FDSN itself wants degrees)."""
    factor = 1.0 if args.radius_unit == "km" else KM_PER_DEG
    return float(args.min_radius) * factor, float(args.max_radius) * factor


def _subset_inventory(inv, per_net: dict):
    """Inventory holding only the given {network: [station, ...]} selections.

    obspy's ``Inventory.select`` does not accept lists for ``station``, so the
    networks are copied and their station lists replaced directly.
    """
    from obspy import Inventory
    out = Inventory()
    for net in inv:
        wanted = per_net.get(net.code)
        if not wanted:
            continue
        wanted = set(wanted)
        keep = [sta for sta in net if sta.code in wanted]
        if not keep:
            continue
        sub = net.copy()
        sub.stations = keep
        out += sub
    return out


def _station_distances_km(inv, lat: float, lon: float) -> dict:
    """{(network, station): epicentral distance in km} for an Inventory."""
    import numpy as np
    keys, slat, slon = [], [], []
    for net in inv:
        for sta in net:
            if sta.latitude is None or sta.longitude is None:
                continue
            keys.append((net.code, sta.code))
            slat.append(sta.latitude)
            slon.append(sta.longitude)
    if not keys:
        return {}
    dist = np.atleast_1d(haversine_km(lat, lon, slat, slon))
    return {k: float(d) for k, d in zip(keys, dist)}


def _annulus_selection(inv, args):
    """Restrict an inventory to min_km <= epicentral distance <= max_km."""
    dist = _station_distances_km(inv, args.latitude, args.longitude)
    lo, hi = args.min_radius_km, args.max_radius_km
    per_net = {}
    for (net_code, sta_code), d in dist.items():
        if lo <= d <= hi:
            per_net.setdefault(net_code, []).append(sta_code)
    return _subset_inventory(inv, per_net), dist


def _discover_stations(args, t0, t1, datacenters):
    """Ask every datacentre for the stations inside the requested annulus.

    Returns (inventory, {datacentre: inventory}, distances, source_of, stats),
    uncapped: the nearest-N trim is applied separately by ``_cap_discovery``
    so it can be relaxed when the capped set turns out to have no data.
    A station is claimed by the first datacentre that reports it, so no channel
    is downloaded twice.
    """
    from obspy import Inventory

    # Ask the server for a slightly larger annulus and filter exactly in km
    # below, so the server's degree rounding can never clip a station away.
    min_deg = _km_to_deg(args.min_radius_km)
    max_deg = _km_to_deg(args.max_radius_km) * 1.01 + 0.01

    inv_all = Inventory()
    per_dc = {}
    claimed = set()
    dist_all = {}
    source_of = {}
    stats = []

    for dc in datacenters:
        try:
            client = _client(dc, args.timeout)
        except Exception as exc:  # noqa: BLE001
            warn(f"{dc}: cannot initialise FDSN client ({exc})")
            stats.append({"datacenter": dc, "error": f"client init: {exc}"})
            continue
        if "station" not in client.services:
            warn(f"{dc}: no FDSN station service — skipped")
            stats.append({"datacenter": dc, "error": "no station service"})
            continue

        try:
            inv = client.get_stations(
                network=args.network, station=args.station, location=args.location,
                channel=args.channel, starttime=t0, endtime=t1,
                level=args.station_level,
                latitude=args.latitude, longitude=args.longitude,
                minradius=min_deg, maxradius=max_deg)
        except Exception as exc:  # noqa: BLE001
            warn(f"{dc}: station query failed ({exc})")
            stats.append({"datacenter": dc, "error": f"station query: {exc}"})
            continue

        inv, dist = _annulus_selection(inv, args)
        keep = {}
        for net in inv:
            for sta in net:
                if (net.code, sta.code) not in claimed:
                    keep.setdefault(net.code, []).append(sta.code)
        inv = _subset_inventory(inv, keep)
        n_stations = sum(len(net) for net in inv)
        n_channels = sum(len(sta) for net in inv for sta in net)
        stats.append({"datacenter": dc, "n_stations": n_stations,
                      "n_channels": n_channels})
        if n_stations == 0:
            log(f"{dc}: no unclaimed station inside the annulus")
            continue
        for net in inv:
            for sta in net:
                key = (net.code, sta.code)
                claimed.add(key)
                dist_all.setdefault(key, dist.get(key))
                source_of[key] = dc
        per_dc[dc] = inv
        inv_all += inv
        log(f"{dc}: {n_stations} station(s) / {n_channels} channel(s)")

    return inv_all, per_dc, dist_all, source_of, stats


def _cap_discovery(per_dc, dist_all, max_stations):
    """Trim a discovery to its ``max_stations`` nearest stations.

    Returns (per_datacentre inventories, distances, combined inventory).
    ``max_stations`` of None/0 means "keep everything".
    """
    from obspy import Inventory

    if not max_stations or len(dist_all) <= max_stations:
        trimmed, kept_dist = per_dc, dist_all
    else:
        nearest = sorted(dist_all, key=lambda k: (dist_all[k] is None, dist_all[k]))
        keep = set(nearest[:max_stations])
        trimmed = {}
        for dc, inv in per_dc.items():
            per_net = {}
            for net in inv:
                for sta in net:
                    if (net.code, sta.code) in keep:
                        per_net.setdefault(net.code, []).append(sta.code)
            if per_net:
                trimmed[dc] = _subset_inventory(inv, per_net)
        kept_dist = {k: v for k, v in dist_all.items() if k in keep}

    inv = Inventory()
    for sub in trimmed.values():
        inv += sub
    return trimmed, kept_dist, inv


def _fallback_caps(max_stations, n_available: int) -> list:
    """Increasing station caps to try when a capped selection has no data.

    The cap only knows about *distance*, not about which channels are actually
    archived (high-gain accelerometer channels in particular often are not), so
    the nearest N stations can come back empty while data exists further out.
    Bounded deliberately: at most 4x the requested cap, to keep a large-radius
    query from turning into a multi-gigabyte download.
    """
    if not max_stations or n_available <= max_stations:
        return [None]
    ladder, cap = [], max_stations
    while cap < n_available and len(ladder) < 3:
        ladder.append(cap)
        cap *= 2
    if cap < n_available:
        ladder.append(cap)              # 4x the requested cap, still bounded
    elif n_available not in ladder:
        ladder.append(n_available)      # the whole annulus is small enough
    return ladder


def _bulk_from_discovery(per_dc, t0, t1) -> dict:
    """{datacentre: [(network, station, location, channel, t0, t1), ...]}"""
    bulk_by_dc = {}
    for dc, inv in per_dc.items():
        seen, entries = set(), []
        for net in inv:
            for sta in net:
                for cha in sta:
                    key = (net.code, sta.code, cha.location_code or "", cha.code)
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(key + (t0, t1))
        if entries:
            bulk_by_dc[dc] = entries
    return bulk_by_dc


def _download_bulk(bulk_by_dc, args):
    """Download every datacentre's bulk request; failures are reported, not fatal."""
    from obspy import Stream

    st = Stream()
    failures = []
    for dc, entries in bulk_by_dc.items():
        log(f"{dc}: requesting {len(entries)} channel(s) of waveform data...")
        try:
            client = _client(dc, args.timeout)
            sub = client.get_waveforms_bulk(entries)
        except Exception as exc:  # noqa: BLE001 - one bad datacentre must not abort
            warn(f"{dc}: waveform request failed ({exc})")
            failures.append({"datacenter": dc, "error": str(exc),
                             "n_channels": len(entries)})
            continue
        log(f"{dc}: {len(sub)} trace(s) received")
        st += sub
    return st, failures


def _attach_responses(st, inv) -> int:
    """Attach instrument responses locally; returns the number of traces with one.

    ``Stream.attach_response`` is deprecated in obspy 1.5, so the response is
    looked up per trace instead. Missing metadata is not fatal.
    """
    n_ok, n_missing = 0, 0
    for tr in st:
        if tr.stats.get("response") is not None:
            n_ok += 1
            continue
        try:
            tr.stats.response = inv.get_response(tr.id, tr.stats.starttime)
            n_ok += 1
        except Exception:  # noqa: BLE001 - no metadata for this channel
            n_missing += 1
    if n_missing:
        warn(f"no response metadata for {n_missing} trace(s)")
    return n_ok


def _write_waveforms(st, out: Path, t0) -> int:
    merged = {}
    for tr in st:
        key = ".".join([tr.stats.network, tr.stats.station,
                        tr.stats.location or "", tr.stats.channel])
        merged.setdefault(key, []).append(tr)
    for key, traces in sorted(merged.items()):
        sub = type(st)(traces)
        path = out / f"{key.replace('.', '_')}_{t0.strftime('%Y%m%dT%H%M%S')}.mseed"
        sub.write(str(path), format="MSEED")
    return len(merged)


def _write_station_metadata(inv, stations_out: Path):
    inv_path = Path(stations_out) / "stations.xml"
    inv.write(str(inv_path), format="STATIONXML")
    rows = stations_from_xml(inv_path)
    csv_path = Path(stations_out) / "stations.csv"
    save_stations_csv(rows, csv_path)
    return inv_path, csv_path, len(rows)


def fdsn_download(args) -> None:
    from obspy import UTCDateTime

    t0, t1 = UTCDateTime(args.start), UTCDateTime(args.end)
    out = ensure_dir(args.out)
    stations_out = ensure_dir(args.stations_out)
    datacenters = _parse_datacenters(args.datacenter) or ["IRIS"]
    radius_mode = args.latitude is not None

    stats, dist_all, source_of, failures = [], {}, {}, []
    inv = None
    st, bulk_by_dc = None, {}
    if radius_mode:
        args.min_radius_km, args.max_radius_km = _radius_km(args)
        if args.max_radius_km <= args.min_radius_km:
            fail("--max-radius must be larger than --min-radius",
                 hint=f"got min={args.min_radius} max={args.max_radius} "
                      f"({args.radius_unit})")
        log(f"Searching stations within {args.min_radius_km:.1f}-"
            f"{args.max_radius_km:.1f} km of ({args.latitude}, {args.longitude}) "
            f"via {', '.join(datacenters)}...")
        _, per_dc, dist_all, source_of, stats = _discover_stations(
            args, t0, t1, datacenters)
        if not per_dc:
            fail("no station found inside the requested annulus",
                 hint=f"widen --max-radius (currently {args.max_radius} "
                      f"{args.radius_unit}), relax --channel "
                      f"(currently {args.channel}) or try another --datacenter",
                 epicenter={"latitude": args.latitude, "longitude": args.longitude},
                 radius_km={"min": args.min_radius_km, "max": args.max_radius_km},
                 datacenter_stats=stats)
            return

        # The nearest-N trim is a guess about distance, not about data: relax it
        # step by step before declaring the annulus empty.
        caps = _fallback_caps(args.max_stations, len(dist_all))
        used_cap = None
        for idx, cap in enumerate(caps):
            if idx:
                warn(f"the {caps[idx - 1]} nearest station(s) returned no data; "
                     f"widening to {cap if cap else 'all'} station(s) of "
                     f"{len(dist_all)}...")
            elif cap and len(dist_all) > cap:
                warn(f"capping to the {cap} nearest station(s) of "
                     f"{len(dist_all)} discovered")
            per_dc_c, dist_c, inv_c = _cap_discovery(per_dc, dist_all, cap)
            bulk_by_dc = _bulk_from_discovery(per_dc_c, t0, t1)
            st, failures = _download_bulk(bulk_by_dc, args)
            if len(st):
                inv, dist_all = inv_c, dist_c
                used_cap = cap
                break
    else:
        bulk_by_dc = {datacenters[0]: [
            (args.network, args.station, args.location, args.channel, t0, t1)]}
        log(f"Downloading waveforms from {datacenters[0]} "
            f"({args.network}.{args.station}.{args.location}.{args.channel}, {t0} → {t1})...")
        st, failures = _download_bulk(bulk_by_dc, args)

    if len(st) == 0:
        hint = ("no data: narrow the time window or station pattern, or widen "
                "the radius")
        if radius_mode and args.max_stations and caps[-1]:
            hint += (f"; tried up to {caps[-1]} nearest station(s) — raise "
                     f"--max-stations or drop --max-stations to use all "
                     f"{len(dist_all)} discovered")
        fail("FDSN returned an empty stream",
             hint=hint,
             request_failures=failures, datacenter_stats=stats)
        return

    requested = {(n, s, loc or "", c)
                 for entries in bulk_by_dc.values() for (n, s, loc, c, _, _) in entries}
    delivered = {(tr.stats.network, tr.stats.station, tr.stats.location or "",
                  tr.stats.channel) for tr in st}
    missing = sorted(".".join(k) for k in requested - delivered)

    # Station metadata: reuse the discovery inventory in radius mode (it is by
    # construction the same station set as the waveforms).
    if inv is None:
        log("Downloading station metadata...")
        try:
            inv = _client(datacenters[0], args.timeout).get_stations(
                network=args.network, station=args.station, location=args.location,
                channel=args.channel, starttime=t0, endtime=t1,
                level=args.station_level)
        except Exception as exc:  # noqa: BLE001
            warn(f"station metadata download failed: {exc}")
            inv = None

    n_with_response = _attach_responses(st, inv) if inv is not None else 0
    n_files = _write_waveforms(st, out, t0)
    if inv is not None:
        inv_path, csv_path, n_stations = _write_station_metadata(inv, stations_out)
    else:
        inv_path = csv_path = None
        n_stations = 0

    dl_dist = [dist_all[k] for k in dist_all
               if k in {(tr.stats.network, tr.stats.station) for tr in st}
               and dist_all[k] is not None]
    n_dl_stations = len({(tr.stats.network, tr.stats.station) for tr in st})
    payload = {
        "status": "ok",
        "mode": "fdsn",
        "datacenter": args.datacenter,
        "datacenters": datacenters,
        "start": str(t0),
        "end": str(t1),
        "channel": args.channel,
        "n_traces": len(st),
        "n_files": n_files,
        "n_stations": n_stations,
        "n_stations_downloaded": n_dl_stations,
        "n_stations_requested": len({(n, s) for (n, s, _, _) in requested}),
        "n_channels_requested": len(requested),
        "n_channels_downloaded": len(delivered),
        "n_missing_channels": len(missing),
        "missing_channels": missing[:20],
        "n_traces_with_response": n_with_response,
        "waveform_dir": str(out),
        "stations_xml": str(inv_path) if inv_path else None,
        "stations_csv": str(csv_path) if csv_path else None,
    }
    if radius_mode:
        nearest = sorted(dist_all.items(), key=lambda kv: (kv[1] is None, kv[1]))
        payload.update({
            "epicenter": {"latitude": args.latitude, "longitude": args.longitude},
            "radius_km": {"min": args.min_radius_km, "max": args.max_radius_km},
            "radius_unit": args.radius_unit,
            "max_stations_requested": args.max_stations,
            "max_stations_used": used_cap,
            "distance_km": ({"min": round(min(dl_dist), 2), "max": round(max(dl_dist), 2)}
                            if dl_dist else None),
            "datacenter_stats": stats,
            "nearest_stations": [
                {"network": n, "station": s, "distance_km": None if d is None else round(d, 2),
                 "datacenter": source_of.get((n, s))}
                for (n, s), d in nearest[:10]],
        })
    if failures:
        payload["request_failures"] = failures
    if missing:
        warn(f"{len(missing)} requested channel(s) returned no data "
             f"(e.g. {', '.join(missing[:5])})")
    emit(payload)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1: fetch waveforms (FDSN or local) and station metadata")
    grp = parser.add_argument_group("selection")
    grp.add_argument("--network", help="network code/pattern (FDSN mode)")
    grp.add_argument("--station", help="station code/pattern (FDSN mode)")
    grp.add_argument("--location", default="*", help="location code/pattern (FDSN)")
    grp.add_argument("--channel", default="BH*,HH*", help="channel pattern")
    grp = parser.add_argument_group("time window")
    grp.add_argument("--start", help="start time ISO8601")
    grp.add_argument("--end", help="end time ISO8601")
    grp = parser.add_argument_group(
        "epicentral radius selection (alternative to --network/--station)")
    grp.add_argument("--latitude", type=float, help="epicentre latitude (deg N)")
    grp.add_argument("--longitude", type=float, help="epicentre longitude (deg E)")
    grp.add_argument("--min-radius", dest="min_radius", type=float, default=0.0,
                     help="minimum epicentral distance (default 0)")
    grp.add_argument("--max-radius", dest="max_radius", type=float, default=300.0,
                     help="maximum epicentral distance (default 300)")
    grp.add_argument("--radius-unit", dest="radius_unit", choices=("km", "deg"),
                     default="km", help="unit of --min-radius/--max-radius "
                                        "(default km; FDSN itself uses degrees)")
    grp.add_argument("--max-stations", dest="max_stations", type=int,
                     help="keep only the N nearest stations (large radii can "
                          "match thousands of channels)")
    grp = parser.add_argument_group("services and output")
    grp.add_argument("--datacenter", default="IRIS",
                     help="FDSN service name/URL, comma-separated for fallback "
                          "(e.g. 'NCEDC,SCEDC,IRIS')")
    grp.add_argument("--station-level", dest="station_level", default="response",
                     choices=("station", "channel", "response"),
                     help="StationXML detail level (default response, needed by "
                          "p02 --remove-response)")
    grp.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout (s)")
    grp.add_argument("--from-local", dest="from_local", help="scan local waveform dir")
    grp.add_argument("--xml-to-csv", dest="xml_to_csv",
                     help="convert a StationXML file to stations.csv and exit")
    grp.add_argument("--out", default="data/raw", help="waveform output dir")
    grp.add_argument("--stations-out", dest="stations_out",
                     default="data/stations", help="station metadata output dir")
    args = parser.parse_args()

    if (args.latitude is None) != (args.longitude is None):
        parser.error("--latitude and --longitude must be given together")

    if args.xml_to_csv:
        xml_to_csv(args)
    elif args.from_local:
        local_scan(args)
    elif args.latitude is not None and args.start and args.end:
        fdsn_download(args)
    elif args.network and args.start and args.end:
        fdsn_download(args)
    else:
        parser.error("specify either --network+--start+--end, "
                     "--latitude+--longitude+--start+--end (radius selection), "
                     "--from-local <dir>, or --xml-to-csv <file>")


if __name__ == "__main__":
    run_main(main)
