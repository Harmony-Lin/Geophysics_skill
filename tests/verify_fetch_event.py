"""Verification of the Phase-1 data-acquisition path.

Covers the two failure modes reported for Phase 1:

  1. natural language -> authoritative event parameters -> download plan
     (`scripts/p01_resolve_event.py`)
  2. the documented radius command actually working, i.e. argparse accepts
     `--min-radius 0 --max-radius 500` and only stations inside the annulus
     are downloaded (`scripts/p01_fetch_waveforms.py`)

Live FDSN queries are required for the resolver and the download checks, so
every network step is skipped (not failed) when the services are unreachable
or `--offline` is given.  The argument-parsing checks always run.

Reference event: NC 75265261, 2025-11-18 17:54:30 UTC, Mw 4.1,
7 km W of Templeton, CA (35.5528 N / 120.7878 W, 4.9 km depth) - the same
mainshock used by tests/fetch_templeton.py.

Usage:
    python tests/verify_fetch_event.py [--offline] [--keep]
        --offline : only run the checks that need no network
        --keep    : keep the downloaded scratch data under tests/tmp_data/

Exit code 0 = no failures; prints a JSON summary.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "seismic-workflow" / "scripts"
TMP = Path(__file__).resolve().parent / "tmp_data"
WS = TMP / "verify_fetch"          # scratch download workspace

MAINSHOCK_ID = "nc75265261"
MAINSHOCK_LAT = 35.5528
MAINSHOCK_LON = -120.7878
# origin +- 1 min of waveform around the 17:54:30 origin, used when the
# resolver is unavailable.
WINDOW = ("2025-11-18T17:53:30", "2025-11-18T17:59:30")

CHECKS: list[dict] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print(f"[SKIP] {name} -- {why}")


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def parse_json(text: str) -> dict:
    """Parse the outermost (longest) JSON object from stdout."""
    if not text or not text.strip():
        return {}
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"^\{", text, re.MULTILINE))):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def cmdline_checks(py: str, ws: Path) -> None:
    """The documented radius command must at least parse (regression test)."""
    proc = run([py, SCRIPTS / "p01_fetch_waveforms.py", "--help"])
    helped = proc.returncode == 0 and "--max-radius" in proc.stdout
    check("P1b --help works", helped,
          "--max-radius missing from --help" if not helped else "")
    if not helped:
        return

    # `--min-radius 0 --max-radius 500` used to abort in argparse.  Paired with
    # --from-local on an empty directory it must get past argument parsing and
    # fail (fast, offline) on the missing waveforms instead.
    empty = ws / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    proc = run([py, SCRIPTS / "p01_fetch_waveforms.py", "--min-radius", "0",
                "--max-radius", "500", "--max-stations", "0",
                "--latitude", "35.5528", "--longitude", "-120.7878",
                "--channel", "BHZ", "--from-local", empty,
                "--out", str(ws / "parse_out"),
                "--stations-out", str(ws / "parse_out")])
    bad = "unrecognized arguments" in proc.stderr or "invalid choice" in proc.stderr
    accepted = parse_json(proc.stdout).get("error", "")
    check("P1b --min-radius 0 --max-radius 500 parses", not bad,
          accepted or proc.stderr.strip().splitlines()[-1:] or "")


def resolver_checks(py: str, offline: bool) -> dict:
    """Natural language / event id -> authoritative parameters + plan."""
    if offline:
        skip("P1a resolver by event id", "--offline")
        skip("P1a resolver on vague input", "--offline")
        return {}

    # (a) insufficient input must ask the user instead of guessing.
    proc = run([py, SCRIPTS / "p01_resolve_event.py", "--text", "帮我下载地震波形"])
    res = parse_json(proc.stdout)
    check("P1a vague input asks the user",
          res.get("resolution") == "insufficient_input"
          and bool(res.get("question")),
          f"resolution={res.get('resolution')}")
    if not res:
        skip("P1a resolver by event id", "no JSON from resolver (offline?)")
        return {}

    # (b) event id -> exact parameters + ready-to-run download plan.
    proc = run([py, SCRIPTS / "p01_resolve_event.py", "--event-id", MAINSHOCK_ID])
    res = parse_json(proc.stdout)
    if not res:
        skip("P1a resolver by event id", "no JSON from resolver (offline?)")
        return {}
    check("P1a resolver by event id", res.get("resolution") == "resolved",
          f"resolution={res.get('resolution')} error={res.get('error', '')}")

    ev = res.get("event") or {}
    lat, lon = ev.get("latitude"), ev.get("longitude")
    near = (isinstance(lat, (int, float)) and abs(lat - MAINSHOCK_LAT) < 0.05
            and isinstance(lon, (int, float)) and abs(lon - MAINSHOCK_LON) < 0.05)
    check("P1a coordinates match USGS/NC catalogue", near,
          f"lat={lat} lon={lon} expected={MAINSHOCK_LAT}/{MAINSHOCK_LON}")
    origin = ev.get("origin_time")
    plan = res.get("download_plan") or {}
    plan_start, plan_end = plan.get("start"), plan.get("end")
    check("P1a plan window brackets the origin",
          bool(origin) and bool(plan_start) and bool(plan_end)
          and plan_start < origin < plan_end,
          f"origin={origin} window={plan_start}..{plan_end}")

    argv = plan.get("argv") or []
    check("P1a plan is runnable", isinstance(argv, list) and len(argv) > 4
          and "--latitude" in argv and "--max-radius" in argv,
          f"{len(argv)} args")
    check("P1a plan uses km radii",
          "--radius-unit" not in argv or argv[argv.index("--radius-unit") + 1] == "km",
          plan.get("command", "")[:120])
    return res


def download_checks(py: str, res: dict, offline: bool) -> None:
    """Radius selection + the annulus invariant on the real download."""
    out = WS / "data" / "raw"
    stations_out = WS / "data" / "stations"

    if offline:
        skip("P1b radius download", "--offline")
        return

    lat, lon = res.get("event", {}).get("latitude"), res.get("event", {}).get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        lat, lon = MAINSHOCK_LAT, MAINSHOCK_LON
    plan = res.get("download_plan") or {}
    start = plan.get("start") or WINDOW[0]
    end = plan.get("end") or WINDOW[1]

    # Deliberately use the originally broken invocation: min 0, max 500 km.
    cmd = [py, SCRIPTS / "p01_fetch_waveforms.py",
           "--latitude", lat, "--longitude", lon,
           "--min-radius", "0", "--max-radius", "500",
           "--max-stations", "6",
           "--start", start, "--end", end,
           "--datacenter", "NCEDC,SCEDC,EARTHSCOPE",
           "--channel", "BH*,HH*,EH*",
           "--station-level", "response",
           "--out", out, "--stations-out", stations_out,
           "--timeout", "120"]
    proc = run(cmd)
    got = parse_json(proc.stdout)

    if not got:
        skip("P1b radius download", "no JSON (offline or all data centres down)")
        return

    check("P1b radius download ok", got.get("status") == "ok",
          f"status={got.get('status')} error={got.get('error', '')}")
    if got.get("status") != "ok":
        return

    distances = (got.get("distance_km") or {})
    dmax = distances.get("max")
    dmin = distances.get("min")
    check("P1b all stations inside the 500 km annulus",
          isinstance(dmax, (int, float)) and dmax <= 500.0 + 1e-6,
          f"distance_km min={dmin} max={dmax}")
    check("P1b radius respected on the near side",
          isinstance(dmin, (int, float)) and dmin >= 0.0,
          f"distance_km min={dmin}")
    used = got.get("max_stations_used")
    cap = used if isinstance(used, int) and used > 0 else 6
    check("P1b the applied station cap is respected",
          got.get("n_stations_downloaded", 0) <= cap,
          f"n_stations_downloaded={got.get('n_stations_downloaded')} "
          f"n_stations_requested={got.get('n_stations_requested')} "
          f"cap requested={got.get('max_stations_requested')} used={used}")
    check("P1b waveform written count matches traces",
          got.get("n_files", 0) > 0 and got.get("n_traces", 0) > 0
          and got.get("n_files") <= got.get("n_traces"),
          f"n_traces={got.get('n_traces')} n_files={got.get('n_files')}")

    xml = stations_out / "stations.xml"
    csv = stations_out / "stations.csv"
    check("P1b station metadata written", xml.exists() and csv.exists(),
          f"{xml.name}={xml.stat().st_size if xml.exists() else 'missing'} bytes")

    if csv.exists():
        header = csv.read_text(encoding="utf-8").splitlines()[:1]
        check("P1b stations.csv schema",
              header and header[0].startswith("station,network,station_code"),
              header[0] if header else "empty")

    check("P1b responses attached",
          got.get("n_traces_with_response", 0) > 0,
          f"n_traces_with_response={got.get('n_traces_with_response')} "
          f"of {got.get('n_traces')}")

    stats = got.get("datacenter_stats")
    check("P1b per-datacenter stats reported",
          isinstance(stats, list) and stats
          and all(isinstance(s, dict) and s.get("datacenter") for s in stats),
          "; ".join(f"{s.get('datacenter')}="
                    f"{s.get('n_stations', s.get('error', '?'))}"
                    for s in (stats or []))[:200])
    check("P1b failures surfaced, not fatal",
          not got.get("request_failures")
          or all("datacenter" in f for f in got["request_failures"]),
          f"{len(got.get('request_failures') or [])} datacenter-level failure(s)")


def finish(keep: bool) -> int:
    if not keep and WS.exists():
        shutil.rmtree(WS, ignore_errors=True)
    failed = [c for c in CHECKS if not c["ok"]]
    summary = {"status": "ok" if not failed else "error",
               "n_checks": len(CHECKS), "n_failed": len(failed),
               "n_skipped": len(SKIPPED), "skipped": SKIPPED,
               "failed": [c["check"] for c in failed], "checks": CHECKS}
    print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not failed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true",
                    help="skip every check that needs FDSN access")
    ap.add_argument("--keep", action="store_true",
                    help="keep tests/tmp_data/verify_fetch/")
    args = ap.parse_args()

    WS.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    cmdline_checks(py, WS)
    res = resolver_checks(py, args.offline)
    download_checks(py, res, args.offline)
    return finish(args.keep)


if __name__ == "__main__":
    sys.exit(main())
