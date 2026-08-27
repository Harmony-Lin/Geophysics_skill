#!/usr/bin/env python3
"""Phase 8: QC and plotting — epicenter map, G-R relation, depth histogram,
residuals, and a Markdown report."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, load_stations, log, run_main, warn  # noqa: E402


def _annotate_events(ax, catalog, max_labels=30):
    """Annotate event time + magnitude next to each epicenter (subset if many)."""
    import pandas as pd
    n = len(catalog)
    step = max(1, int(np.ceil(n / max_labels)))
    for i, row in enumerate(catalog.itertuples()):
        if i % step:
            continue
        t = getattr(row, "time", None)
        t_str = ""
        if t is not None and not pd.isna(t):
            try:
                t_str = str(pd.Timestamp(t).strftime("%m-%d %H:%M"))
            except Exception:
                t_str = ""
        mag = pd.to_numeric(pd.Series([getattr(row, "magnitude", None)]),
                            errors="coerce").iloc[0]
        label = " ".join(x for x in (
            f"M{mag:.1f}" if pd.notna(mag) else "", t_str) if x)
        if label:
            ax.annotate(label, (row.longitude, row.latitude),
                        textcoords="offset points", xytext=(6, 6),
                        fontsize=6, color="darkred")


def _add_vector_features(ax, lon_min, lon_max, lat_min, lat_max):
    """叠加 Natural Earth 矢量要素（海岸线、国界、州界、河流、湖泊、
    道路、城镇），按区域跨度自动选择分辨率（10m/50m/110m）。

    每个要素独立 try/except：下载失败或要素缺失时跳过，不影响成图。
    """
    import cartopy.feature as cfeature
    span = max(lon_max - lon_min, lat_max - lat_min)
    res = "10m" if span <= 1.0 else ("50m" if span <= 8.0 else "110m")

    def _add(category, name, **kw):
        try:
            ax.add_feature(cfeature.NaturalEarthFeature(
                category, name, res, **kw))
        except Exception as exc:
            warn(f"vector feature {name} ({res}) skipped: {exc}")

    # 底色（110m 常量即可，仅作背景）
    ax.add_feature(cfeature.LAND, facecolor="lightgray", zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor="white", zorder=0)
    # 河流与湖泊
    _add("physical", "rivers_lake_centerlines",
         edgecolor="steelblue", facecolor="none", linewidth=0.6, zorder=1)
    _add("physical", "lakes", facecolor="lightblue",
         edgecolor="steelblue", linewidth=0.4, zorder=1)
    # 海岸线
    _add("physical", "coastline", edgecolor="black",
         facecolor="none", linewidth=0.6, zorder=2)
    # 国界与州/省界
    _add("cultural", "admin_0_boundary_lines_land",
         edgecolor="gray", facecolor="none", linewidth=0.6, zorder=2)
    _add("cultural", "admin_1_states_provinces_lines",
         edgecolor="gray", facecolor="none", linewidth=0.4, zorder=2)
    # 道路（仅 10m 分辨率提供）
    if res == "10m":
        _add("cultural", "roads", edgecolor="darkgray",
             facecolor="none", linewidth=0.3, zorder=1)
    # 城镇（点要素，FeatureArtist 不支持 marker 参数，用默认样式）
    _add("cultural", "populated_places", edgecolor="none",
         facecolor="none", zorder=2)


def plot_epicenter_map(catalog, stations, out_png: Path) -> str:
    """Try PyGMT first; fall back to Cartopy/Matplotlib. Returns backend name."""
    try:
        import pygmt
        region = [
            min(catalog["longitude"].min(), stations["longitude"].min()) - 0.3,
            max(catalog["longitude"].max(), stations["longitude"].max()) + 0.3,
            min(catalog["latitude"].min(), stations["latitude"].min()) - 0.3,
            max(catalog["latitude"].max(), stations["latitude"].max()) + 0.3]
        fig = pygmt.Figure()
        fig.basemap(region=region, projection="M12c",
                    frame=["af", 'WSne+t"Epicenter distribution"'])
        fig.coast(land="lightgray", water="white", borders=1, roads="gray",
                  rivers=1, lakes=True)
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
        return "pygmt"
    except Exception as exc:
        warn(f"PyGMT unavailable ({exc}); falling back to Matplotlib/Cartopy")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    def _plain_map():
        fig, ax = plt.subplots(figsize=(8, 7))
        return fig, ax, "matplotlib"

    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        lon_min = float(min(catalog["longitude"].min(),
                            stations["longitude"].min())) - 0.3
        lon_max = float(max(catalog["longitude"].max(),
                            stations["longitude"].max())) + 0.3
        lat_min = float(min(catalog["latitude"].min(),
                            stations["latitude"].min())) - 0.3
        lat_max = float(max(catalog["latitude"].max(),
                            stations["latitude"].max())) + 0.3
        proj = ccrs.PlateCarree()
        fig, ax = plt.subplots(figsize=(9, 8),
                               subplot_kw={"projection": proj})
        ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=proj)
        _add_vector_features(ax, lon_min, lon_max, lat_min, lat_max)
        # 注意：不用 cartopy 的 gridlines —— Cartopy 0.25 的 gridliner 在
        # PlateCarree + 特定 extent 下会触发 "LinearRing do not form a
        # closed linestring" 的 GEOS 异常，改用普通经纬度刻度。
        import numpy as _np
        lon_step = 0.5 if (lon_max - lon_min) <= 4 else 1.0
        lat_step = 0.5 if (lat_max - lat_min) <= 4 else 1.0
        ax.set_xticks(_np.arange(_np.floor(lon_min), _np.ceil(lon_max) + 1,
                                 lon_step))
        ax.set_yticks(_np.arange(_np.floor(lat_min), _np.ceil(lat_max) + 1,
                                 lat_step))
        ax.set_xticklabels([f"{x:.1f}°" for x in ax.get_xticks()])
        ax.set_yticklabels([f"{y:.1f}°" for y in ax.get_yticks()])
        backend = "cartopy"
    except Exception as exc:
        warn(f"Cartopy unavailable ({exc}); plain Matplotlib map")
        fig, ax, backend = _plain_map()

    sc = ax.scatter(catalog["longitude"], catalog["latitude"],
                    c=pd.to_numeric(catalog.get("magnitude"),
                                    errors="coerce").fillna(0.0),
                    cmap="YlOrRd", edgecolors="black", linewidths=0.3,
                    s=12 + 8 * pd.to_numeric(
                        catalog.get("magnitude"), errors="coerce").fillna(0.0),
                    label="earthquake", zorder=3)
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
        fig.colorbar(sc, ax=ax, shrink=0.7, label="Magnitude")
    except Exception:
        pass
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Epicenter distribution")
    ax.legend(loc="upper left")
    fig.tight_layout()
    try:
        fig.savefig(out_png, dpi=200)
    except Exception as exc:
        # Cartopy can fail at draw time (e.g. shapely gridliner issues);
        # retry once with a plain Matplotlib axes.
        warn(f"Cartopy render failed ({exc}); retrying with plain Matplotlib")
        plt.close(fig)
        fig, ax, backend = _plain_map()
        sc = ax.scatter(catalog["longitude"], catalog["latitude"],
                        c=pd.to_numeric(catalog.get("magnitude"),
                                        errors="coerce").fillna(0.0),
                        cmap="YlOrRd", edgecolors="black", linewidths=0.3,
                        s=12 + 8 * pd.to_numeric(
                            catalog.get("magnitude"),
                            errors="coerce").fillna(0.0),
                        label="earthquake", zorder=3)
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
            fig.colorbar(sc, ax=ax, shrink=0.7, label="Magnitude")
        except Exception:
            pass
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title("Epicenter distribution")
        ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return backend


def _pygmt_event_text(fig, catalog, max_labels=30):
    """Annotate event time+magnitude on a PyGMT figure (subset if many)."""
    import pandas as pd
    n = len(catalog)
    step = max(1, int(np.ceil(n / max_labels)))
    for i, row in enumerate(catalog.itertuples()):
        if i % step:
            continue
        t = getattr(row, "time", None)
        t_str = ""
        if t is not None and not pd.isna(t):
            try:
                t_str = str(pd.Timestamp(t).strftime("%m-%d %H:%M"))
            except Exception:
                t_str = ""
        mag = pd.to_numeric(pd.Series([getattr(row, "magnitude", None)]),
                            errors="coerce").iloc[0]
        label = " ".join(x for x in (
            f"M{mag:.1f}" if pd.notna(mag) else "", t_str) if x)
        if label:
            fig.text(x=row.longitude, y=row.latitude, text=label,
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
    rms = pd.to_numeric(catalog.get("rms_s"), errors="coerce").dropna()
    if len(rms) == 0:
        warn("no rms_s column — skipping residual plot")
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


def plot_pick_samples(picks_csv, waveform_dir, out_png: Path,
                      n_events: int = 4, window_s: float = 60.0) -> dict | None:
    """Phase 3 sample figure: waveforms with PhaseNet picks overlaid.

    Selects a few events (by pick-time clustering), plots the vertical
    traces of the stations that picked them, and marks P (red) / S (blue)
    pick times. Only a representative sample is drawn, not all events.
    """
    import pandas as pd
    picks = pd.read_csv(picks_csv)
    if len(picks) == 0:
        warn("no picks — skipping pick sample plot")
        return None

    from obspy import read, UTCDateTime

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
    # prefer the largest-magnitude-looking events: most picks per event
    sel = sorted(order, key=lambda e: -len(picks[picks.event == e]))[:n_events]
    sel = [order.index(e) for e in sorted(sel)]

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

    n_rows = sum(len(sel) and 1 for _ in sel)
    fig, axes = plt.subplots(n_rows, 1, figsize=(11, 2.6 * n_rows),
                             squeeze=False)

    info = {"events_plotted": 0, "picks_plotted": 0}
    for r, ev_idx in enumerate(sel):
        ax = axes[r][0]
        ev = picks[picks.event == order[ev_idx]]
        t0 = ev["time"].min()
        plotted = False
        for row in ev.itertuples():
            trace_id = str(row.station)
            cands = [trs for tid, lst in streams.items()
                     if tid.split(".")[1] == trace_id.split(".")[1]
                     for trs in lst]
            if not cands:
                continue
            tr = cands[0]
            delta = (pd.Timestamp(tr.stats.starttime.datetime).tz_localize("UTC")
                     - t0).total_seconds()
            tt = np.arange(tr.stats.npts) / tr.stats.sampling_rate + delta
            m = (tt >= -5) & (tt <= window_s)
            if not m.any():
                continue
            ax.plot(tt[m], tr.data[m], lw=0.7, color="black")
            plotted = True
            break  # one representative trace per event keeps the figure clean
        for prow in ev.itertuples():
            dt = (prow.time - t0).total_seconds()
            if dt < -5 or dt > window_s:
                continue
            color = "red" if str(prow.phase).upper().startswith("P") else "blue"
            ax.axvline(dt, color=color, lw=1.0, alpha=0.8)
            info["picks_plotted"] += 1
        ax.set_xlim(-5, window_s)
        ax.set_ylabel("counts")
        mag_txt = ""
        ax.set_title(
            f"event {ev_idx + 1}: origin≈{t0.strftime('%Y-%m-%d %H:%M:%S')} UTC"
            f"  ({len(ev)} picks){mag_txt}", fontsize=9, loc="left")
        if plotted:
            info["events_plotted"] += 1

    from matplotlib.lines import Line2D
    handles = [Line2D([], [], color="red", lw=1.2, label="P pick"),
               Line2D([], [], color="blue", lw=1.2, label="S pick")]
    axes[-1][0].set_xlabel("Time since first pick (s)")
    fig.legend(handles=handles, loc="upper right", ncol=2)
    fig.suptitle("PhaseNet picking samples (Phase 3)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 8: QC plots and report")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--picks", help="picks CSV (optional)")
    parser.add_argument("--waveforms", dest="waveforms",
                        default="data/preprocessed",
                        help="preprocessed waveform dir for pick sample plot")
    parser.add_argument("--stations", required=True)
    parser.add_argument("--output-dir", dest="output_dir", required=True)
    parser.add_argument("--backend-note", dest="backend_note", default="")
    args = parser.parse_args()

    import pandas as pd

    catalog = pd.read_csv(args.catalog)
    stations = load_stations(args.stations)
    outdir = ensure_dir(args.output_dir)

    log("Plotting epicenter map...")
    map_backend = plot_epicenter_map(catalog, stations, outdir / "epicenter_map.png")

    log("Computing G-R relation...")
    gr = plot_gr(catalog, outdir / "gr_relation.png")

    log("Plotting depth histogram...")
    plot_depth_hist(catalog, outdir / "depth_histogram.png")

    log("Plotting residuals...")
    res = plot_residuals(catalog, outdir / "residuals.png")

    pick_info = None
    if args.picks and Path(args.picks).exists():
        log("Plotting PhaseNet picking samples...")
        try:
            pick_info = plot_pick_samples(args.picks, args.waveforms,
                                          outdir / "pick_samples.png")
        except Exception as exc:
            warn(f"pick sample plot failed: {exc}")
    else:
        warn("--picks not given or missing; skipping pick sample plot")

    report_path = outdir / "report.md"
    mag_series = pd.to_numeric(catalog.get("magnitude"), errors="coerce").dropna()
    report = f"""# 地震目录处理报告

## 概要
- **事件总数**: {len(catalog)}
- **台站数**: {len(stations)}
- **有震级的事件数**: {len(mag_series)} ({100 * len(mag_series) / max(len(catalog), 1):.0f}%)
- **制图后端**: {map_backend}
- **定位后端备注**: {args.backend_note or '见 SKILL.md Phase 5/6'}

## 统计
- 中位深度: {catalog['depth_km'].median():.1f} km
- 震级范围: {mag_series.min() if len(mag_series) else 'N/A'} – {mag_series.max() if len(mag_series) else 'N/A'}
- 完整性震级 Mc: {gr['mc'] if gr else 'N/A'}
- b 值: {gr['b'] if gr else 'N/A'}
- 平均走时残差: {res['mean_rms_s'] if res else 'N/A'} s

## 图件
![震中分布](epicenter_map.png)
![G-R关系](gr_relation.png)
![深度分布](depth_histogram.png)
![走时残差](residuals.png)
![拾取样例](pick_samples.png)

## 注意事项
- 若未做仪器响应去除，ML 震级不可用（见 Phase 2/7 说明）。
- 纯 Python 定位后端深度误差典型为 2–5 km；如需亚公里级精度请使用 HypoDD。
"""
    report_path.write_text(report, encoding="utf-8")

    emit({
        "status": "ok",
        "n_events": len(catalog),
        "map_backend": map_backend,
        "mc": gr["mc"] if gr else None,
        "b_value": gr["b"] if gr else None,
        "mean_rms_s": res["mean_rms_s"] if res else None,
        "report_file": str(report_path),
        "plots": ["epicenter_map.png", "gr_relation.png",
                  "depth_histogram.png", "residuals.png", "pick_samples.png"],
        "pick_samples": pick_info,
    })


if __name__ == "__main__":
    run_main(main)
