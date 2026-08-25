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


def plot_epicenter_map(catalog, stations, out_png: Path) -> str:
    """Try PyGMT first; fall back to Cartopy/Matplotlib. Returns backend name."""
    try:
        import pygmt
        fig = pygmt.Figure()
        region = [
            catalog["longitude"].min() - 0.5, catalog["longitude"].max() + 0.5,
            catalog["latitude"].min() - 0.5, catalog["latitude"].max() + 0.5]
        fig.basemap(region=region, projection="M12c", frame=True)
        fig.coast(land="lightgray", water="white", borders=1)
        fig.plot(x=catalog["longitude"], y=catalog["latitude"],
                 style="c0.15c", color="red", label="earthquake")
        fig.plot(x=stations["longitude"], y=stations["latitude"],
                 style="t0.3c", color="blue", label="station")
        fig.legend()
        fig.savefig(str(out_png), dpi=200)
        return "pygmt"
    except Exception as exc:
        warn(f"PyGMT unavailable ({exc}); falling back to Matplotlib/Cartopy")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 7))
    ax.scatter(catalog["longitude"], catalog["latitude"], c="red", s=12,
               label="earthquake")
    ax.scatter(stations["longitude"], stations["latitude"], marker="^",
               c="blue", s=40, label="station")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Epicenter distribution")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return "matplotlib"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 8: QC plots and report")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--picks", help="picks CSV (optional)")
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
                  "depth_histogram.png", "residuals.png"],
    })


if __name__ == "__main__":
    run_main(main)
