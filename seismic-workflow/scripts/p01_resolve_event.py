#!/usr/bin/env python3
"""Phase 1a: resolve a partial earthquake description into an authoritative
event record plus a ready-to-run waveform download plan.

Phase 1 is split into two deterministic steps:

  p01_resolve_event.py    (this script)
      partial constraints (place, approximate time, name, event id, ...)
        -> FDSN event-catalogue queries (USGS / ISC / EMSC)
        -> de-duplicated candidate events
        -> either ONE authoritative event + a download plan, or an explicit
           `resolution` telling the agent that it must ask the user.

  p01_fetch_waveforms.py
      executes that plan and writes miniSEED waveforms + StationXML.

Translating *free text* ("2024 年能登半岛地震") into catalogue constraints is
the agent's job (web search + the tables in SKILL.md); everything below is
deterministic and reproducible.  The script never guesses silently: with too
little input it returns resolution="insufficient_input" and when several
distinct events still match it returns resolution="ambiguous".  In both cases
`needs_user_input` is true and `question` holds the text the agent must put to
the user.

Output (stdout, JSON):
  status, resolution, needs_user_input, query, event, download_plan, candidates
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (deg2km_factor, emit, ensure_dir, fail, haversine_km,  # noqa: E402
                    log, run_main, warn)

KM_PER_DEG = deg2km_factor()

#: Event catalogues queried in this order.  The first one that returns a record
#: owns the identity of the event; the others merely confirm it (`sources`).
EVENT_DATACENTERS = ("USGS", "ISC", "EMSC")

#: (pre_seconds, post_seconds) relative to the origin time.  A negative
#: post_seconds means "end before the origin" (used for pre-event noise).
MODE_WINDOWS = {
    "mainshock": (60.0, 300.0),
    "aftershock": (86400.0, 7.0 * 86400.0),
    "noise": (7.0 * 86400.0, -86400.0),
}

DEFAULT_WITHIN_KM = 150.0

#: (name, lat_min, lat_max, lon_min, lon_max, download datacentres, network hint)
#: Used only for *suggestions* in the download plan: station selection in Phase
#: 1b is driven by the epicentral radius, so an imperfect hint can never drop a
#: usable station.
REGIONS = (
    ("Japan", 24.0, 46.5, 122.0, 148.0, ("IRIS",), ("JP", "JU", "VU", "XJ")),
    ("Taiwan", 21.0, 26.5, 119.0, 123.5, ("IRIS",), ("TW", "TA", "TP")),
    ("Chinese mainland and surroundings", 17.0, 54.0, 73.0, 136.0,
     ("IRIS",), ("CB", "KC", "G", "II")),
    ("Europe and the Mediterranean", 33.0, 72.0, -13.0, 42.0,
     ("EIDA", "IRIS"), ("GE", "GR", "HT", "IV", "MN", "Z3")),
    ("New Zealand", -48.0, -33.0, 165.0, 180.0, ("GEONET", "IRIS"), ("NZ", "IU")),
    ("Alaska", 50.0, 72.0, -180.0, -125.0, ("IRIS",), ("AK", "AT", "TA")),
    ("Western United States", 30.0, 50.0, -126.0, -100.0,
     ("NCEDC", "SCEDC", "IRIS"), ("BK", "CI", "NC", "NN", "PG", "TA")),
    ("South America", -56.0, 14.0, -82.0, -34.0, ("IRIS",), ("C", "GT", "BR", "IU")),
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _iso(t) -> str:
    """UTCDateTime -> 'YYYY-MM-DDTHH:MM:SS.sssZ' (repo-wide time convention)."""
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc(value: str):
    from obspy import UTCDateTime
    try:
        return UTCDateTime(value)
    except Exception as exc:
        fail(f"cannot parse --start/--end value {value!r} as ISO8601 ({exc})",
             hint="use e.g. 2024-01-01T07:10:00Z")
        raise  # pragma: no cover - fail() exits


def _event_id(ev) -> str:
    """Extract the catalogue-local event id from a resource id.

    'quakeml:earthquake.usgs.gov/fdsnws/event/1/query?eventid=nc75265261' -> nc75265261
    'smi:ISC/evid=644657431'                                             -> 644657431
    'quakeml:eu.emsc/event/20251118_0000316'                             -> 20251118_0000316
    """
    rid = str(ev.resource_id.id or "")
    for key in ("eventid=", "evid="):
        if key in rid:
            return rid.split(key, 1)[1].split("&")[0].strip()
    return rid.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] or rid


def _place(ev):
    """Human-readable place string (obspy exposes it via event_descriptions)."""
    for desc in getattr(ev, "event_descriptions", None) or []:
        text = getattr(desc, "text", None)
        if text:
            return text
    origin = ev.preferred_origin() or (ev.origins[0] if ev.origins else None)
    return getattr(origin, "region", None)


def _record(ev, datacenter: str):
    """Flatten one obspy Event into a plain dict (or None if unusable)."""
    origin = ev.preferred_origin() or (ev.origins[0] if ev.origins else None)
    if origin is None or origin.latitude is None or origin.longitude is None:
        return None
    mag = ev.preferred_magnitude() or (ev.magnitudes[0] if ev.magnitudes else None)
    mag_value = getattr(mag, "mag", None) if mag is not None else None
    return {
        # obspy notes: origin.depth is in METRES, magnitudes carry `.mag`.
        "event_id": _event_id(ev),
        "origin_time": _iso(origin.time),
        "latitude": round(float(origin.latitude), 6),
        "longitude": round(float(origin.longitude), 6),
        "depth_km": None if origin.depth is None else round(float(origin.depth) / 1000.0, 3),
        "magnitude": None if mag_value is None else round(float(mag_value), 3),
        "magnitude_type": getattr(mag, "magnitude_type", None) if mag is not None else None,
        "place": _place(ev),
        "event_type": ev.event_type,
        "datacenter": datacenter,
        "sources": [datacenter],
        "resource_id": str(ev.resource_id.id or ""),
    }


# ---------------------------------------------------------------------------
# catalogue queries
# ---------------------------------------------------------------------------
def _call(client, kw: dict):
    """Run one FDSN event query.  Returns (catalog_or_None, error_or_None).

    Some nodes reject `orderby`; that is retried transparently.  `FDSNNoData`
    is not an error (None, None) so callers can distinguish "no match" from
    "service failure".
    """
    from obspy.clients.fdsn.header import FDSNException, FDSNNoDataException

    try:
        return client.get_events(**kw), None
    except FDSNNoDataException:
        return None, None
    except FDSNException as exc:
        if "orderby" in kw:
            kw = {k: v for k, v in kw.items() if k != "orderby"}
            warn(f"retrying without orderby ({exc})")
            try:
                return client.get_events(**kw), None
            except FDSNNoDataException:
                return None, None
            except Exception as exc2:  # noqa: BLE001
                return None, f"{type(exc2).__name__}: {exc2}"
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - network hiccups must not kill the run
        return None, f"{type(exc).__name__}: {exc}"


def _query_datacenter(name: str, args, t0, t1, radius_deg):
    from obspy.clients.fdsn import Client

    try:
        client = Client(name, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - any client init problem is a skip
        warn(f"{name}: cannot initialise FDSN client ({exc})")
        return []
    if "event" not in client.services:
        warn(f"{name}: no FDSN event service — skipped")
        return []

    kw = {"limit": args.limit}
    if args.event_id:
        kw["eventid"] = args.event_id
    else:
        # Rank by magnitude so `--limit` keeps the largest events of a busy window.
        kw["orderby"] = "magnitude"
    if t0 is not None:
        kw["starttime"] = t0
    if t1 is not None:
        kw["endtime"] = t1
    if args.min_magnitude is not None:
        kw["minmagnitude"] = args.min_magnitude
    if args.max_magnitude is not None:
        kw["maxmagnitude"] = args.max_magnitude
    if args.latitude is not None:
        kw.update(latitude=args.latitude, longitude=args.longitude,
                  minradius=0.0, maxradius=radius_deg)
    if args.catalog:
        kw["catalog"] = args.catalog

    cat, err = _call(client, kw)
    if err and "eventid" in kw and (t0 is not None or t1 is not None):
        # e.g. the ISC client cannot parse the by-id answer; a window query on
        # the same catalogue still gives its authoritative solution.
        warn(f"{name}: id query failed ({err}); retrying with the time window")
        fallback = {k: v for k, v in kw.items() if k != "eventid"}
        fallback["orderby"] = "magnitude"
        cat, err = _call(client, fallback)
    if err:
        warn(f"{name}: event query failed ({err})")
        return []
    if cat is None:
        log(f"{name}: no event matches the query")
        return []

    log(f"{name}: {len(cat)} candidate event(s)")
    return [r for r in (_record(ev, name) for ev in cat) if r]


def _diff_seconds(a, b) -> float:
    from obspy import UTCDateTime
    return abs(float(UTCDateTime(a["origin_time"]) - UTCDateTime(b["origin_time"])))


def _dist_km(a, b) -> float:
    return float(haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]))


def _cluster(records, km: float, seconds: float):
    """Group catalogue records that describe the same physical earthquake."""
    from obspy import UTCDateTime

    clusters: list[list[dict]] = []
    for rec in records:
        for cl in clusters:
            ref = cl[0]
            if _diff_seconds(rec, ref) <= seconds and _dist_km(rec, ref) <= km:
                cl.append(rec)
                break
        else:
            clusters.append([rec])
    for cl in clusters:
        cl[0]["sources"] = [r["datacenter"] for r in cl]
        mags = [r["magnitude"] for r in cl if r["magnitude"] is not None]
        cl[0]["magnitude_spread"] = (
            round(max(mags) - min(mags), 3) if len(mags) > 1 else None)
        times = [float(UTCDateTime(r["origin_time"])) for r in cl]
        cl[0]["time_spread_s"] = round(max(times) - min(times), 3) if len(times) > 1 else None
    return clusters


def _cluster_mag(cl) -> float | None:
    mags = [r["magnitude"] for r in cl if r["magnitude"] is not None]
    return max(mags) if mags else None


def _mag_key(cl):
    m = _cluster_mag(cl)
    return (m is not None, float("-inf") if m is None else m)


def _resolve(clusters, args):
    """Return (event, resolution, reason, needs_user_input)."""
    if args.select:
        wanted = args.select.strip().lower()
        for cl in clusters:
            for rec in cl:
                if rec["event_id"].lower() == wanted:
                    return rec, "resolved", f"matched --select {args.select}", False
        return (None, "not_found",
                f"--select {args.select!r} is not among the {len(clusters)} candidate event(s)",
                True)
    if not clusters:
        return None, "not_found", "no event matched the query in " + "/".join(EVENT_DATACENTERS), True
    if len(clusters) == 1:
        return clusters[0][0], "resolved", "unique candidate", False

    best = max(clusters, key=_mag_key)
    others = [c for c in clusters if c is not best]
    best_m = _cluster_mag(best)
    if not args.strict and best_m is not None:
        runner = max(others, key=_mag_key)
        runner_m = _cluster_mag(runner)
        if runner_m is not None and best_m - runner_m >= args.mag_tolerance:
            return (best[0], "resolved",
                    f"largest of {len(clusters)} nearby events "
                    f"(M{best_m:g} vs M{runner_m:g}, ΔM≥{args.mag_tolerance:g})",
                    True)
    return (None, "ambiguous",
            f"{len(clusters)} distinct events match the query and none dominates "
            f"by M≥{args.mag_tolerance:g}", True)


# ---------------------------------------------------------------------------
# download plan
# ---------------------------------------------------------------------------
def _region(lat: float, lon: float) -> dict:
    for name, lat0, lat1, lon0, lon1, dcs, nets in REGIONS:
        if lat0 <= lat <= lat1 and lon0 <= lon <= lon1:
            return {"name": name, "download_datacenters": list(dcs),
                    "network_hint": list(nets)}
    return {"name": "global", "download_datacenters": ["IRIS"], "network_hint": []}


def _radius_for_magnitude(mag) -> float:
    """Initial epicentral search radius in km (mainshock regional recordings)."""
    if mag is None:
        return 300.0
    if mag < 4.0:
        return 100.0
    if mag < 5.5:
        return 200.0
    if mag < 7.0:
        return 500.0
    return 1000.0


def _download_plan(event: dict, args) -> dict:
    from obspy import UTCDateTime

    origin = UTCDateTime(event["origin_time"])
    pre, post = MODE_WINDOWS[args.mode]
    if args.pre_seconds is not None:
        pre = args.pre_seconds
    if args.post_seconds is not None:
        post = args.post_seconds
    start, end = origin - pre, origin + post
    if end <= start:
        fail(f"degenerate time window ({_iso(start)} -> {_iso(end)})",
             hint="check --mode / --pre-seconds / --post-seconds")

    region = _region(event["latitude"], event["longitude"])
    if args.max_radius_km is not None:
        radius, radius_src = args.max_radius_km, "user supplied (--max-radius-km)"
    elif event["magnitude"] is not None:
        radius = _radius_for_magnitude(event["magnitude"])
        radius_src = f"estimated from M{event['magnitude']:g}"
    else:
        radius, radius_src = _radius_for_magnitude(None), "default (magnitude unknown)"

    datacenters = ([d.strip() for d in args.download_datacenter.split(",") if d.strip()]
                   if args.download_datacenter else region["download_datacenters"])

    argv = [
        "python", "scripts/p01_fetch_waveforms.py",
        "--network", "*", "--station", "*", "--channel", args.channel,
        "--start", _iso(start), "--end", _iso(end),
        "--latitude", f"{event['latitude']:.6f}",
        "--longitude", f"{event['longitude']:.6f}",
        "--min-radius", "0", "--max-radius", f"{radius:.0f}",
        "--datacenter", ",".join(datacenters),
        "--out", args.raw_out, "--stations-out", args.stations_out,
    ]
    if args.max_stations is not None:
        argv += ["--max-stations", str(args.max_stations)]

    plan = {
        "mode": args.mode,
        "start": _iso(start),
        "end": _iso(end),
        "window_seconds": {"pre": pre, "post": post},
        "latitude": event["latitude"],
        "longitude": event["longitude"],
        "min_radius_km": 0.0,
        "max_radius_km": float(radius),
        "radius_source": radius_src,
        "channel": args.channel,
        "datacenters": datacenters,
        "region": region,
        "argv": argv,
        "command": " ".join(shlex.quote(a) for a in argv),
    }
    return plan


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _question_for(resolution: str, args, clusters) -> str:
    if resolution == "insufficient_input":
        return ("请补充这次地震的更多信息：发震日期/时间（UTC，或当地时间）"
                "、震中位置或地名，以及事件编号（若有）。"
                "也可以直接给出 USGS/EMSC/ISC 的事件编号。")
    if resolution == "not_found":
        return ("候选中没有匹配的事件。请确认时间、位置或事件编号是否正确，"
                "或者换一个地区/更宽的时间窗口（必要时用 --start/--end 显式指定）。")
    if resolution == "ambiguous":
        lines = ["匹配到多个不同地震，请确认是哪一次："]
        for cl in clusters:
            ev = cl[0]
            mag = "M?" if ev["magnitude"] is None else f"M{ev['magnitude']:g}"
            lines.append(f"- {ev['origin_time']}  {mag}  "
                         f"({ev['latitude']:.3f}, {ev['longitude']:.3f})  "
                         f"{ev['place'] or ''}  id={ev['event_id']}")
        lines.append("回复其中一条（或把 id 传给 --select）即可继续。")
        return "\n".join(lines)
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1a: resolve an earthquake description into an "
                    "authoritative event record + waveform download plan "
                    "(Phase 1b executes the plan)")
    grp = parser.add_argument_group("event identity (at least one is required)")
    grp.add_argument("--event-id", dest="event_id",
                     help="catalogue event id (e.g. nc75265261, us6000jllz)")
    grp.add_argument("--start", help="search window start, ISO8601 (>= origin time)")
    grp.add_argument("--end", help="search window end, ISO8601")
    grp = parser.add_argument_group("event filters")
    grp.add_argument("--latitude", type=float, help="search centre latitude (deg N)")
    grp.add_argument("--longitude", type=float, help="search centre longitude (deg E)")
    grp.add_argument("--within-km", dest="within_km", type=float,
                     help=f"search radius around --latitude/--longitude "
                          f"(default {DEFAULT_WITHIN_KM:g} km)")
    grp.add_argument("--min-magnitude", dest="min_magnitude", type=float,
                     help="lower magnitude bound passed to the catalogue")
    grp.add_argument("--max-magnitude", dest="max_magnitude", type=float,
                     help="upper magnitude bound passed to the catalogue")
    grp.add_argument("--catalog", help="restrict to a catalogue (e.g. 'ISC', 'GCMT')")
    grp.add_argument("--limit", type=int, default=20,
                     help="max candidate events per datacentre (default 20)")
    grp = parser.add_argument_group("resolution policy")
    grp.add_argument("--select", help="pick this event id from the candidates")
    grp.add_argument("--strict", action="store_true",
                     help="never auto-pick the largest event when several match")
    grp.add_argument("--mag-tolerance", dest="mag_tolerance", type=float, default=0.6,
                     help="ΔM required to auto-pick the largest of several "
                          "candidates (default 0.6)")
    grp.add_argument("--cluster-km", dest="cluster_km", type=float, default=30.0,
                     help="candidates closer than this are the same event (default 30)")
    grp.add_argument("--cluster-seconds", dest="cluster_seconds", type=float, default=60.0,
                     help="candidates closer in time than this are the same event "
                          "(default 60)")
    grp = parser.add_argument_group("download plan")
    grp.add_argument("--mode", choices=sorted(MODE_WINDOWS), default="mainshock",
                     help="waveform window preset (default mainshock: -60s/+300s)")
    grp.add_argument("--pre-seconds", dest="pre_seconds", type=float,
                     help="override the window start offset (s before origin)")
    grp.add_argument("--post-seconds", dest="post_seconds", type=float,
                     help="override the window end offset (s after origin)")
    grp.add_argument("--channel", default="BH*,HH*",
                     help="channel pattern for the download plan (default BH*,HH*)")
    grp.add_argument("--max-radius-km", dest="max_radius_km", type=float,
                     help="override the epicentral download radius")
    grp.add_argument("--max-stations", dest="max_stations", type=int,
                     help="cap the number of stations in the download plan")
    grp.add_argument("--download-datacenter", dest="download_datacenter",
                     help="comma-separated waveform datacentres "
                          "(default: from the region table)")
    grp.add_argument("--raw-out", dest="raw_out", default="data/raw",
                     help="--out value used in the download plan")
    grp.add_argument("--stations-out", dest="stations_out", default="data/stations",
                     help="--stations-out value used in the download plan")
    grp = parser.add_argument_group("misc")
    grp.add_argument("--text", help="free-text query this call was derived from "
                                    "(recorded for provenance)")
    grp.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout (s)")
    grp.add_argument("--out", help="also write the JSON result to this path")
    args = parser.parse_args()

    if (args.latitude is None) != (args.longitude is None):
        parser.error("--latitude and --longitude must be given together")
    t0 = _utc(args.start) if args.start else None
    t1 = _utc(args.end) if args.end else None
    if t0 is not None and t1 is not None and t1 <= t0:
        parser.error("--end must be later than --start")

    warnings: list[str] = []
    radius_deg = None
    if args.latitude is not None:
        if args.within_km is None:
            args.within_km = DEFAULT_WITHIN_KM
            log(f"--within-km not given; searching {DEFAULT_WITHIN_KM:g} km "
                f"around ({args.latitude}, {args.longitude})")
        radius_deg = args.within_km / KM_PER_DEG
    elif args.within_km is not None:
        parser.error("--within-km requires --latitude/--longitude")

    query = {
        "text": args.text,
        "event_id": args.event_id,
        "start": args.start,
        "end": args.end,
        "latitude": args.latitude,
        "longitude": args.longitude,
        "within_km": args.within_km,
        "min_magnitude": args.min_magnitude,
        "max_magnitude": args.max_magnitude,
        "datacenters": list(EVENT_DATACENTERS),
    }

    def _finish(payload: dict) -> None:
        payload["query"] = query
        if warnings:
            payload.setdefault("warnings", []).extend(warnings)
        if args.out:
            path = Path(args.out)
            ensure_dir(path.parent)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            payload["out_path"] = str(path)
        emit(payload)

    # --- sufficiency: do we have anything to query at all? -----------------
    if not (args.event_id or t0 is not None or t1 is not None or args.latitude is not None):
        _finish({
            "resolution": "insufficient_input",
            "needs_user_input": True,
            "missing": ["event id, or a time window, or a place/epicentre"],
            "question": _question_for("insufficient_input", args, []),
            "hints": [
                "用联网搜索把用户的自然语言描述（地名/日期/震级）转换成时间窗口、震中坐标或事件编号。",
                "常见权威目录：USGS ComCat (earthquake.usgs.gov)、EMSC (www.emsc-csem.org)、"
                "ISC (www.isc.ac.uk)、中国地震台网 (www.ceic.ac.cn)。",
                "把检索结果传给本脚本，由脚本再从 FDSN 事件服务取权威记录；仍不确定时向用户提问。",
            ],
            "event": None,
            "download_plan": None,
            "candidates": [],
        })
        return

    # --- query catalogues --------------------------------------------------
    if args.event_id and t0 is None and t1 is None:
        log("event id given: querying catalogues by id")
    elif t0 is None and t1 is None:
        warnings.append("no time window given: candidates span the whole catalogue history")

    records: list[dict] = []
    for dc in EVENT_DATACENTERS:
        records.extend(_query_datacenter(dc, args, t0, t1, radius_deg))

    clusters = _cluster(records, args.cluster_km, args.cluster_seconds)
    clusters.sort(key=_mag_key, reverse=True)
    log(f"{len(records)} catalogue record(s) -> {len(clusters)} distinct event(s)")

    event, resolution, reason, needs_input = _resolve(clusters, args)
    candidates = [{
        "event_id": cl[0]["event_id"],
        "origin_time": cl[0]["origin_time"],
        "latitude": cl[0]["latitude"],
        "longitude": cl[0]["longitude"],
        "depth_km": cl[0]["depth_km"],
        "magnitude": _cluster_mag(cl),
        "magnitude_type": cl[0]["magnitude_type"],
        "place": cl[0]["place"],
        "sources": cl[0]["sources"],
    } for cl in clusters]

    if resolution != "resolved":
        _finish({
            "resolution": resolution,
            "needs_user_input": needs_input,
            "reason": reason,
            "question": _question_for(resolution, args, clusters),
            "event": None,
            "download_plan": None,
            "n_candidates": len(candidates),
            "candidates": candidates,
        })
        return

    plan = _download_plan(event, args)
    if needs_input:
        warnings.append(f"auto-selected by rule ({reason}); confirm with the user "
                        "before running the download if the choice is not obvious")

    _finish({
        "resolution": "resolved",
        "needs_user_input": needs_input,
        "reason": reason,
        "question": _question_for("ambiguous", args, clusters) if needs_input else None,
        "event": event,
        "download_plan": plan,
        "n_candidates": len(candidates),
        "candidates": candidates,
    })


if __name__ == "__main__":
    run_main(main)
