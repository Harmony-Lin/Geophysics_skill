#!/usr/bin/env python3
"""Phase 6 (backend A): relocDD-py wrapper (pure Python double-difference).

Pipeline: catalog + picks -> phase.dat/station.dat -> relocDD-py ph2dt
(catalog differential times) -> relocDD-py hypoDD (inversion) -> parse
hypoDD.reloc -> CSV.  No compiled Fortran binaries required; relocDD-py is
installed at install time from git (see packages/relocdd-py/install.py).

NOTE: relocDD-py's ph2dt builds differential times from the *catalog picks*
(phase.dat) only -- it does NOT perform waveform cross-correlation.  The
--waveforms argument is therefore accepted for CLI compatibility but ignored.
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, log, run_main, warn  # noqa: E402

ASSETS = Path(__file__).parent.parent / "assets"
# installed relocDD-py (packages/relocdd-py/install.py installs it from git)
RELOCDD_PACKAGES = ("ph2dt", "hypoDD")
INSTALL_HINT = ("install the pinned git revision: python "
                "seismic-workflow/packages/relocdd-py/install.py")

# Fixes the pinned upstream revision still needs, applied to the *installed*
# sources at startup (idempotent, so repeated runs are safe).
#  * NumPy >= 2 rejects `int()` on a 1-element `np.argwhere` row and the bare
#    `except` in the readers turns that into "skip every event pair": hypoDD
#    then reads 0 differential times and clustering crashes with `src_x` unbound.
#  * ph2dt divides by the number of linked pairs, which is 0 for an unlinked catalog.
_ARGWHERE_ROW = re.compile(r"int\(tuple\((np\.argwhere\([^)]*\))\)\[0\]\)")
# the tail groups keep these patterns idempotent (already-guarded lines do not match)
_AVOFF = re.compile(r"^(\s*)avoff = avoff/npair(\s*(?:#.*)?)$", re.M)
_AVOFF_STR = re.compile(r"^(\s*)avoff_str = avoff_str/\(ipair_str\)(\s*(?:#.*)?)$", re.M)
_AVOFF_STR1 = re.compile(r"^(\s*)avoff_str = avoff_str/\(ipair_str-1\)(\s*(?:#.*)?)$", re.M)
_AVLINKS = re.compile(r"^(\s*)print\('> average links per pair = ',"
                      r"int\(\(n7\+n8\)/ipair\)\)(\s*(?:#.*)?)$", re.M)
_PATCHES: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("methodTypes/eventPair/hypoDD_ev.py", _ARGWHERE_ROW, r"int(\1.flat[0])"),
    ("methodTypes/stationPair/hypoDD_stat.py", _ARGWHERE_ROW, r"int(\1.flat[0])"),
    ("methodTypes/doublePair/hypoDD_doub.py", _ARGWHERE_ROW, r"int(\1.flat[0])"),
    ("methodTypes/eventPair/ph2dt_ev.py", _AVOFF,
     r"\1avoff = avoff/npair if npair else 0.0"),
    ("methodTypes/eventPair/ph2dt_ev.py", _AVOFF_STR,
     r"\1avoff_str = avoff_str/(ipair_str) if ipair_str else 0.0\2"),
    ("methodTypes/eventPair/ph2dt_ev.py", _AVOFF_STR1,
     r"\1avoff_str = avoff_str/(ipair_str-1) if ipair_str > 1 else 0.0\2"),
    ("methodTypes/eventPair/ph2dt_ev.py", _AVLINKS,
     r"\1print('> average links per pair = ',int((n7+n8)/ipair) if ipair else 0)\2"),
)


def _relocdd_installed() -> bool:
    """True when the installed relocDD-py provides ph2dt and hypoDD."""
    try:
        return all(importlib.util.find_spec(mod) is not None
                   for mod in RELOCDD_PACKAGES)
    except (ImportError, ValueError):
        return False


def _relocdd_source_root() -> Path | None:
    """Directory of the installed relocDD-py sources (site-packages or clone).

    The package is installed editable from a git checkout, so `find_spec`
    points back at the checkout rather than into site-packages.
    """
    try:
        spec = importlib.util.find_spec("methodTypes")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent.parent


def _patch_installed_sources() -> None:
    """Repair the known upstream breakages in the installed relocDD-py tree."""
    root = _relocdd_source_root()
    if root is None:
        return
    touched: set[str] = set()
    for rel, pattern, repl in _PATCHES:
        path = root / rel
        if not path.is_file():
            continue
        src = path.read_text()
        fixed = pattern.sub(repl, src)
        if fixed != src:
            path.write_text(fixed)
            touched.add(rel)
    for rel in sorted(touched):
        warn(f"patched installed relocDD-py: {rel}")


def _import_relocdd():
    """Import relocDD-py from the pip-installed package."""
    if not _relocdd_installed():
        fail("relocDD-py is not installed",
             hint=f"{INSTALL_HINT}; see docs/install.md")
    _patch_installed_sources()
    try:
        from ph2dt.ph2dt import ph2dt
        from ph2dt.ph2dt_files import ph2dt_input
        from hypoDD.hypoDD import hypoDD
        from hypoDD.hypoDD_files import hypoDD_input
        return ph2dt, ph2dt_input, hypoDD, hypoDD_input
    except Exception as exc:  # noqa: BLE001
        fail(f"failed to import relocDD-py: {exc}",
             hint="ensure numpy/scipy/matplotlib/obspy/utm are installed, then "
                  f"{INSTALL_HINT}")


def _parse_time(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    # strip any trailing timezone offset (e.g. +00:00 / +0000)
    for sep in ("+", "-"):
        if sep in s[10:]:
            s = s[:10] + " " + s[11:].split(sep)[0]
            break
    return datetime.fromisoformat(s)


def _finite(value, default: float = 0.0) -> float:
    """Coerce a catalog field to a finite float, mapping blank/NaN to `default`.

    ph2dt/hypoDD read these columns as plain numbers, so 'nan' is not a legal
    magnitude or error value.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _station_code(row, known: set[str] | None = None) -> str:
    """Normalize a station code from a CSV row.

    Prefers an explicit `station_code` column; otherwise parses the dotted
    `station` field (e.g. 'AK.CCB.' -> 'CCB').  Some pickers emit a location
    code in the last field (e.g. 'IU.COLA.00'), so when `known` is given the
    first component matching a station of the network is preferred, and a
    trailing numeric component is never taken as the station code.
    """
    if "station_code" in row.index and str(row["station_code"]).strip():
        return str(row["station_code"]).strip()
    parts = [p for p in str(row["station"]).strip().split(".") if p]
    if not parts:
        return str(row["station"]).strip()
    if known:
        lookup = {c.upper(): c for c in known}
        for part in parts:
            if part.upper() in lookup:
                return lookup[part.upper()]
    if len(parts) > 1 and parts[-1].isdigit():
        return parts[-2]
    return parts[-1]


def _assign_picks_to_events(catalog, picks, max_window_s: float = 120.0):
    """Assign each pick to the temporally-nearest event origin.

    picks.csv from PhaseNet has no event_id column, so we match by time
    proximity.  Returns a dict {event_index: [pick_rows]}.
    """
    origins = [_parse_time(str(ev["time"])) for _, ev in catalog.iterrows()]
    assigned: dict[int, list] = {i: [] for i in range(len(catalog))}
    for _, pk in picks.iterrows():
        try:
            pt = _parse_time(str(pk["time"]))
        except ValueError:
            continue
        best, best_dt = -1, None
        for i, ot in enumerate(origins):
            d = abs((pt - ot).total_seconds())
            if d <= max_window_s and (best_dt is None or d < best_dt):
                best, best_dt = i, d
        if best >= 0:
            assigned[best].append(pk)
    return assigned


def write_phase_dat(catalog, picks, out: Path, known: set[str]) -> int:
    """Write relocDD-py phase.dat from absolute-located catalog + picks.

    Event line:  # year month day hour minute sec lat lon depth mag herr verr res cuspid
    Phase line:  station travel_time weight phase

    The phase time column is the travel time relative to the event origin time
    (`t - t_origin`), NOT an absolute clock time.  ph2dt differences the two
    events' times for each common station/phase, so an absolute base such as
    seconds-of-day (0-86400) makes every event pair look like an outlier
    (ph2dt's |dt| acceptance test is a few seconds wide) and the run ends with
    zero linked event pairs.

    hypoDD allows at most one P and one S arrival per (event, station), so
    duplicate picks of the same phase at the same station are reduced to the
    earliest arrival at/after the origin time, and picks at stations absent
    from station.dat are dropped: ph2dt cannot match them, and they would
    otherwise consume its per-event MAXOBS allocation of `2 * n_stations`
    rows.  Returns number of events written.
    """
    max_rows = 2 * max(len(known), 1)
    assigned = _assign_picks_to_events(catalog, picks)
    n_events = 0
    n_written = 0
    with open(out, "w") as fh:
        for i, (_, ev) in enumerate(catalog.iterrows()):
            origin = _parse_time(str(ev["time"]))
            # seconds field: SSff (integer sec + 2-digit fraction, no dot)
            sec = origin.second + origin.microsecond / 1e6
            sec_field = f"{int(sec):02d}{int(round((sec % 1) * 100)):02d}"
            cuspid = int(str(ev["event_id"]).replace("ev", "").lstrip("0") or 0)
            fh.write(
                f"# {origin.year:04d} {origin.month:02d} {origin.day:02d} "
                f"{origin.hour:02d} {origin.minute:02d} {sec_field} "
                f"{ev['latitude']:.6f} {ev['longitude']:.6f} "
                f"{_finite(ev['depth_km']):.3f} "
                f"{_finite(ev.get('magnitude')):.1f} "
                f"{_finite(ev.get('err_h')):.2f} "
                f"{_finite(ev.get('err_z')):.2f} "
                f"{_finite(ev.get('rms_s')):.2f} {cuspid}\n"
            )
            # at most one arrival per (station, phase): keep the earliest
            # arrival at/after the origin, else the pick closest to it
            best: dict[tuple[str, str], tuple[tuple[int, float], float]] = {}
            for pk in assigned[i]:
                sta = _station_code(pk, known)
                phase = str(pk["phase"]).upper()
                if phase not in ("P", "S") or sta not in known:
                    continue
                try:
                    tt = (_parse_time(str(pk["time"])) - origin).total_seconds()
                except ValueError:
                    continue
                rank = (0, tt) if tt >= 0 else (1, -tt)
                key = (sta, phase)
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, tt)
            rows = sorted(best.items(), key=lambda kv: kv[1][1])
            if len(rows) > max_rows:
                warn(f"event {cuspid}: {len(rows)} phase rows truncated to "
                     f"ph2dt MAXOBS={max_rows}")
                rows = rows[:max_rows]
            for (sta, phase), (_rank, tt) in rows:
                fh.write(f"{sta:<8} {tt:12.4f} {1.0:4.1f} {phase}\n")
                n_written += 1
            n_events += 1
    n_seen = sum(len(v) for v in assigned.values())
    if n_seen > n_written:
        log(f"phase.dat: {n_written} phase rows written, "
            f"{n_seen - n_written} picks dropped (duplicate station/phase "
            f"or station absent from station.dat)")
    return n_events


def write_station_dat(stations, out: Path) -> set[str]:
    """Write relocDD-py station.dat: sta lat lon [elev_km].

    Returns the set of station codes written; callers pass it back to
    `_station_code` as `known` so dotted pick ids ('IU.COLA.00') resolve to a
    real station instead of a location code.
    """
    rows = [(s, _station_code(s)) for _, s in stations.iterrows()]
    known = {code for _, code in rows}
    codes = []
    with open(out, "w") as fh:
        for s, _ in rows:
            code = _station_code(s, known)
            codes.append(code)
            elev = float(s.get("elevation_m", 0.0) or 0.0) / 1000.0
            fh.write(f"{code:<8}{s['latitude']:>10.4f}{s['longitude']:>10.4f}"
                     f" {elev:.3f}\n")
    return set(codes)


def _default_picks_path(catalog_path: Path) -> Path:
    """Infer the standard picks CSV location from the catalog path.

    Pipeline layout: {w}/locate/catalog_abs.csv -> {w}/picks/phasenet_picks.csv
    """
    return catalog_path.parent.parent / "picks" / "phasenet_picks.csv"


def _count_dt_pairs(path: Path) -> int:
    """Count event-pair blocks ('# cuspid1 cuspid2') in a dt.ct file."""
    n = 0
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                n += 1
    return n


def _hypodd_log_hint(log_path: Path, max_tail: int = 4) -> str:
    """Summarise a hypoDD log so a failure is diagnosable from the CLI.

    `# Catalog P/S dtimes` and `NCTP/NCTS` show how much of the dt.ct hypoDD
    actually accepted: zeros mean the differential times were rejected (or
    silently skipped) upstream rather than the inversion diverging.
    """
    if not log_path.exists():
        return "hypodd.log not written"
    lines = [ln.strip() for ln in log_path.read_text().splitlines() if ln.strip()]
    keys = ("# Catalog P dtimes", "# Catalog S dtimes", "NCTP", "NCTS")
    hits = [ln for ln in lines if any(ln.startswith(k) for k in keys)]
    return " | ".join(hits[:4]) if hits else " | ".join(lines[-max_tail:])


def _emit_no_pairs(workdir: Path, n_input: int) -> None:
    """Report that ph2dt linked no event pairs and stop before the inversion.

    ph2dt keeps only differential times whose residual is physically possible
    (`|dt_obs - dt_pred| <= 0.5 s` against a constant velocity), so a catalog
    whose events are far apart in space *or* time yields zero pairs.  hypoDD
    then divides by the pair count; upstream relocDD-py raises
    ZeroDivisionError instead of reporting the condition, so we translate it
    into an actionable warning and leave the absolute catalog in place.
    """
    warn("ph2dt linked no event pairs; skipping the hypoDD inversion")
    emit({
        "status": "warning",
        "backend": "relocdd-py",
        "n_events_input": n_input,
        "n_relocated": 0,
        "warning": "ph2dt found no event pairs with physically consistent "
                   "differential times, so no relative relocation was attempted",
        "hint": "see ph2dt.log ('outliers', 'linked event pairs'). If nearly "
                "every arrival is an outlier, first verify phase.dat carries "
                "travel times relative to the event origin (t - t_origin) and "
                "not absolute clock times; otherwise the events are not "
                "clustered in space or origin time, and lowering MINLNK or "
                "raising MAXSEPE cannot create pairs the travel-time test "
                "rejects",
        "run_dir": str(workdir),
    })


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6 backend A: relocDD-py (pure Python DD) wrapper")
    parser.add_argument("--catalog", required=True, help="absolute-located catalog CSV")
    parser.add_argument("--waveforms",
                        help="preprocessed waveform dir (ignored by relocDD-py; "
                             "kept for CLI compatibility)")
    parser.add_argument("--stations", required=True, help="stations CSV")
    parser.add_argument("--picks",
                        help="PhaseNet picks CSV (default: inferred from --catalog)")
    parser.add_argument("--inp-template", dest="inp_template",
                        default=str(ASSETS / "relocdd_hypodd_inp_template.txt"))
    parser.add_argument("--ph2dt-template", dest="ph2dt_template",
                        default=str(ASSETS / "relocdd_ph2dt_inp_template.txt"))
    parser.add_argument("--output-dir", dest="output_dir", required=True)
    args = parser.parse_args()

    import pandas as pd

    ph2dt, ph2dt_input, hypoDD, hypoDD_input = _import_relocdd()
    workdir = ensure_dir(args.output_dir)
    datdir = ensure_dir(workdir / "datfiles")

    # --- Step 1: prepare relocDD-py input files ---
    log("Step 1/4: preparing phase.dat / station.dat / control files...")
    catalog = pd.read_csv(args.catalog)
    picks_path = Path(args.picks) if args.picks else _default_picks_path(Path(args.catalog))
    if not picks_path.exists():
        fail(f"picks CSV not found: {picks_path}",
             hint="pass --picks explicitly or place phasenet_picks.csv in the "
                  "standard pipeline location")
        return
    picks = pd.read_csv(picks_path)
    from common import load_stations
    stations = load_stations(args.stations)

    known = write_station_dat(stations, datdir / "station.dat")
    if not known:
        fail("no stations written to station.dat",
             hint="check the stations CSV: ph2dt sizes its phase arrays from "
                  "the station count and cannot run without stations")
        return
    n_events = write_phase_dat(catalog, picks, datdir / "phase.dat", known)
    if n_events == 0:
        fail("no events written to phase.dat",
             hint="check catalog/picks time alignment")
        return

    shutil.copy(args.ph2dt_template, workdir / "ph2dt.inp")
    shutil.copy(args.inp_template, workdir / "hypoDD.inp")

    # --- Step 2: run relocDD-py ph2dt (catalog differential times) ---
    log("Step 2/4: running relocDD-py ph2dt...")
    logfile = open(workdir / "ph2dt.log", "w")
    pinputs = ph2dt_input(logfile, str(workdir / "ph2dt.inp"), dd_version=1)
    # point data-file paths at datdir
    pinputs[0] = str(datdir / pinputs[0])
    pinputs[1] = str(datdir / pinputs[1])
    try:
        ph2dt(logfile, pinputs, datfol=str(datdir), outfol=str(workdir),
              reloctype=1, fileout=0, idata=2)
    except ZeroDivisionError:
        # pristine relocDD-py normalises offsets by the pair count, which is
        # zero as soon as ph2dt forms no pairs; that is an input-data
        # condition, not a crash.
        logfile.close()
        _emit_no_pairs(workdir, len(catalog))
        return
    except Exception as exc:  # noqa: BLE001
        logfile.close()
        fail(f"relocDD-py ph2dt failed: {exc}",
             hint="see ph2dt.log; check phase.dat/station.dat consistency")
        return
    logfile.close()

    # ph2dt writes dte.ct; hypoDD expects dt.ct
    dte = datdir / "dte.ct"
    n_pairs = _count_dt_pairs(dte) if dte.exists() else 0
    if n_pairs == 0:
        _emit_no_pairs(workdir, len(catalog))
        return
    shutil.copy(dte, datdir / "dt.ct")

    # --- Step 3: run relocDD-py hypoDD (inversion) ---
    log("Step 3/4: running relocDD-py hypoDD inversion...")
    logfile = open(workdir / "hypodd.log", "w")
    hinputs = hypoDD_input(logfile, str(workdir / "hypoDD.inp"))
    # point data-file paths at datdir (fn_cc, fn_ct, fn_sta, fn_eve)
    for i in (0, 1, 2, 3):
        hinputs[i] = str(datdir / hinputs[i])
    # point output paths at workdir (fn_loc, fn_reloc, fn_res, fn_stares, fn_srcpar)
    for i in (4, 5, 6, 7, 8):
        hinputs[i] = str(workdir / hinputs[i])
    try:
        hypoDD(logfile, hinputs, outfol=str(workdir), reloctype=1, fileout=0)
    except Exception as exc:  # noqa: BLE001
        logfile.close()
        fail(f"relocDD-py hypoDD failed: {exc}",
             hint=f"hypodd.log: {_hypodd_log_hint(workdir / 'hypodd.log')}; "
                  "see ph2dt.log for pair counts")
        return
    logfile.close()

    reloc = workdir / "hypoDD.reloc"
    if not reloc.exists():
        fail("relocDD-py hypoDD produced no hypoDD.reloc",
             hint=f"hypodd.log: {_hypodd_log_hint(workdir / 'hypodd.log')}")
        return

    # --- Step 4: parse hypoDD.reloc -> catalog_dd.csv ---
    # relocDD-py .reloc columns:
    #   cuspid lat lon depth x y z ex ey ez year month day hour minute sec mag cluster
    log("Step 4/4: parsing hypoDD.reloc...")
    relocations: dict[str, tuple[float, float, float]] = {}
    for line in reloc.read_text().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            cuspid = int(float(parts[0]))
            hypocentre = (float(parts[1]), float(parts[2]), float(parts[3]))
        except ValueError:
            continue
        relocations[f"ev{cuspid:06d}"] = hypocentre
    if not relocations:
        fail("hypoDD.reloc contains no parsable event rows",
             hint="check that ph2dt/hypoDD actually inverted a cluster")
        return

    # Emit the *whole* catalog, not only the relocated events: downstream
    # phases (magnitude, QC) read catalog_dd.csv as the final catalog and need
    # the origin times/magnitudes that only the absolute catalog carries.
    # Relocated events get the hypoDD hypocentre, the rest keep their absolute
    # location, and `relocation` records which is which.
    rows = []
    for _, ev in catalog.iterrows():
        row = ev.to_dict()
        hypocentre = relocations.get(str(ev.get("event_id")))
        if hypocentre is None:
            row["relocation"] = "absolute"
        else:
            row["latitude"], row["longitude"], row["depth_km"] = hypocentre
            row["relocation"] = "hypoDD"
        rows.append(row)

    n_relocated = sum(1 for r in rows if r["relocation"] == "hypoDD")
    if n_relocated == 0:
        fail("hypoDD.reloc event ids do not match the input catalog",
             hint=f"reloc ids: {sorted(relocations)[:5]}...; "
                  f"catalog ids: {list(catalog['event_id'].astype(str))[:5]}...")
        return

    out = workdir / "catalog_dd.csv"
    pd.DataFrame(rows).to_csv(out, index=False)

    rms_before = float(catalog["rms_s"].fillna(0).mean() or 0)
    emit({
        "status": "ok",
        "backend": "relocdd-py",
        "n_events_input": len(catalog),
        "n_relocated": n_relocated,
        "n_events_output": len(rows),
        "rms_before_s": round(rms_before, 4),
        "catalog_file": str(out),
        "run_dir": str(workdir),
        "note": "events that ph2dt could not link keep their absolute "
                "hypocentre; see the `relocation` column",
    })


if __name__ == "__main__":
    run_main(main)
