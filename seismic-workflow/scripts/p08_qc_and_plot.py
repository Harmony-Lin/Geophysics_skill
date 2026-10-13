#!/usr/bin/env python3
"""Phase 8: QC and plotting — epicenter map, G-R relation, depth histogram,
residuals, and a Markdown report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import (StepTimer, emit, ensure_dir, fail, format_duration,  # noqa: E402
                    load_stations, log, phase_label, run_main, warn)


def _event_labels(catalog, max_labels: int = 30):
    """Yield ``(longitude, latitude, label)`` for a subset of the events.

    The label is "<magnitude> <MM-DD HH:MM>"; when a catalog is larger than
    ``max_labels`` only every n-th event is labelled, so the map stays legible.
    """
    import pandas as pd
    step = max(1, int(np.ceil(len(catalog) / max_labels)))
    for i, row in enumerate(catalog.itertuples()):
        if i % step:
            continue
        mag = pd.to_numeric(pd.Series([getattr(row, "magnitude", None)]),
                            errors="coerce").iloc[0]
        parts = [f"M{mag:.1f}"] if pd.notna(mag) else []
        t = getattr(row, "time", None)
        if t is not None and not pd.isna(t):
            try:
                parts.append(str(pd.Timestamp(t).strftime("%m-%d %H:%M")))
            except (TypeError, ValueError):
                pass
        if parts:
            yield row.longitude, row.latitude, " ".join(parts)


def _annotate_events(ax, catalog) -> None:
    """Annotate event time + magnitude next to each epicenter."""
    for lon, lat, label in _event_labels(catalog):
        ax.annotate(label, (lon, lat), textcoords="offset points",
                    xytext=(6, 6), fontsize=6, color="darkred")


def _ne_scale(extent) -> str:
    """Cheapest Natural Earth resolution that still looks right."""
    span = max(extent[1] - extent[0], extent[3] - extent[2])
    return "10m" if span <= 2 else ("50m" if span <= 20 else "110m")


def _add_cartopy_basemap(ax, extent, admin1: bool = True) -> list:
    """Ocean/land/coastline plus national (and optionally state) borders.

    Each layer is added on its own so that missing Natural Earth data (or a
    shapely/GEOS quirk) only drops that layer instead of the whole basemap.
    """
    import cartopy.feature as cfeature
    scale = _ne_scale(extent)
    layers = [
        ("ocean", cfeature.OCEAN, dict(facecolor="#e8f1fa", zorder=0)),
        ("land", cfeature.LAND, dict(facecolor="#f4f1e8", zorder=1)),
        ("coastline", cfeature.COASTLINE,
         dict(edgecolor="#1a1a1a", linewidth=0.6, zorder=2)),
        ("country_borders", cfeature.BORDERS,
         dict(edgecolor="#b15928", linewidth=0.7, zorder=2)),
    ]
    if admin1:
        layers.append(("province_borders", cfeature.STATES,
                       dict(edgecolor="#888888", linewidth=0.4,
                            linestyle="--", zorder=2)))
    drawn = []
    for name, feature, style in layers:
        try:
            ax.add_feature(feature.with_scale(scale), **style)
        except Exception as exc:  # missing or broken Natural Earth data
            warn(f"basemap layer {name} skipped ({exc})")
            continue
        drawn.append(name) 
    return drawn


def _geo_figure_layout(extent):
    """Figure size + axes rectangle matched to the map aspect.

    Cartopy keeps an equal aspect, so a fixed ``figsize`` plus automatic
    layout (``tight_layout``) leaves big blank margins and — with labeled
    gridlines — trips a gridliner/GEOS bug that aborts the whole render.
    Sizing the figure from the extent avoids both.
    """
    span_x = max(extent[1] - extent[0], 1e-9)
    span_y = max(extent[3] - extent[2], 1e-9)
    width = 9.0
    height = float(np.clip(width * span_y / span_x + 0.8, 5.0, 12.0))
    return (width, height), [0.07, 0.08, 0.77, 0.86]


def _draw_epicenter_figure(catalog, stations, mags, extent, *, cartopy=True,
                           admin1=True, gridline_labels=True):
    """Draw the epicenter figure once. Returns (fig, backend, layers)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    layers = []
    cax = None
    if cartopy:
        import cartopy.crs as ccrs
        crs = ccrs.PlateCarree()
        figsize, rect = _geo_figure_layout(extent)
        fig = plt.figure(figsize=figsize)
        ax = fig.add_axes(rect, projection=crs)
        ax.set_extent(list(extent), crs=crs)
        layers = _add_cartopy_basemap(ax, extent, admin1=admin1)
        gl = ax.gridlines(draw_labels=gridline_labels, linestyle=":",
                          linewidth=0.4, color="gray")
        if gridline_labels:
            gl.top_labels = gl.right_labels = False
        cax = fig.add_axes([0.88, 0.25, 0.022, 0.5])
        backend = "cartopy"
    else:
        fig, ax = plt.subplots(figsize=(8, 7))
        backend = "matplotlib"

    sc = ax.scatter(catalog["longitude"], catalog["latitude"], c=mags,
                    cmap="YlOrRd", edgecolors="black", linewidths=0.3,
                    s=12 + 8 * mags, label="earthquake", zorder=3)
    ax.scatter(stations["longitude"], stations["latitude"], marker="^",
               c="blue", s=50, edgecolors="black", linewidths=0.4,
               label="station", zorder=4)
    for row in stations.itertuples():
        code = getattr(row, "station_code", str(row.station))
        ax.annotate(code, (row.longitude, row.latitude),
                    textcoords="offset points", xytext=(0, 7),
                    ha="center", fontsize=7, color="blue", zorder=5)
    _annotate_events(ax, catalog)
    try:
        if cax is not None:
            fig.colorbar(sc, cax=cax, label="Magnitude")
        else:
            fig.colorbar(sc, ax=ax, shrink=0.7, label="Magnitude")
    except Exception:
        pass
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Epicenter distribution")
    ax.legend(loc="upper left")
    if not cartopy:  # Cartopy sizes itself from the extent already
        fig.tight_layout()
    return fig, backend, layers


def plot_epicenter_map(catalog, stations, out_png: Path):
    """Plot the epicenter map. Returns (backend name, basemap layers drawn).

    Backend order: PyGMT → Cartopy → plain Matplotlib. The Cartopy layers are
    tried most-complete-first and degraded one at a time, so a gridline or
    state-border problem only drops that layer.
    """
    lon_min = float(min(catalog["longitude"].min(),
                        stations["longitude"].min())) - 0.3
    lon_max = float(max(catalog["longitude"].max(),
                        stations["longitude"].max())) + 0.3
    lat_min = float(min(catalog["latitude"].min(),
                        stations["latitude"].min())) - 0.3
    lat_max = float(max(catalog["latitude"].max(),
                        stations["latitude"].max())) + 0.3
    extent = (lon_min, lon_max, lat_min, lat_max)

    try:
        import pygmt
        fig = pygmt.Figure()
        fig.basemap(region=list(extent), projection="M12c",
                    frame=["af", 'WSne+t"Epicenter distribution"'])
        fig.coast(land="lightgray", water="white", borders=1, roads="gray")
        fig.plot(x=catalog["longitude"], y=catalog["latitude"],
                 style="c0.15c", color="red", pen="black",
                 label="earthquake")
        fig.plot(x=stations["longitude"], y=stations["latitude"],
                 style="t0.35c", color="blue", pen="black", label="station")
        for row in stations.itertuples():
            fig.text(x=row.longitude, y=row.latitude,
                     text=getattr(row, "station_code", str(row.station)),
                     offset="0c/0.45c", font="7p,Helvetica-Bold,blue")
        _pygmt_event_text(fig, catalog)
        fig.legend(position="JTR+jTR+o0.2c")
        fig.savefig(str(out_png), dpi=200)
        return "pygmt", ["land", "ocean", "country_borders"]
    except Exception as exc:
        warn(f"PyGMT unavailable ({exc}); falling back to Matplotlib/Cartopy")

    try:
        import cartopy.feature  # noqa: F401
        have_cartopy = True
    except Exception as exc:
        warn(f"Cartopy unavailable ({exc}); plain Matplotlib map")
        have_cartopy = False

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    mags = pd.to_numeric(catalog.get("magnitude"), errors="coerce").fillna(0.0)

    attempts = []
    if have_cartopy:
        attempts += [
            dict(cartopy=True, admin1=True, gridline_labels=True),
            dict(cartopy=True, admin1=True, gridline_labels=False),
            dict(cartopy=True, admin1=False, gridline_labels=False),
        ]
    attempts.append(dict(cartopy=False, admin1=False, gridline_labels=False))

    for opts in attempts:
        fig = None
        backend, layers = "matplotlib", []
        try:
            fig, backend, layers = _draw_epicenter_figure(
                catalog, stations, mags, extent, **opts)
            fig.savefig(out_png, dpi=200)
        except Exception as exc:
            warn(f"{backend} render failed "
                 f"({', '.join(layers) if layers else 'no basemap'}): {exc}; "
                 f"retrying with a simpler basemap")
            if fig is not None:
                plt.close(fig)
            continue
        plt.close(fig)
        return backend, layers
    fail("epicenter map could not be rendered by any backend")


def _pygmt_event_text(fig, catalog) -> None:
    """Annotate event time+magnitude on a PyGMT figure."""
    for lon, lat, label in _event_labels(catalog):
        fig.text(x=lon, y=lat, text=label,
                 offset="0.15c/0.15c", font="6p,Helvetica,darkred")


def plot_gr(catalog, out_png: Path):
    mags = np.asarray(catalog["magnitude"].dropna().astype(float))
    if len(mags) == 0:
        warn("no magnitudes in catalog — skipping G-R plot")
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    bins = np.arange(np.floor(mags.min()), np.ceil(mags.max()) + 0.1, 0.1)
    hist, edges = np.histogram(mags, bins=bins)
    cum = np.cumsum(hist[::-1])[::-1]
    centers = (edges[:-1] + edges[1:]) / 2

    # completeness magnitude: bin with max b-value stability (simple Mmax of
    # the "goodness" heuristic: largest Mc where N(M>=Mc)>=10)
    valid = cum >= 10
    mc = float(centers[valid][0]) if valid.any() else float(np.median(mags))

    sel = centers >= mc
    if sel.sum() >= 2:
        A = np.polyfit(centers[sel], np.log10(cum[sel]), 1)
        b_value = -A[0]
    else:
        A, b_value = [0, 0], None

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.semilogy(centers, cum, "o-", label="cumulative")
    if b_value:
        ax.semilogy(centers[sel], 10 ** np.polyval(A, centers[sel]),
                    "--", label=f"fit: b={b_value:.2f}, Mc={mc:.1f}")
    ax.set_xlabel("Magnitude")
    ax.set_ylabel("N (≥ M)")
    ax.set_title("Gutenberg–Richter relation")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return {"mc": round(mc, 2), "b": round(b_value, 3) if b_value else None}


def plot_depth_hist(catalog, out_png: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    depths = catalog["depth_km"].dropna().astype(float)
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist(depths, bins=min(30, max(5, len(depths))), color="steelblue")
    ax.set_xlabel("Depth (km)")
    ax.set_ylabel("Count")
    ax.set_title("Depth distribution")
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)


def plot_residuals(catalog, out_png: Path):
    import pandas as pd
    if "rms_s" not in catalog.columns:
        warn("no rms_s column — skipping residual plot")
        return None
    rms = pd.to_numeric(catalog["rms_s"], errors="coerce").dropna()
    if len(rms) == 0:
        warn("no usable rms_s values — skipping residual plot")
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist(rms, bins=30, color="darkorange")
    ax.set_xlabel("Travel-time RMS (s)")
    ax.set_ylabel("Count")
    ax.set_title(f"Residuals (mean={rms.mean():.3f} s)")
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return {"mean_rms_s": round(float(rms.mean()), 4),
            "median_rms_s": round(float(rms.median()), 4)}


PICK_WINDOW_PRE_S = 5.0    # left padding before the cluster's first pick
PICK_WINDOW_MIN_S = 30.0   # floor for the auto-sized window
PICK_WINDOW_MAX_S = 300.0  # ceiling: beyond this the figure is unreadable


def plot_pick_samples(picks_csv, waveform_dir, out_png: Path,
                      event: str = "main", window_s: float | None = None,
                      ) -> dict | None:
    """Phase 3 sample figure: waveforms with PhaseNet picks overlaid.

    Plots ONE event — by default the mainshock (the event with the most
    picks). Each station that picked the event gets its own subplot; the
    vertical (Z) channel is preferred for the waveform, and the P (red) /
    S (blue) pick times of that station are marked on its subplot.

    The x axis starts at the cluster's first pick. ``window_s=None`` (the
    default) sizes it from the cluster itself, because a fixed 60 s window
    silently drops every later arrival (regional P coda, S, and any nearby
    event merged into the same cluster) and leaves subplots with no pick
    line at all. The origin-relative time base is ``t0`` (first pick), not
    a real origin time, so treat the axis as relative motion time.
    """
    import pandas as pd
    picks = pd.read_csv(picks_csv)
    if len(picks) == 0:
        warn("no picks — skipping pick sample plot")
        return None

    from obspy import read

    picks["time"] = pd.to_datetime(picks["time"], errors="coerce",
                                   format="mixed", utc=True)
    picks = picks.dropna(subset=["time"])
    if len(picks) == 0:
        warn("no parseable pick times — skipping pick sample plot")
        return None

    # cluster picks into events by gaps > 120 s in pick time
    picks = picks.sort_values("time").reset_index(drop=True)
    gap = pd.Timedelta(seconds=120)
    picks["event"] = (picks["time"].diff() > gap).cumsum()
    order = (picks.groupby("event")["time"].min()
             .sort_values().index.tolist())

    # choose the event: "main" = most picks (mainshock), or an index
    def _mainshock():
        return max(order, key=lambda e: len(picks[picks.event == e]))

    if str(event).lower() in ("main", "mainshock", "largest"):
        ev_idx = _mainshock()
    else:
        try:
            ev_idx = order[int(event)]
        except (ValueError, IndexError):
            warn(f"invalid --event {event!r}; falling back to mainshock")
            ev_idx = _mainshock()

    ev = picks[picks.event == ev_idx]
    t0 = ev["time"].min()

    # The window has to cover the whole cluster: within one cluster the picks
    # span far more than a fixed 60 s, and a too-narrow window leaves subplots
    # without a single pick line.
    span_s = float((ev["time"].max() - t0).total_seconds())
    auto_window = window_s is None or window_s <= 0
    if auto_window:
        win = float(np.clip(span_s + PICK_WINDOW_PRE_S,
                            PICK_WINDOW_MIN_S, PICK_WINDOW_MAX_S))
        log(f"pick window auto-sized to {win:.0f} s "
            f"(event #{ev_idx} spans {span_s:.0f} s)")
    else:
        win = float(window_s)
    pre = -PICK_WINDOW_PRE_S

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    files = sorted(Path(waveform_dir).rglob("*.mseed")) + \
            sorted(Path(waveform_dir).rglob("*.miniseed"))
    streams = {}
    for f in files:
        try:
            st = read(str(f), headonly=False)
            for tr in st:
                streams.setdefault(tr.id, []).append(tr)
        except Exception as exc:
            warn(f"unreadable waveform skipped: {f.name} ({exc})")

    # one subplot per station that picked the event
    stations = sorted(ev["station"].unique())
    n_rows = len(stations)
    fig, axes = plt.subplots(n_rows, 1, figsize=(11, 2.6 * n_rows),
                             squeeze=False)

    info = {"event": ev_idx, "n_stations": n_rows, "window_s": round(win, 1),
            "window_auto": auto_window, "stations_plotted": 0,
            "picks_plotted": 0, "picks_off_window": 0}
    for r, sta in enumerate(stations):
        ax = axes[r][0]
        sta_picks = ev[ev["station"] == sta]
        cands = [trs for tid, lst in streams.items()
                 if tid.split(".")[1] == sta.split(".")[1]
                 for trs in lst]
        # prefer the vertical (Z) channel of this station
        cands.sort(key=lambda tr: 0 if tr.id.endswith("Z") else 1)
        plotted = False
        for tr in cands:
            delta = (pd.Timestamp(tr.stats.starttime.datetime).tz_localize("UTC")
                     - t0).total_seconds()
            tt = np.arange(tr.stats.npts) / tr.stats.sampling_rate + delta
            m = (tt >= pre) & (tt <= win)
            if not m.any():
                continue
            ax.plot(tt[m], tr.data[m], lw=0.7, color="black")
            plotted = True
            break
        n_off = 0
        for prow in sta_picks.itertuples():
            dt = (prow.time - t0).total_seconds()
            if dt < pre or dt > win:
                n_off += 1
                continue
            color = "red" if str(prow.phase).upper().startswith("P") else "blue"
            ax.axvline(dt, color=color, lw=1.0, alpha=0.8)
            info["picks_plotted"] += 1
        info["picks_off_window"] += n_off
        ax.set_xlim(pre, win)
        ax.set_ylabel("counts")
        n_win = len(sta_picks) - n_off
        title = f"{sta}  ({n_win} picks"
        title += f", +{n_off} off-window)" if n_off else ")"
        ax.set_title(title, fontsize=9, loc="left")
        if plotted:
            info["stations_plotted"] += 1

    from matplotlib.lines import Line2D
    handles = [Line2D([], [], color="red", lw=1.2, label="P pick"),
               Line2D([], [], color="blue", lw=1.2, label="S pick")]
    axes[-1][0].set_xlabel("Time since first pick t0 (s)")
    fig.legend(handles=handles, loc="upper right", ncol=2)
    if info["picks_off_window"]:
        warn(f"{info['picks_off_window']} of {len(ev)} picks fall outside the "
             f"{win:.0f} s window and are not drawn "
             "(raise --window to show more)")
    fig.suptitle(f"PhaseNet picks — event {ev_idx + 1} "
                 f"(t0≈{t0.strftime('%Y-%m-%d %H:%M:%S')} UTC, "
                 f"window {win:.0f} s, "
                 f"{len(ev)} picks)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return info


def _fmt(value, spec: str = ".2f", default: str = "—") -> str:
    """Format a number that may be missing/NaN for the Markdown report."""
    import pandas as pd
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not np.isfinite(value):
        return default
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


def _fmt_time(value) -> str:
    """UTC timestamp as ``YYYY-MM-DD HH:MM:SS`` (placeholder when missing)."""
    import pandas as pd
    if value is None:
        return "—"
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return "—"
    if pd.isna(ts):
        return "—"
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _md_table(headers, rows) -> str:
    """Render ``rows`` as a GitHub-flavoured Markdown table."""
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _load_timings(path: str | None, outdir: Path) -> dict | None:
    """Read ``p99_run_pipeline.py``'s timing file (``None`` when unavailable).

    Without ``--timings`` the pipeline's own location is tried, so a Phase 8
    run inside the standard layout (``data/qc`` + ``data/pipeline_logs``)
    picks the breakdown up automatically.
    """
    cand = (Path(path) if path
            else Path(outdir).parent / "pipeline_logs" / "timings.json")
    if not cand.exists():
        if path:
            warn(f"timings file not found: {cand}; the report will only carry "
                 "this phase's own duration")
        return None
    try:
        data = json.loads(cand.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"could not read timings file {cand}: {exc}")
        return None
    log(f"Loaded phase timings from {cand}")
    return data if isinstance(data, dict) else None


def _timing_block(timings: dict | None, self_elapsed_s: float | None,
                  self_timings: dict | None = None) -> str:
    """Markdown table of per-phase durations (P2-P7 from the pipeline + P8).

    A ``qc`` entry in the timing file is dropped: it comes from an earlier
    pipeline run, while this run measures P8 itself (``self_elapsed_s``), and
    keeping both would list Phase 8 twice.

    Phases that time their own sub-steps (``steps[].timings`` in the timing
    file, e.g. Phase 2's per-operation breakdown) get a detail line under the
    table, so a slow phase can be read without opening its log.
    """
    rows: list[tuple[str, float]] = []
    details: list[str] = []
    for step in (timings or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("phase", "")) == "qc":
            continue
        label = str(step.get("label")
                    or phase_label(str(step.get("phase", "?"))))
        rows.append((label, float(step.get("seconds") or 0.0)))
        inner = _inner_steps(step.get("timings"))
        if inner:
            cache = step.get("response_cache")
            if isinstance(cache, dict) and "responses_evaluated" in cache:
                inner += (f"（仪器响应仅实际评估 "
                          f"{cache['responses_evaluated']} 条道，"
                          f"{cache.get('responses_reused', 0)} 条复用缓存）")
            details.append(f"- **{label}** 内部步骤：{inner}")
    if self_elapsed_s is not None:
        label = "P8 质检与制图（本阶段）"
        rows.append((label, float(self_elapsed_s)))
        inner = _inner_steps(self_timings)
        if inner:
            details.append(f"- **{label}** 内部步骤：{inner}")
    if not rows:
        return "（本次运行未记录耗时）"
    total = sum(secs for _, secs in rows)
    table = _md_table(
        ["阶段", "耗时", "占比"],
        [[name, format_duration(secs),
          f"{100 * secs / total:.1f}%" if total > 0 else "—"]
         for name, secs in rows]
        + [["**合计**", f"**{format_duration(total)}**", "100.0%"]])
    note = ("> P2–P7 为流水线运行器（`p99_run_pipeline.py` / `tests/verify_skill.py`）"
            "测得的各阶段子进程墙钟时间"
            "（写入 `data/pipeline_logs/timings.json`，各阶段明细另见同目录 "
            "`<阶段>.log` 末尾的 `---ELAPSED---`）；P8 一行为本脚本内部计时"
            "（不含解释器启动与报告写入本身），其内部步骤见上方明细。")
    if timings is None:
        note = ("> 未找到流水线计时文件（独立运行 P8 时属正常），下表仅含本阶段"
                "耗时；P2–P7 每阶段的耗时见 `data/pipeline_logs/<阶段>.log`。")
    blocks = [table]
    if details:
        blocks.append("**各阶段内部步骤**（同一阶段内多次调用会累加，`×N` 为"
                      "调用次数）\n\n" + "\n".join(details))
    blocks.append(note)
    return "\n\n".join(blocks)


def _inner_steps(timings: dict | None) -> str:
    """``{"steps": [{"name", "seconds", "count"?}]}`` -> human-readable list."""
    if not isinstance(timings, dict):
        return ""
    return StepTimer.step_detail(timings.get("steps") or [])


def _azimuth_deg(lat0, lon0, lat1, lon1) -> float:
    """Initial great-circle azimuth from (lat0, lon0) to (lat1, lon1), °N."""
    p0, p1 = np.radians(lat0), np.radians(lat1)
    dl = np.radians(lon1 - lon0)
    x = np.sin(dl) * np.cos(p1)
    y = np.cos(p0) * np.sin(p1) - np.sin(p0) * np.cos(p1) * np.cos(dl)
    return float(np.degrees(np.arctan2(x, y)) % 360.0)


def _azimuthal_gap(lat0, lon0, coords) -> float | None:
    """Largest azimuthal gap (°); returns None when fewer than 3 stations."""
    if len(coords) < 3:
        return None
    az = sorted(_azimuth_deg(lat0, lon0, lat, lon) for lat, lon in coords)
    gaps = [(az[(i + 1) % len(az)] - az[i]) % 360.0 for i in range(len(az))]
    return round(float(max(gaps)), 1)


def _station_coords(stations) -> dict:
    """``(NET, STA)`` → ``(lat, lon)``, to map diagnostic ``trace_id`` rows."""
    coords = {}
    has_net = {"network", "station_code"} <= set(stations.columns)
    for row in stations.itertuples():
        if has_net:
            key = (str(row.network).upper(), str(row.station_code).upper())
        else:
            parts = str(row.station).split(".")
            if len(parts) < 2:
                continue
            key = (parts[0].upper(), parts[1].upper())
        try:
            coords[key] = (float(row.latitude), float(row.longitude))
        except (TypeError, ValueError):
            continue
    return coords


def _ml_stats_from_diag(catalog_path, catalog, stations) -> dict:
    """Per-event summary of the single-station ML diagnostics (p07).

    p07 writes one row per trace carrying a Wood–Anderson amplitude to
    ``<catalog>.magnitude_diag.csv``; this aggregates it into 参与台站/道数、
    离散度、震中距范围与方位角间隙。离散度沿用 p07 的口径（偏离单台中位
    震级 0.5 级以上视为异常并剔除），便于和目录中的最终 ML 对齐。
    """
    import pandas as pd
    diag_path = Path(catalog_path).with_suffix(".magnitude_diag.csv")
    if not diag_path.exists():
        warn(f"magnitude diagnostics not found ({diag_path.name}); "
             f"per-station ML statistics omitted from the report")
        return {}
    try:
        diag = pd.read_csv(diag_path)
    except Exception as exc:
        warn(f"magnitude diagnostics unreadable ({exc}); per-station ML "
             f"statistics omitted")
        return {}
    if not {"event_id", "single_ml"} <= set(diag.columns):
        warn("magnitude diagnostics lacks event_id/single_ml; skipped")
        return {}

    coords = _station_coords(stations)
    ev_loc = {}
    for row in catalog.itertuples():
        try:
            ev_loc[str(row.event_id)] = (float(row.latitude),
                                         float(row.longitude))
        except (TypeError, ValueError):
            continue
    has_trace = "trace_id" in diag.columns

    stats = {}
    for eid, grp in diag.groupby("event_id"):
        ml = pd.to_numeric(grp["single_ml"], errors="coerce").dropna()
        if ml.empty:
            continue
        med = float(ml.median())
        kept = ml[np.abs(ml.to_numpy() - med) <= 0.5]
        if kept.empty:
            kept = ml

        stations_used = []
        if has_trace:
            for tid in grp["trace_id"].astype(str):
                parts = tid.split(".")
                stations_used.append(
                    (parts[0].upper(), parts[1].upper())
                    if len(parts) >= 2 else ("", parts[0].upper()))
        uniq = sorted(set(stations_used))

        dist = pd.to_numeric(grp.get("epicentral_km"), errors="coerce").dropna()
        loc = ev_loc.get(str(eid))
        gap = None
        if loc is not None and uniq:
            gap = _azimuthal_gap(loc[0], loc[1],
                                 [coords[k] for k in uniq if k in coords])
        stats[str(eid)] = {
            "n_traces": int(ml.size),
            "n_stations": len(uniq) if uniq else None,
            "ml_min": float(ml.min()),
            "ml_max": float(ml.max()),
            "ml_median": med,
            "ml_std": float(kept.std()) if kept.size > 1 else None,
            "n_rejected": int(ml.size - kept.size),
            "dist_min_km": float(dist.min()) if not dist.empty else None,
            "dist_max_km": float(dist.max()) if not dist.empty else None,
            "az_gap_deg": gap,
        }
    log(f"Magnitude diagnostics: single-station ML summarised for "
        f"{len(stats)}/{len(catalog)} events")
    return stats


def _quality_flag(n_stations, rms_s, has_mag: bool = True, *, rms_ref=None,
                  az_gap=None) -> str:
    """Rough per-event quality grade.

    Graded mainly on station count and azimuthal coverage; the travel-time
    RMS is only used as an outlier check against the catalog median, because
    ``rms_s`` in the final catalog reflects pick-time scatter rather than a
    calibrated travel-time residual level.
    """
    if not has_mag:
        return "无震级"
    if not n_stations:
        return "待查"
    if az_gap is not None and az_gap > 270:
        return "待查"
    if n_stations < 5:
        return "待查"
    if rms_ref and rms_s is not None and rms_s > 2.0 * rms_ref:
        return "一般"
    if n_stations >= 8 and (az_gap is None or az_gap <= 180):
        return "好"
    return "一般"


def _write_report(outdir: Path, catalog, stations, paths: dict, map_backend,
                  map_layers, gr, pick_info, backend_note,
                  timings: dict | None = None,
                  self_elapsed_s: float | None = None,
                  self_timings: dict | None = None) -> Path:
    """Write ``report.md``: 概要 / 统计 / 耗时 / 逐事件表格 / 完整性检查 / 图件."""
    timing_block = _timing_block(timings, self_elapsed_s, self_timings)
    import pandas as pd
    from datetime import datetime, timezone

    def num(col):
        if col not in catalog.columns:
            return pd.Series(dtype="float64")
        return pd.to_numeric(catalog[col], errors="coerce")

    mag = num("magnitude").dropna()
    depth = num("depth_km").dropna()
    rms = num("rms_s").dropna()
    picks_n = num("n_picks").dropna()
    lat, lon = num("latitude"), num("longitude")
    times = pd.to_datetime(catalog.get("time"), errors="coerce", utc=True)
    times = times.dropna()
    diag = _ml_stats_from_diag(paths["catalog"], catalog, stations)

    det = ""
    if len(times) >= 2:
        det = (f"{times.min().strftime('%Y-%m-%d %H:%M')} – "
               f"{times.max().strftime('%Y-%m-%d %H:%M')} UTC")
    n_networks = (stations["network"].nunique()
                  if "network" in stations.columns else None)

    # ── 逐事件表格 ──
    rms_ref = float(rms.median()) if len(rms) else None
    ev_rows, ml_rows = [], []
    per_event_stations = {}
    for row in catalog.itertuples():
        eid = str(getattr(row, "event_id", "") or "—")
        st = diag.get(eid, {})
        n_sta = st.get("n_stations")
        n_tr = st.get("n_traces")
        if n_sta is None and n_tr is None:
            # no diagnostics: the catalog only carries the retained-trace count
            n_sta = getattr(row, "n_stations_used", None)
            n_tr = None
        per_event_stations[eid] = n_sta
        mag_v = pd.to_numeric(
            pd.Series([getattr(row, "magnitude", None)]), errors="coerce").iloc[0]
        rms_v = pd.to_numeric(
            pd.Series([getattr(row, "rms_s", None)]), errors="coerce").iloc[0]
        rms_v = float(rms_v) if pd.notna(rms_v) else None
        ev_rows.append([
            eid, _fmt_time(getattr(row, "time", None)),
            _fmt(getattr(row, "latitude", None), ".4f"),
            _fmt(getattr(row, "longitude", None), ".4f"),
            _fmt(getattr(row, "depth_km", None), ".1f"),
            _fmt(mag_v, ".2f"),
            _fmt(n_sta, ".0f"),
            _fmt(n_tr, ".0f"),
            _fmt(getattr(row, "n_picks", None), ".0f"),
            _fmt(rms_v, ".2f"),
            _quality_flag(n_sta, rms_v, pd.notna(mag_v), rms_ref=rms_ref,
                          az_gap=st.get("az_gap_deg")),
        ])
        if eid in diag:
            s = diag[eid]
            ml_rows.append([
                eid,
                f"{_fmt(s['ml_min'], '.2f')} – {_fmt(s['ml_max'], '.2f')}",
                _fmt(s["ml_median"], ".2f"),
                _fmt(s["ml_std"], ".2f"),
                _fmt(s["n_rejected"], ".0f"),
                f"{_fmt(s['dist_min_km'], '.1f')} – "
                f"{_fmt(s['dist_max_km'], '.1f')}",
                _fmt(s["az_gap_deg"], ".1f"),
            ])

    ev_table = _md_table(
        ["事件", "发震时刻 (UTC)", "纬度", "经度", "深度 (km)", "ML",
         "台站数", "参与道数", "拾取数", "RMS (s)", "质量"], ev_rows)
    ml_table = ""
    if ml_rows:
        ml_table = _md_table(
            ["事件", "单台 ML 范围", "单台 ML 中位", "单台 ML 标准差",
             "剔除道数", "震中距 (km)", "方位角间隙 (°)"], ml_rows)

    # ── 完整性检查 ──
    checks = []
    if "event_id" in catalog.columns:
        dup_id = int(catalog["event_id"].duplicated().sum())
        checks.append(("event_id 唯一", dup_id == 0, f"重复 {dup_id} 个"))
    bad_loc = int((lat.isna() | lon.isna()).sum()) if len(lat) else len(catalog)
    checks.append(("经纬度完整", bad_loc == 0, f"缺失 {bad_loc} 个事件"))
    bad_depth = int(depth.isna().sum()) if "depth_km" in catalog.columns else 0
    checks.append(("深度完整", bad_depth == 0, f"缺失 {bad_depth} 个事件"))
    n_no_mag = len(catalog) - len(mag)
    checks.append(("震级完整", n_no_mag == 0, f"缺失 {n_no_mag} 个事件"))
    n_off = int(((lat.abs() > 90) | (lon.abs() > 180)).sum()) if len(lat) else 0
    checks.append(("坐标范围合理", n_off == 0, f"越界 {n_off} 个事件"))
    dup_t = int(times.duplicated().sum())
    checks.append(("发震时刻无重复", dup_t == 0, f"重复 {dup_t} 个事件"))
    few = [k for k, v in per_event_stations.items()
           if v is not None and v < 3]
    checks.append(("参与台站 ≥ 3", not few,
                   f"{len(few)} 个事件台站偏少" + (f"（{', '.join(few[:5])}）"
                                                if few else "")))
    check_lines = "\n".join(
        f"- {'✅' if ok else '⚠️'} {name}：{detail if not ok else '通过'}"
        for name, ok, detail in checks)

    # ── 输入数据 ──
    inputs = [f"- 事件目录: `{paths['catalog']}`（{len(catalog)} 个事件）",
              f"- 台站表: `{paths['stations']}`（{len(stations)} 个台站"
              + (f"，{n_networks} 个台网）" if n_networks else "）")]
    if paths.get("picks"):
        pick_line = f"- 拾取文件: `{paths['picks']}`"
        try:
            picks = pd.read_csv(paths["picks"], usecols=["phase"])
            counts = picks["phase"].astype(str).str.upper().value_counts()
            detail = "，".join(f"{k} {v}" for k, v in counts.items())
            pick_line += f"（{len(picks)} 条：{detail}）"
        except Exception:
            pass
        inputs.append(pick_line)
    if paths.get("waveforms") and pick_info:
        inputs.append(f"- 波形目录: `{paths['waveforms']}`（拾取样例图）")
    elif paths.get("waveforms"):
        inputs.append(f"- 波形目录: `{paths['waveforms']}`（未生成拾取样例图）")
    diag_path = Path(paths["catalog"]).with_suffix(".magnitude_diag.csv")
    if diag_path.exists():
        inputs.append(f"- 震级诊断: `{diag_path.name}`"
                      f"（{sum(s['n_traces'] for s in diag.values())} 条单台记录）")

    avg_sta = (np.mean([s["n_stations"] for s in diag.values()
                        if s["n_stations"]]) if diag else None)
    avg_tr = np.mean([s["n_traces"] for s in diag.values()]) if diag else None
    n_rejected = sum(s["n_rejected"] for s in diag.values()) if diag else None
    input_block = "\n".join(inputs)

    pick_note = ""
    if pick_info:
        win_note = (f"{pick_info.get('window_s', '—')} s"
                    + ("（自动按事件跨度取）"
                       if pick_info.get("window_auto") else "（手动指定）"))
        pick_note = (f"> 拾取样例图为聚类事件 #{pick_info['event']}"
                     f"（x 轴起点为该簇最早拾取 t0），窗口 {win_note}；绘制了 "
                     f"{pick_info['stations_plotted']}/"
                     f"{pick_info['n_stations']} 个台站、"
                     f"{pick_info['picks_plotted']} 条拾取。")
        if pick_info.get("picks_off_window"):
            pick_note += (f" 另有 {pick_info['picks_off_window']} 条拾取落在窗口外未绘制"
                          "（可用 `--window` 放宽）。")

    figures = "\n".join(
        f"![{title}]({fname})" for title, fname in
        [("震中分布", "epicenter_map.png"), ("G-R关系", "gr_relation.png"),
         ("深度分布", "depth_histogram.png"), ("走时残差", "residuals.png"),
         ("拾取样例", "pick_samples.png")]
        if (outdir / fname).exists())

    report = f"""# 地震目录处理报告

生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## 概要
- **事件总数**: {len(catalog)}
- **台站数**: {len(stations)}{f'（{n_networks} 个台网）' if n_networks else ''}
- **有震级的事件数**: {len(mag)} ({100 * len(mag) / max(len(catalog), 1):.0f}%)
- **目录时间范围**: {det or '—'}
- **制图后端**: {map_backend}
- **底图图层**: {', '.join(map_layers) if map_layers else '无（纯 Matplotlib 点图）'}
- **定位后端备注**: {backend_note or '见 SKILL.md Phase 5/6'}

## 统计
- 深度: 中位 {_fmt(depth.median(), '.1f')} km，范围 {_fmt(depth.min(), '.1f')} – {_fmt(depth.max(), '.1f')} km
- 震级 (ML): 范围 {_fmt(mag.min(), '.2f')} – {_fmt(mag.max(), '.2f')}，中位 {_fmt(mag.median(), '.2f')}，均值 {_fmt(mag.mean(), '.2f')}
- 完整性震级 Mc: {_fmt(gr.get('mc'), '.2f', 'N/A') if gr else 'N/A'}（b 值 {_fmt(gr.get('b'), '.2f', 'N/A（样本量不足）') if gr else 'N/A'}）
- 走时残差 RMS: 均值 {_fmt(rms.mean(), '.3f')} s，中位 {_fmt(rms.median(), '.3f')} s，最大 {_fmt(rms.max(), '.3f')} s
  （目录 `rms_s` 反映定位所用拾取的绝对时刻离散水平，量级偏大时以台站数与方位覆盖作为主要质量依据）
- 单事件平均: 台站 {_fmt(avg_sta, '.0f')} 个 / 参与道 {_fmt(avg_tr, '.0f')} 条 / 拾取 {_fmt(picks_n.mean(), '.0f')} 条
- 单台 ML 异常剔除: {_fmt(n_rejected, '.0f')} 条道（偏离中位 0.5 级以上）

## 耗时（timing）

{timing_block}

## 事件目录与震级

{ev_table}

> 台站数 / 参与道数 取自 p07 单台震级诊断（无诊断记录时回退为目录中的 `n_stations_used`；后者统计的是保留道数，可能大于台站数）。
> 质量分级：台站数 ≥ 8 且方位角间隙 ≤ 180° → 好；台站数 ≥ 5 → 一般；台站数 < 5 或方位角间隙 > 270° → 待查；目录缺震级 → 无震级。

## 单台震级质控

{ml_table if ml_table else '（未找到 p07 震级诊断文件，跳过本节）'}

> 单台 ML 范围为逐道结果，未剔除离群；中位/标准差为剔除偏离中位 0.5 级以上的道之后的结果，与目录最终 ML 的口径一致。
> 方位角间隙由台站经纬度计算，仅统计能在台站表中匹配到坐标的台站。

## 数据完整性检查

{check_lines}

## 输入数据

{input_block}

## 图件

{figures}
{pick_note}
"""
    report_path = outdir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 8: QC plots and report")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--picks", help="picks CSV (optional)")
    parser.add_argument("--waveforms", dest="waveforms",
                        default="data/preprocessed",
                        help="preprocessed waveform dir for pick sample plot")
    parser.add_argument("--event", default="main",
                        help="event to plot: 'main' (mainshock, most picks) "
                             "or an event index (default: main)")
    parser.add_argument("--window", type=float, default=None,
                        help="pick sample plot x-window in s after the first "
                             "pick (default: auto-sized from the event)")
    parser.add_argument("--stations", required=True)
    parser.add_argument("--output-dir", dest="output_dir", required=True)
    parser.add_argument("--backend-note", dest="backend_note", default="")
    parser.add_argument("--timings",
                        help="timings.json written by p99_run_pipeline.py; used "
                             "to show the P2-P8 duration breakdown in report.md "
                             "(default: <output-dir>/../pipeline_logs/timings.json)")
    args = parser.parse_args()

    import pandas as pd

    timer = StepTimer()
    catalog = pd.read_csv(args.catalog)
    stations = load_stations(args.stations)
    outdir = ensure_dir(args.output_dir)
    timings = _load_timings(args.timings, outdir)

    log("Plotting epicenter map...")
    with timer.step("P8 epicenter map"):
        map_backend, map_layers = plot_epicenter_map(
            catalog, stations, outdir / "epicenter_map.png")
    log(f"Epicenter map drawn by {map_backend}: "
        f"{', '.join(map_layers) if map_layers else 'no basemap'}")

    with timer.step("P8 G-R relation"):
        log("Computing G-R relation...")
        gr = plot_gr(catalog, outdir / "gr_relation.png")

    with timer.step("P8 depth histogram"):
        log("Plotting depth histogram...")
        plot_depth_hist(catalog, outdir / "depth_histogram.png")

    with timer.step("P8 residuals"):
        log("Plotting residuals...")
        res = plot_residuals(catalog, outdir / "residuals.png")

    pick_info = None
    if args.picks and Path(args.picks).exists():
        with timer.step("P8 pick samples"):
            log("Plotting PhaseNet picking samples (mainshock)...")
            try:
                pick_info = plot_pick_samples(args.picks, args.waveforms,
                                              outdir / "pick_samples.png",
                                              event=args.event,
                                              window_s=args.window)
            except Exception as exc:
                warn(f"pick sample plot failed: {exc}")
    else:
        warn("--picks not given or missing; skipping pick sample plot")

    timer.log_summary("Phase 8 内部步骤耗时")

    with timer.step("P8 report"):
        report_path = _write_report(
            outdir, catalog, stations,
            paths=dict(catalog=args.catalog, stations=args.stations,
                       picks=args.picks, waveforms=args.waveforms),
            map_backend=map_backend, map_layers=map_layers, gr=gr,
            pick_info=pick_info, backend_note=args.backend_note,
            timings=timings, self_elapsed_s=timer.total_s,
            self_timings=timer.to_dict())

    emit({
        "status": "ok",
        "n_events": len(catalog),
        "map_backend": map_backend,
        "map_layers": map_layers,
        "mc": gr["mc"] if gr else None,
        "b_value": gr["b"] if gr else None,
        "mean_rms_s": res["mean_rms_s"] if res else None,
        "report_file": str(report_path),
        "plots": ["epicenter_map.png", "gr_relation.png",
                  "depth_histogram.png", "residuals.png", "pick_samples.png"],
        "pick_samples": pick_info,
        "timings": timer.to_dict(),
    })


if __name__ == "__main__":
    run_main(main)
