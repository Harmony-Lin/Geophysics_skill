"""Shared utilities for seismic-workflow scripts.

Contract (see SKILL.md):
- stdout: structured JSON only
- stderr: human-readable logs
- exit code 0 = success, non-zero = failure
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from contextlib import contextmanager
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

#: Script start reference (common is imported at the top of every phase script).
#: ``emit()`` reports ``now - _STARTED_AT`` as ``elapsed_s``.
_STARTED_AT = time.perf_counter()


def log(msg: str) -> None:
    """Human-readable log line to stderr."""
    print(f"[INFO] {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def format_duration(seconds: float) -> str:
    """Human-readable duration: ``0.42 s`` / ``12.34 s`` / ``1 m 05 s`` / ``1 h 02 m 03 s``."""
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, sec = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} m {sec:02d} s"
    return f"{minutes} m {sec:02d} s"


#: Pipeline phase keys as used by ``p99_run_pipeline.py`` -> readable label.
PHASE_LABELS = {
    "preprocess": "P2 预处理",
    "pick": "P3 相位拾取",
    "associate": "P4 事件关联",
    "locate": "P5 绝对定位",
    "hypodd": "P6 相对定位",
    "magnitude": "P7 震级计算",
    "qc": "P8 质检与制图",
}


def phase_label(key: str) -> str:
    """``"preprocess"`` -> ``"P2 预处理"``; unknown keys are returned as-is."""
    return PHASE_LABELS.get(key, key)


class StepTimer:
    """Wall-clock timing for named steps: logs each one and collects a summary.

    Keeps the number that reaches stderr and the JSON payload identical::

        timer = StepTimer()
        with timer.step("绘制震中分布图"):
            plot(...)
        emit({..., "timings": timer.to_dict()})
    """

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self._started = time.perf_counter()

    @contextmanager
    def step(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, time.perf_counter() - t0)

    def record(self, name: str, seconds: float) -> None:
        self.steps.append({"name": name, "seconds": round(seconds, 3)})
        log(f"{name}: {format_duration(seconds)}")

    @staticmethod
    def step_label(step: dict) -> str:
        """``{"name", "seconds", "count"?}`` -> ``"bandpass filter ×5002"``."""
        name = str(step.get("name", "?"))
        count = step.get("count")
        return f"{name} ×{count}" if isinstance(count, int) and count > 1 else name

    @staticmethod
    def step_detail(steps: list[dict]) -> str:
        """``[{"name", "seconds"}]`` -> ``"read mseed 1.20 s、bandpass filter 13.11 s"``."""
        return "、".join(
            f"{StepTimer.step_label(s)} {format_duration(float(s.get('seconds') or 0.0))}"
            for s in steps if isinstance(s, dict) and s.get("name"))

    @property
    def total_s(self) -> float:
        """Seconds elapsed since the timer was created."""
        return round(time.perf_counter() - self._started, 3)

    def to_dict(self) -> dict:
        return {"total_s": self.total_s, "steps": list(self.steps)}

    def log_summary(self, title: str = "耗时汇总") -> None:
        detail = self.step_detail(self.steps)
        log(f"{title}: 总计 {format_duration(self.total_s)}"
            + (f"（{detail}）" if detail else ""))


class CumulativeTimer(StepTimer):
    """Like :class:`StepTimer`, but repeated ``step()`` calls with one name add up.

    Phase 2 loops over ~5000 traces and times each operation inside that loop;
    logging every single call would flood stderr.  Here the per-name total (and
    the number of calls) is accumulated and reported once by
    :meth:`~StepTimer.log_summary`.  ``to_dict()`` keeps the :class:`StepTimer`
    shape — plus a ``count`` — so report rendering needs no special case::

        timer = CumulativeTimer()
        for tr in stream:
            with timer.step("remove response"):
                ...
        timer.log_summary("Phase 2 内部步骤耗时")
    """

    def __init__(self) -> None:
        super().__init__()
        self._by_name: dict[str, dict] = {}

    @contextmanager
    def step(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - t0)

    def add(self, name: str, seconds: float) -> None:
        """Accumulate ``seconds`` under ``name`` (creating the entry on first use)."""
        entry = self._by_name.get(name)
        if entry is None:
            entry = {"name": name, "seconds": 0.0, "count": 0}
            self._by_name[name] = entry
            self.steps.append(entry)
        entry["seconds"] = round(float(entry["seconds"]) + float(seconds), 3)
        entry["count"] += 1


def emit(payload: dict, exit_code: int = 0) -> None:
    """Emit the final JSON result to stdout and exit."""
    payload.setdefault("status", "ok" if exit_code == 0 else "error")
    payload.setdefault("elapsed_s",
                       round(time.perf_counter() - _STARTED_AT, 3))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def fail(error: str, **extra) -> None:
    emit({"status": "error", "error": error, **extra}, exit_code=1)


def run_main(main_fn):
    """Decorator-style wrapper: catch exceptions and convert to JSON error."""
    t0 = time.perf_counter()
    try:
        main_fn()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        fail(f"{type(exc).__name__}: {exc}")
    finally:
        # stderr only, so the stdout JSON contract is untouched
        log(f"脚本耗时: {format_duration(time.perf_counter() - t0)}")


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
