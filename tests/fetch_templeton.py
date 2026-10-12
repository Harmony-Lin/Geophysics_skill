"""Fetch the default test dataset: real waveforms of the 2025 M4.1
Templeton (California) earthquake sequence from FDSN data centres.

Default scenario (real NCEDC catalogue parameters):

  Mainshock  : 2025-11-18 17:54:30 UTC, Mw 4.1, 35.5528 N / 120.7878 W,
               4.9 km depth (NC event nc75265261, "7 km W of Templeton, CA")
  Aftershocks: the sequence is listed in tests/aftershock_sequence.csv
               (156 catalog rows). Because the sequence is long, only the N
               largest aftershocks by magnitude are downloaded in addition to
               the mainshock (default N = 10, see --n-aftershocks).
  Channels   : tests/channels.csv lists the channels recorded for this
               sequence (303 rows, 157 stations on the AZ/BK/CE/CI/LB/NC/NN/
               NP/PG/WR networks).  The inventory only lists horizontal
               components, so the matching vertical component
               (HHE -> HHZ, HNE -> HNZ, ...) is requested as well.

For every event ~3.5 minutes of 3-component waveform data around the origin
time are downloaded and written as one miniSEED file per event, together with
a StationXML inventory (with responses, needed for ML magnitudes) and a
stations.csv.

The channels are spread over three FDSN archives - EarthScope, NCEDC and
SCEDC - and no single one holds the whole sequence, so each network is routed
to the archive that actually serves it (`NETWORK_ROUTES` holds the defaults,
the script learns overrides at run time).  Requests are retried and an
unreachable archive is skipped after repeated failures instead of aborting
the run.

Outputs (under tests/tmp_data/):
    raw/event_<NN>.mseed     one file per event (event_00 = mainshock)
    stations.xml             StationXML for the downloaded channels
    stations/stations.csv    station list (same schema as the pipeline)
    templeton_catalog.csv    catalogue of the downloaded events

Usage:
    python tests/fetch_templeton.py [--n-aftershocks 10] [--services ...]
"""
from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "tmp_data"
RAW = OUT / "raw"
CHANNELS_CSV = HERE / "channels.csv"
AFTERSHOCK_CSV = HERE / "aftershock_sequence.csv"

# Mainshock, NC event nc75265261 (the USGS event page rounds it to
# M 4.1, 35.553 N / 120.788 W, 4.9 km depth).
MAINSHOCK_ID = "nc75265261"
MAINSHOCK_FALLBACK = {
    "id": MAINSHOCK_ID,
    "time": "2025-11-18T17:54:30.480Z",
    "latitude": 35.552833,
    "longitude": -120.787833,
    "depth": 4.92,
    "mag": 4.11,
    "magType": "mw",
    "place": "7 km W of Templeton, CA",
}

PRE_SECONDS = 30.0    # window start relative to origin time
POST_SECONDS = 180.0  # window end relative to origin time

DEFAULT_N_AFTERSHOCKS = 10

# Which archive serves which network.  Established with the inventory of
# tests/channels.csv: NCEDC holds the whole sequence except AZ and LB, which
# only EarthScope carries.  SCEDC is kept as a second fallback for its own
# networks.  These are only start values - the script promotes whatever
# archive actually answers.
NETWORK_ROUTES = {
    "AZ": ["EARTHSCOPE", "SCEDC", "NCEDC"],
    "BK": ["NCEDC", "EARTHSCOPE"],
    "CE": ["NCEDC", "EARTHSCOPE"],
    "CI": ["NCEDC", "SCEDC", "EARTHSCOPE"],
    "LB": ["EARTHSCOPE", "NCEDC"],
    "NC": ["NCEDC", "EARTHSCOPE"],
    "NN": ["EARTHSCOPE", "NCEDC"],
    "NP": ["NCEDC", "EARTHSCOPE"],
    "PG": ["NCEDC", "SCEDC", "EARTHSCOPE"],
    "WR": ["NCEDC", "EARTHSCOPE"],
}
SERVICE_ORDER = ["EARTHSCOPE", "NCEDC", "SCEDC"]
DEFAULT_SERVICES = ",".join(SERVICE_ORDER)

# Concurrent requests allowed per archive.  The NCEDC rejects bursts of
# parallel connections, so it is kept deliberately low.
SERVICE_LIMITS = {"EARTHSCOPE": 4, "NCEDC": 2, "SCEDC": 4}
DEFAULT_SERVICE_LIMIT = 3

DEFAULT_SERVICES_PER_EVENT = 2  # primary archive + one fallback per network


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", flush=True)


def _short(exc: BaseException, limit: int = 120) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:limit]


# ---------------------------------------------------------------------------
# input files
# ---------------------------------------------------------------------------
def load_channel_lines(path: Path = CHANNELS_CSV, add_vertical: bool = True
                       ) -> tuple[dict[tuple[str, str, str], str], int]:
    """Read the channel inventory (NET / STA / CHN / LOC TSV).

    Returns ({(net, sta, loc): "HHE,HHN,HHZ"}, number_of_inventory_rows).
    The vertical component of every horizontal channel is added because the
    inventory only lists horizontal components while the workflows need Z.
    """
    lines: dict[tuple[str, str, str], set[str]] = {}
    n_rows = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if not row or row[0].strip().upper() == "NET":
                continue
            net, sta = row[0].strip(), row[1].strip()
            chn = row[2].strip() if len(row) > 2 else ""
            if not (net and sta and chn):
                continue
            loc = row[3].strip() if len(row) > 3 else ""
            chans = {chn}
            if add_vertical and chn[-1] in ("E", "N"):
                chans.add(chn[:-1] + "Z")
            lines.setdefault((net, sta, loc), set()).update(chans)
            n_rows += 1
    if not lines:
        raise SystemExit(f"[ERROR] no channels found in {path}")
    return {key: ",".join(sorted(v)) for key, v in lines.items()}, n_rows


def load_aftershocks(n_aftershocks: int = DEFAULT_N_AFTERSHOCKS
                     ) -> tuple[dict, list[dict]]:
    """Return (mainshock, the `n_aftershocks` largest events as a list)."""
    rows = []
    with open(AFTERSHOCK_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("time") or not row.get("mag"):
                continue
            rows.append({
                "id": (row.get("id") or "").strip(),
                "time": row["time"].strip(),
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "depth": float(row["depth"]),
                "mag": float(row["mag"]),
                "magType": (row.get("magType") or "").strip(),
                "place": (row.get("place") or "").strip(),
            })
    if not rows:
        raise SystemExit(f"[ERROR] no events in {AFTERSHOCK_CSV}")

    mainshock = next((r for r in rows if r["id"] == MAINSHOCK_ID), None)
    if mainshock is None:
        warn(f"{MAINSHOCK_ID} is not in {AFTERSHOCK_CSV.name}; using the "
             f"parameters of the USGS event page instead")
        mainshock = dict(MAINSHOCK_FALLBACK)

    aftershocks = sorted((r for r in rows if r["id"] != mainshock["id"]),
                         key=lambda r: (-r["mag"], r["time"]))
    return mainshock, aftershocks[:max(0, n_aftershocks)]


# ---------------------------------------------------------------------------
# FDSN access
# ---------------------------------------------------------------------------
def _make_client(name: str, timeout: float):
    """Build an obspy client for one of the supported archives."""
    from obspy.clients.fdsn import Client

    if name.upper() == "NCEDC":
        # The NCEDC station service is often unreachable, which makes obspy's
        # automatic service discovery abort; map the endpoints explicitly.
        return Client(base_url="https://service.ncedc.org",
                      _discover_services=False, timeout=timeout,
                      service_mappings={
                          "dataselect":
                              "https://service.ncedc.org/fdsnws/dataselect/1",
                          "station":
                              "https://service.ncedc.org/fdsnws/station/1"})
    return Client(name.upper() if name.isupper() else name, timeout=timeout)


class ServicePool:
    """Thread-safe FDSN client pool with routing and failure memory.

    * one client per (thread, archive), built lazily
    * a semaphore per archive so a service never sees a burst of parallel
      connections
    * a per-network archive order that starts from `NETWORK_ROUTES` and is
      reordered whenever an archive turns out to be the one that answers
    * a failure counter so a permanently broken archive is dropped instead of
      being retried for every station of every event
    """

    def __init__(self, names: list[str], timeout: float = 180.0,
                 max_failures: int = 3):
        self.names = [n.upper() for n in names]
        self.timeout = float(timeout)
        self.max_failures = int(max_failures)
        self._local = threading.local()
        self._sems = {n: threading.Semaphore(SERVICE_LIMITS.get(
            n, DEFAULT_SERVICE_LIMIT)) for n in self.names}
        self.routes: dict[str, list[str]] = {}
        self._failures: dict[tuple[str, str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def client(self, name: str):
        clients = getattr(self._local, "clients", None)
        if clients is None:
            clients = self._local.clients = {}
        if name not in clients:
            clients[name] = _make_client(name, self.timeout)
        return clients[name]

    def order(self, net: str) -> list[str]:
        """Archive order for a network: hints first, then the remaining ones."""
        hints = [n for n in NETWORK_ROUTES.get(net, []) if n in self.names]
        if not hints:
            hints = list(self.names)
        learned = self.routes.get(net)
        if learned:
            hints = [n for n in learned if n in self.names] + \
                    [n for n in hints if n not in learned]
        return hints + [n for n in self.names if n not in hints]

    def promote(self, net: str, name: str) -> None:
        with self._lock:
            current = self.routes.setdefault(net, self.order(net))
            if current[0] != name:
                self.routes[net] = [name] + [n for n in current if n != name]

    def note_failure(self, name: str, net: str, kind: str) -> bool:
        """Count a failure; True once the archive is dropped for `net`."""
        with self._lock:
            key = (name, net, kind)
            self._failures[key] += 1
            return self._failures[key] >= self.max_failures

    def dead(self, name: str, net: str, kind: str = "wave") -> bool:
        return self._failures[(name, net, kind)] >= self.max_failures

    def request(self, name: str, fn):
        """Run `fn(client)` under this archive's concurrency limit."""
        with self._sems[name]:
            return fn(self.client(name))


def _waveforms(pool: ServicePool, name: str, lines, t0, t1):
    bulk = [(net, sta, loc, chans, t0, t1) for net, sta, loc, chans in lines]
    return pool.request(name, lambda c: c.get_waveforms_bulk(bulk))


def request_waveforms(pool: ServicePool, name: str, net: str, lines, t0, t1,
                      tries: int = 2):
    """Fetch waveforms from one archive with retries.

    Returns (stream, status) with status
      "ok"     -> the archive answered (the stream may still be partial),
      "nodata" -> the archive does not hold this network/epoch,
      "error"  -> the request failed (and was counted against the archive).
    """
    from obspy.clients.fdsn.header import FDSNNoDataException

    for attempt in range(1, tries + 1):
        try:
            return _waveforms(pool, name, lines, t0, t1), "ok"
        except FDSNNoDataException:
            return None, "nodata"
        except Exception as exc:
            warn(f"{name}: {net} waveforms failed "
                 f"(attempt {attempt}/{tries}): {_short(exc)}")
            if attempt < tries:
                time.sleep(3.0 * attempt)
    if pool.note_failure(name, net, "wave"):
        warn(f"{name}: dropping {net} waveforms for the rest of this run")
    return None, "error"


def _stations(pool: ServicePool, name: str, bulk):
    return pool.request(name, lambda c: c.get_stations_bulk(
        bulk, level="response"))


def _covered(stream) -> set[tuple[str, str, str]]:
    return {(tr.stats.network, tr.stats.station, tr.stats.location)
            for tr in stream}


def download_event(pool: ServicePool, net_lines, t0, t1, workers: int,
                   max_services: int = DEFAULT_SERVICES_PER_EVENT):
    """Download the waveforms of one event (one request per network).

    The network is asked at its preferred archive first; only the stations
    that are still missing are then requested from the next archive, so a
    single incomplete archive cannot ruin the event.
    """
    from obspy import Stream

    def worker(net: str):
        lines = net_lines[net]
        wanted = {(n, s, lo) for n, s, lo, _ in lines}
        got = Stream()
        tried = 0
        for name in pool.order(net):
            if tried >= max_services:
                break
            missing = [ln for ln in lines
                       if (ln[0], ln[1], ln[2]) not in _covered(got)]
            if not missing:
                break
            if pool.dead(name, net, "wave"):
                continue
            tried += 1
            stream, status = request_waveforms(pool, name, net, missing, t0, t1)
            if status == "ok" and stream is not None and len(stream):
                got += stream
                if _covered(got) >= wanted:
                    pool.promote(net, name)
        missing_n = len(wanted - (_covered(got) & wanted))
        if missing_n:
            warn(f"{net}: {missing_n}/{len(wanted)} stations have no data "
                 f"for this event")
        return got

    stream = Stream()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(net_lines)))) as ex:
        for fut in as_completed([ex.submit(worker, net) for net in net_lines]):
            stream += fut.result()
    return stream


def probe_routes(pool: ServicePool, net_lines, t0, t1, workers: int) -> set[str]:
    """Check which archive answers for each network (one station each).

    Cheap pre-flight so that the expensive station-metadata download is only
    sent to archives that really hold the network.  Returns the networks for
    which no archive has data.
    """
    def probe(net: str):
        for name in pool.order(net):
            if pool.dead(name, net, "wave"):
                continue
            try:
                st, status = request_waveforms(pool, name, net,
                                               [net_lines[net][0]], t0, t1,
                                               tries=1)
            except Exception as exc:  # never let the probe abort the run
                warn(f"{name}: probing {net} failed: {_short(exc)}")
                continue
            if status == "ok" and st is not None and len(st):
                pool.promote(net, name)
                return net, name
        return net, None

    routes: dict[str, str] = {}
    nets = list(net_lines)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(nets)))) as ex:
        for fut in as_completed([ex.submit(probe, net) for net in nets]):
            net, name = fut.result()
            if name:
                routes[net] = name
    return set(net_lines) - set(routes)


def fetch_inventory(pool: ServicePool, wanted, t0, t1, workers: int):
    """StationXML with responses for `wanted` {(net, sta, loc, cha)}.

    Each network is fetched as a single bulk request routed to an archive that
    holds it; the leftover channels are retried once at the next archive.
    Returns (inventory, missing_channels).
    """
    from obspy import Inventory

    per_net: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for net, sta, loc, cha in wanted:
        per_net[net].add((sta, loc, cha))

    def worker(net: str):
        wanted_net = {(net, s, l, c) for s, l, c in per_net[net]}
        found: set[tuple[str, str, str, str]] = set()
        inv = Inventory()
        tried = 0
        for name in pool.order(net):
            need = sorted(per_net[net] - {(s, l, c) for _n, s, l, c in found})
            if not need or tried >= DEFAULT_SERVICES_PER_EVENT:
                break
            if pool.dead(name, net, "meta"):
                continue
            tried += 1
            by_sta: dict[str, dict[str, set[str]]] = defaultdict(
                lambda: defaultdict(set))
            for sta, loc, cha in need:
                by_sta[sta][loc].add(cha)
            lines = [(net, sta, loc, ",".join(sorted(chas)))
                     for sta, locs in sorted(by_sta.items())
                     for loc, chas in sorted(locs.items())]
            bulk = [(n, s, l, c, t0, t1) for n, s, l, c in lines]
            try:
                got = _stations(pool, name, bulk)
            except Exception as exc:
                warn(f"{name}: {net} station metadata failed: {_short(exc)}")
                if pool.note_failure(name, net, "meta"):
                    warn(f"{name}: dropping {net} metadata for this run")
                continue
            keys = {(ncd.code, stc.code, cha.location_code, cha.code)
                    for ncd in got for stc in ncd for cha in stc}
            gained = keys & wanted_net
            if gained:
                inv += got
                found |= gained
                if found >= wanted_net:
                    pool.promote(net, name)
        return inv, wanted_net - found

    merged = Inventory()
    missing: set[tuple[str, str, str, str]] = set()
    nets = list(per_net)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(nets)))) as ex:
        for fut in as_completed([ex.submit(worker, net) for net in nets]):
            inv, miss = fut.result()
            merged += inv
            missing |= miss
    return merged, missing


# ---------------------------------------------------------------------------
# post-processing
# ---------------------------------------------------------------------------
def dedupe_sensors(st, requested_locs: dict[tuple[str, str], set[str]]):
    """Keep one 3-component sensor set per station.

    A station can appear with several location codes (for example both "" and
    "00"); keep the requested location if present, otherwise the one with the
    most components.
    """
    by_sta: dict[tuple[str, str], list] = defaultdict(list)
    for tr in st:
        by_sta[(tr.stats.network, tr.stats.station)].append(tr)

    kept = []
    for (net, sta), traces in by_sta.items():
        locs = requested_locs.get((net, sta), set())
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for tr in traces:
            groups[(tr.stats.location, tr.stats.channel[0])].append(tr)

        def rank(item, locs=locs):
            (loc, _band), members = item
            return (0 if loc in locs else 1, -len(members))

        kept.extend(min(groups.items(), key=rank)[1])
    return type(st)(kept)


def has_response(inv, tr) -> bool:
    try:
        inv.get_response(tr.id, tr.stats.starttime)
        return True
    except Exception:
        return False


def trim_inventory(inv, used: set[tuple[str, str, str, str]]):
    """Keep only the channels that ended up in the downloaded dataset.

    ``Inventory.select`` hands its arguments to :func:`fnmatch.fnmatch`, so a
    comma separated list of codes is treated as one pattern and matches
    nothing; the networks/stations/channels are filtered by hand instead.
    """
    from copy import copy

    wanted: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(set))
    for net, sta, loc, cha in used:
        wanted[net][sta].add((loc, cha))

    trimmed = copy(inv)
    trimmed.networks = []
    for net in inv:
        per_sta = wanted.get(net.code)
        if not per_sta:
            continue
        out_net = copy(net)
        out_net.stations = []
        for sta in net:
            pairs = per_sta.get(sta.code)
            if not pairs:
                continue
            out_sta = copy(sta)
            out_sta.channels = [cha for cha in sta.channels
                                if (cha.location_code, cha.code) in pairs]
            if out_sta.channels:
                out_net.stations.append(out_sta)
        if out_net.stations:
            trimmed.networks.append(out_net)
    return trimmed


def write_stations_csv(inv, used: set[tuple[str, str, str, str]]) -> int:
    """Write the station list for the channels that were downloaded.

    One row per (network, station, location) present in `used`.  The upstream
    helper `common.stations_from_xml` writes only ``NET.STA.``, which loses the
    location code; the pick ids keep it (``BK.PKD.00``), so the two could not
    be joined.  Station epochs that share a location collapse into one row
    here, which also keeps the ids unique.
    """
    sys.path.insert(0, str(ROOT / "seismic-workflow" / "scripts"))
    from common import save_stations_csv

    wanted = {(net, sta, loc) for net, sta, loc, _ in used}
    rows = []
    for net in inv:
        for sta in net:
            for cha in sta.channels:
                key = (net.code, sta.code, cha.location_code)
                if key not in wanted:
                    continue
                wanted.discard(key)
                rows.append({
                    "station": f"{net.code}.{sta.code}.{cha.location_code}",
                    "network": net.code,
                    "station_code": sta.code,
                    "latitude": sta.latitude,
                    "longitude": sta.longitude,
                    "elevation_m": sta.elevation or 0.0,
                })
    if wanted:
        warn(f"{len(wanted)} station/location combinations are missing from "
             f"stations.xml (e.g. {sorted(wanted)[0]})")
    if not rows:
        raise SystemExit("[ERROR] no stations to write to stations.csv")
    rows.sort(key=lambda r: (r["network"], r["station_code"], r["station"]))
    save_stations_csv(rows, OUT / "stations" / "stations.csv")
    return len(rows)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-aftershocks", dest="n_aftershocks", type=int,
                    default=DEFAULT_N_AFTERSHOCKS,
                    help="largest aftershocks to download on top of the "
                         f"mainshock (default {DEFAULT_N_AFTERSHOCKS})")
    ap.add_argument("--services", default=DEFAULT_SERVICES,
                    help="FDSN archives considered, comma separated "
                         "(default: %(default)s)")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="per-request timeout in seconds (default 180)")
    ap.add_argument("--workers", type=int, default=5,
                    help="parallel requests per phase (default 5)")
    ap.add_argument("--skip-probe", action="store_true",
                    help="skip the per-network pre-flight check")
    args = ap.parse_args()

    from obspy import UTCDateTime

    names = [s.strip() for s in args.services.split(",") if s.strip()]
    if not names:
        raise SystemExit("[ERROR] --services must name at least one archive")
    pool = ServicePool(names, timeout=args.timeout)
    started = time.time()

    channel_lines, n_rows = load_channel_lines()
    mainshock, aftershocks = load_aftershocks(args.n_aftershocks)
    events = [mainshock] + aftershocks

    net_lines: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    requested_locs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (net, sta, loc), chans in sorted(channel_lines.items()):
        net_lines[net].append((net, sta, loc, chans))
        requested_locs[(net, sta)].add(loc)

    n_stations = len({(net, sta) for net, sta, _ in channel_lines})
    mags = [e["mag"] for e in aftershocks]
    log(f"Scenario : M{mainshock['mag']:.2f} {mainshock['time']} "
        f"{mainshock['place']} ({mainshock['id']})")
    log(f"Events   : mainshock"
        + (f" + {len(aftershocks)} largest aftershocks "
           f"(M{min(mags):.2f}-M{max(mags):.2f})" if mags else ""))
    log(f"Channels : {sum(len(c.split(',')) for c in channel_lines.values())} "
        f"channels on {n_stations} stations ({n_rows} rows in "
        f"{CHANNELS_CSV.name}, {len(net_lines)} networks)")
    log(f"Archives : {', '.join(pool.names)}")

    # ---- pre-flight: which archive answers for which network -------------
    probe_t0 = UTCDateTime(mainshock["time"]) - PRE_SECONDS
    probe_t1 = UTCDateTime(mainshock["time"]) + POST_SECONDS
    if not args.skip_probe:
        log("Probing archives per network...")
        dead_nets = probe_routes(pool, net_lines, probe_t0, probe_t1,
                                 args.workers)
        for net in sorted(net_lines):
            route = pool.order(net)[0]
            n_sta = len({s for _n, s, _l, _c in net_lines[net]})
            if net in dead_nets:
                warn(f"  {net:3s} -> no archive answered ({n_sta} stations "
                     f"will be skipped)")
            else:
                log(f"  {net:3s} -> {route:11s} ({n_sta} stations)")
        for net in dead_nets:
            net_lines.pop(net)
        if not net_lines:
            raise SystemExit("[ERROR] no archive returned data for any "
                             f"network; tried {', '.join(pool.names)}")

    planned = {(net, sta, loc, chn)
               for net, lines in net_lines.items()
               for _n, sta, loc, chans in lines
               for chn in chans.split(",")}

    # ---- station metadata (responses are needed for ML magnitudes) -------
    t_first = UTCDateTime(events[0]["time"]) - PRE_SECONDS
    t_last = max(UTCDateTime(e["time"]) for e in events) + POST_SECONDS
    log(f"Downloading station metadata "
        f"({t_first.date} .. {t_last.date})...")
    t_meta = time.time()
    inv, missing_meta = fetch_inventory(pool, planned, t_first, t_last,
                                        max(2, args.workers // 2))
    if missing_meta:
        warn(f"{len(missing_meta)} of {len(planned)} channels have no station "
             f"metadata and will be left out of the dataset")
    n_meta = len({(n.code, s.code, c.location_code, c.code)
                  for n in inv for s in n for c in s})
    log(f"    {n_meta} channels with response in {time.time() - t_meta:.0f}s")

    # ---- event waveforms --------------------------------------------------
    RAW.mkdir(parents=True, exist_ok=True)
    for stale in RAW.glob("event_*.mseed"):
        stale.unlink()

    catalog_rows = []
    used: set[tuple[str, str, str, str]] = set()
    n_ok = n_dropped = 0
    for k, ev in enumerate(events):
        t0 = UTCDateTime(ev["time"]) - PRE_SECONDS
        t1 = UTCDateTime(ev["time"]) + POST_SECONDS
        log(f"[{k + 1}/{len(events)}] {ev['id']} M{ev['mag']:.2f} "
            f"{ev['time']}")
        t_ev = time.time()
        st = download_event(pool, net_lines, t0, t1, args.workers)
        if not len(st):
            warn(f"event {k} ({ev['id']}): no data after retries")
            continue

        st = dedupe_sensors(st, requested_locs)
        st.merge(method=1, fill_value="interpolate")
        st.trim(starttime=t0, endtime=t1)

        without = [tr for tr in st if not has_response(inv, tr)]
        if without:
            n_dropped += len(without)
            warn(f"event {k}: dropping {len(without)} traces without a "
                 f"response (e.g. {without[0].id})")
            drop = {id(tr) for tr in without}
            st = type(st)([tr for tr in st if id(tr) not in drop])
        if not len(st):
            warn(f"event {k} ({ev['id']}): nothing left to write")
            continue

        path = RAW / f"event_{k:02d}.mseed"
        st.write(str(path), format="MSEED")
        used.update((tr.stats.network, tr.stats.station, tr.stats.location,
                     tr.stats.channel) for tr in st)
        n_ok += 1
        n_sta = len({(tr.stats.network, tr.stats.station) for tr in st})
        catalog_rows.append({
            "event_index": k,
            "event_id": ev["id"],
            "time": ev["time"],
            "latitude": ev["latitude"],
            "longitude": ev["longitude"],
            "depth_km": ev["depth"],
            "magnitude": ev["mag"],
            "mag_type": ev["magType"],
            "place": ev["place"],
            "n_traces": len(st),
            "n_stations": n_sta,
            "waveform_file": path.name,
        })
        log(f"    -> {path.name}: {len(st)} traces, {n_sta} stations "
            f"({time.time() - t_ev:.0f}s)")

    if not used:
        raise SystemExit("[ERROR] no waveform data was downloaded")

    # ---- keep only the channels that ended up in the dataset -------------
    inv = trim_inventory(inv, used)
    OUT.mkdir(parents=True, exist_ok=True)
    inv.write(str(OUT / "stations.xml"), format="STATIONXML")
    n_sta_csv = write_stations_csv(inv, used)

    with open(OUT / "templeton_catalog.csv", "w", newline="",
              encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(catalog_rows[0]))
        writer.writeheader()
        writer.writerows(catalog_rows)

    n_used_stations = len({(net, sta) for net, sta, _, _ in used})
    log(f"OK: {n_ok}/{len(events)} events, {len(used)} channels on "
        f"{n_used_stations} stations -> {RAW}")
    if n_dropped:
        log(f"    {n_dropped} traces dropped (no station metadata)")
    log(f"    stations.xml + stations/stations.csv ({n_sta_csv} rows, one "
        f"per network.station.location), templeton_catalog.csv")
    log(f"Scenario: 2025 M4.1 Templeton, CA sequence (real FDSN waveforms) "
        f"- {time.time() - started:.0f}s total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
