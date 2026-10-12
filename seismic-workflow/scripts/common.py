"""Shared utilities for seismic-workflow scripts.

Contract (see SKILL.md):
- stdout: structured JSON only
- stderr: human-readable logs
- exit code 0 = success, non-zero = failure
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def _force_utf8_streams() -> None:
    """Write stdout/stderr as UTF-8 regardless of the OS locale.

    Windows defaults pipes to the ANSI code page (e.g. cp936), which garbles
    non-ASCII JSON fields and raises UnicodeEncodeError on characters that code
    page lacks (→, Δ, ≥ ...).  The stdout contract is "structured JSON", and JSON
    is UTF-8, so the encoding is pinned here for every script.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - unusual/detached stream: keep default
            pass


_force_utf8_streams()


def log(msg: str) -> None:
    """Human-readable log line to stderr."""
    print(f"[INFO] {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def emit(payload: dict, exit_code: int = 0) -> None:
    """Emit the final JSON result to stdout and exit."""
    payload.setdefault("status", "ok" if exit_code == 0 else "error")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def fail(error: str, **extra) -> None:
    emit({"status": "error", "error": error, **extra}, exit_code=1)


def run_main(main_fn):
    """Decorator-style wrapper: catch exceptions and convert to JSON error."""
    try:
        main_fn()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        fail(f"{type(exc).__name__}: {exc}")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def count_files(directory: str | Path, pattern: str = "*") -> int:
    return sum(1 for f in Path(directory).rglob(pattern) if f.is_file())


# ---------------------------------------------------------------------------
# Catalog CSV schema shared across phases
# ---------------------------------------------------------------------------
CATALOG_COLUMNS = [
    "event_id", "time", "latitude", "longitude", "depth_km",
    "magnitude", "magnitude_type", "n_picks", "rms_s",
]

PICK_COLUMNS = [
    "station", "phase", "time", "probability", "amplitude",
]


def read_catalog(path: str | Path):
    import pandas as pd
    df = pd.read_csv(path)
    missing = [c for c in ("latitude", "longitude") if c not in df.columns]
    if missing:
        raise ValueError(f"catalog {path} missing columns: {missing}")
    return df


def write_catalog(df, path: str | Path) -> None:
    import pandas as pd
    ensure_dir(Path(path).parent)
    pd.DataFrame(df).to_csv(path, index=False)


def read_picks(path: str | Path):
    import pandas as pd
    df = pd.read_csv(path)
    for col in ("station", "phase", "time"):
        if col not in df.columns:
            raise ValueError(f"picks file {path} missing column: {col}")
    return df


def parse_tvel(path: str | Path):
    """Parse a .tvel (TauP/NLLoc layered velocity model) file.

    Returns list of dicts: {"vp": float, "vs": float, "top_km": float}.
    """
    layers = []
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                depth, vp = float(parts[0]), float(parts[1])
                vs = float(parts[2]) if parts[2].lower() != "nan" else vp / 1.73
                layers.append({"top_km": depth, "vp": vp, "vs": vs})
            except ValueError:
                continue  # header/comment lines
    if not layers:
        raise ValueError(f"no valid layers parsed from {path}")
    return layers


def stations_from_xml(xml_path: str | Path):
    """Extract station inventory to list of dicts."""
    from obspy import read_inventory
    inv = read_inventory(str(xml_path))
    rows = []
    for net in inv:
        for sta in net:
            rows.append({
                "station": f"{net.code}.{sta.code}.",
                "network": net.code,
                "station_code": sta.code,
                "latitude": sta.latitude,
                "longitude": sta.longitude,
                "elevation_m": sta.elevation or 0.0,
            })
    return rows


def save_stations_csv(rows, path: str | Path) -> None:
    import pandas as pd
    ensure_dir(Path(path).parent)
    pd.DataFrame(rows).to_csv(path, index=False)


def load_stations(path: str | Path):
    import pandas as pd
    df = pd.read_csv(path)
    # normalize column names commonly produced by GaMMA / our scripts
    rename = {
        "lat": "latitude", "lon": "longitude", "elevation": "elevation_m",
        "id": "station",
    }
    df = df.rename(columns=rename)
    required = ("station", "latitude", "longitude")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"stations file {path} missing columns: {missing}")
    return df


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km (vectorized-friendly numpy impl)."""
    import numpy as np
    lat1, lon1, lat2, lon2 = map(
        lambda x: np.asarray(x, dtype=float), (lat1, lon1, lat2, lon2))
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return r * 2 * np.arcsin(np.sqrt(a))


def deg2km_factor() -> float:
    return 111.19  # km per degree (approx at mid-latitudes)
