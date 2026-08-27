#!/usr/bin/env python3
"""Phase 7: local magnitude (ML) via Wood-Anderson simulation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, log, run_main, warn  # noqa: E402

WAVEFORM_EXTS = {".mseed", ".miniseed", ".seed", ".sac", ".msd"}

# Wood-Anderson 地震仪极点/零点/增益（经典定义）
WA_POLES = [-6.283 + 4.7124j, -6.283 - 4.7124j]
WA_ZEROS = [0j]
WA_GAIN = 2800.0          # 经典静态放大倍率
WA_SENSITIVITY = 1.0

# Hutton & Boore (1987) 南加州标定系数
# 标准形式（Richter 原始定义，A 单位 mm，D 为震源距 km）：
#   ML = log10(A) + 1.11*log10(D) + 0.00189*D - 2.09
# 本脚本振幅使用微米 (μm)，需将常数 -2.09 换算为 -5.09
# 如果你的研究区域不在南加州，请替换为区域标定系数
HB_C1 = 1.11
HB_C2 = 0.00189
HB_C3 = -5.09


def _hutton_boore_ml(amplitude_um: float, hypocentral_dist_km: float) -> float:
    """
    计算单次台站的 ML。
    amplitude_um: WA 模拟后的最大位移振幅，单位微米 (μm)
    hypocentral_dist_km: 震源距 (km)
    """
    a = max(amplitude_um, 1e-12)
    r = max(hypocentral_dist_km, 1.0)
    return (np.log10(a)
            + HB_C1 * np.log10(r)
            + HB_C2 * r
            + HB_C3)


def _check_and_convert_to_displacement_um(tr, assume_units: str = "VEL") -> float | None:
    """
    检查 trace 的物理单位，并返回最大位移振幅（微米）。

    注意：MSEED 格式不保存 units 元数据，p02 去响应后写入的
    tr.stats.units 在读回时会丢失。因此这里优先读 stats.units，
    缺失时回退到 --assume-units（默认 "VEL"，与 p02 的 VEL 输出一致）。
    """
    units = getattr(tr.stats, "units", None)
    units_upper = str(units).upper().strip() if units else str(assume_units).upper().strip()

    if units_upper in ("M", "METERS", "DISP"):
        # 位移，单位米 → 微米
        return float(np.max(np.abs(tr.data))) * 1e6

    if units_upper in ("M/S", "M/SEC", "VELOCITY"):
        # 速度，单位 m/s → 对时间积分得到位移(m) → 再转微米
        try:
            tr_disp = tr.copy()
            tr_disp.detrend("demean")
            tr_disp.detrend("linear")
            tr_disp.taper(max_percentage=0.05, type="hann")
            tr_disp.integrate()
            return float(np.max(np.abs(tr_disp.data))) * 1e6
        except Exception as exc:
            warn(f"trace {tr.id} 速度→位移积分失败 ({exc})，跳过")
            return None

    if units_upper in ("COUNTS", "COUNT"):
        warn(f"trace {tr.id} 单位仍为 COUNTS，去响应可能失败，跳过")
        return None

    warn(f"trace {tr.id} 单位 '{units_upper}' 未识别，跳过")
    return None


def _is_horizontal_channel(channel: str) -> bool:
    """判断通道是否为水平分量（E/N/1/2）"""
    if not channel or len(channel) < 3:
        return False
    return channel[-1] in ("E", "N", "1", "2")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 7: compute ML magnitudes (Wood-Anderson)")
    parser.add_argument("--catalog", required=True, help="located catalog CSV")
    parser.add_argument("--waveforms", required=True)
    parser.add_argument("--stations", help="StationXML with response")
    parser.add_argument("--output", required=True)
    parser.add_argument("--assume-units", default="DISP",
                        choices=["VEL", "DISP", "COUNTS"],
                        help="assumed physical units when trace metadata "
                             "lacks units (MSEED drops it); this phase "
                             "removes response to DISP")
    args = parser.parse_args()

    import pandas as pd
    from obspy import Stream, read, read_inventory

    catalog = pd.read_csv(args.catalog)
    files = [f for f in sorted(Path(args.waveforms).rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files under {args.waveforms}")
        return

    # ── 加载台站响应 ──
    inventory = None
    if args.stations and Path(args.stations).exists():
        try:
            inventory = read_inventory(args.stations)
        except Exception as exc:
            warn(f"cannot read inventory ({exc}); response removal skipped")

    if inventory is None:
        emit({
            "status": "warning",
            "magnitude_available": False,
            "error": "no StationXML/response available — ML cannot be computed",
            "catalog_file": args.catalog,
            "hint": "re-run Phase 2 with --remove-response and a valid "
                    "StationXML, then re-run this script",
        })
        return

    # ── 加载台站位置信息 ──
    stas = None
    try:
        from common import load_stations
        csv_path = args.stations.replace(".xml", ".csv")
        if Path(csv_path).exists():
            stas = load_stations(csv_path)
            log(f"Loaded station list from {csv_path} ({len(stas)} stations)")
        else:
            warn(f"station CSV not found at {csv_path}; "
                 "distances will fall back to 10 km — results unreliable")
    except Exception as exc:
        warn(f"failed to load station CSV ({exc})")

    # ── 加载波形并去响应 ──
    log("Loading waveforms...")
    stream = Stream()
    for f in files:
        try:
            stream += read(str(f))
        except Exception as exc:
            warn(f"unreadable file skipped: {f} ({exc})")

    if len(stream) == 0:
        fail("no valid waveforms loaded")
        return

    # 上游 p02 已经执行过去响应（output=VEL），MSEED 写回会丢失 units。
    # 为避免对已经物理化的数据再次反卷积导致振幅异常，默认跳过重复去响应，
    # 仅当用户显式要求 --assume-units=COUNTS 时才执行。
    needs_response_removal = (args.assume_units.upper() == "COUNTS")
    if needs_response_removal:
        log("Removing instrument response...")
        try:
            stream.remove_response(inventory=inventory, output="DISP",
                                   water_level=60)
        except Exception as exc:
            fail(f"response removal failed ({exc}); "
                 "cannot compute reliable magnitudes without physical units")
            return
    else:
        log("Assuming waveforms are already in physical units "
            "(p02 remove_response); skipping repeated response removal")

    # 标记单位，因为 MSEED 写回会丢失 units 元数据
    for tr in stream:
        tr.stats.units = "M"

    # ── 模拟 Wood-Anderson 地震仪 ──
    log("Simulating Wood-Anderson response...")
    stream.simulate(paz_remove=None,
                    paz_simulate={
                        "poles": WA_POLES,
                        "zeros": WA_ZEROS,
                        "gain": WA_GAIN,
                        "sensitivity": WA_SENSITIVITY,
                    })
    # simulate 后仍是位移量，保留单位标记
    for tr in stream:
        tr.stats.units = "M"

    # ── 按事件计算震级 ──
    from obspy import UTCDateTime

    diag = []
    mags = []
    for _, ev in catalog.iterrows():
        ev_time = pd.Timestamp(ev["time"])
        origin = UTCDateTime(ev_time.to_pydatetime())

        ml_values = []

        for tr in stream:
            # 只取水平分量
            if not _is_horizontal_channel(tr.stats.channel):
                continue

            # 基于走时的时间窗口（简化：使用经验走时估算）
            # 对于本地地震，S 波走时 ≈ 距离 / 3.5 km/s
            # 这里先用一个较宽松的窗口，后续可替换为精确走时表
            r_epi_km = 10.0
            if stas is not None:
                match = stas[
                    stas["station"].astype(str).str.split(".").str[-1]
                    == tr.stats.station
                ]
                if len(match):
                    from common import haversine_km
                    r_epi_km = float(haversine_km(
                        ev["latitude"], ev["longitude"],
                        match.latitude.iloc[0],
                        match.longitude.iloc[0],
                    ))

            depth_km = float(ev.get("depth_km", 0.0) or 0.0)
            r_hyp_km = float(np.sqrt(r_epi_km ** 2 + depth_km ** 2))

            # 时间窗口：从 P 波到时前 5 秒开始，持续 max(30, 距离/3) 秒
            p_travel = max(r_epi_km / 6.0, 1.0)   # P 波粗略走时
            s_travel = max(r_epi_km / 3.5, 2.0)   # S 波粗略走时
            win_start = origin + p_travel - 5
            win_duration = max(30.0, s_travel - p_travel + 20.0)
            win_end = win_start + win_duration

            windowed = tr.slice(win_start, win_end)
            if len(windowed.data) == 0:
                continue

            # 检查单位并获取振幅（微米）
            amp_um = _check_and_convert_to_displacement_um(
                windowed, assume_units=args.assume_units)
            if amp_um is None:
                continue

            # 使用震源距（Hutton-Boore 公式要求）
            hypocentral_km = r_hyp_km

            ml = _hutton_boore_ml(amp_um, hypocentral_km)
            ml_values.append(ml)
            diag.append({
                "event_id": ev["event_id"],
                "trace_id": tr.id,
                "epicentral_km": round(r_epi_km, 2),
                "hypocentral_km": round(hypocentral_km, 2),
                "amplitude_um": round(amp_um, 4),
                "single_ml": round(ml, 2),
            })

        if not ml_values:
            mags.append({
                "event_id": ev["event_id"],
                "magnitude": np.nan,
                "magnitude_type": "ML",
                "n_stations_used": 0,
            })
            continue

        # 剔除偏离中位数超过 0.5 级的异常值后取中位数
        ml_arr = np.array(ml_values)
        med = np.median(ml_arr)
        mask = np.abs(ml_arr - med) <= 0.5
        ml_filtered = ml_arr[mask]

        if len(ml_filtered) == 0:
            ml_filtered = ml_arr  # 全部异常时回退

        ml_final = float(np.median(ml_filtered))
        mags.append({
            "event_id": ev["event_id"],
            "magnitude": round(ml_final, 2),
            "magnitude_type": "ML",
            "n_stations_used": len(ml_filtered),
        })

    # ── 合并结果并输出 ──
    mag_df = pd.DataFrame(mags)
    out = catalog.merge(
        mag_df,
        on="event_id", how="left", suffixes=("", "_new"),
    )
    if "magnitude_new" in out.columns:
        out["magnitude"] = out["magnitude_new"].combine_first(out["magnitude"])
        out.drop(columns=[c for c in out.columns if c.endswith("_new")],
                 inplace=True)

    out_path = Path(args.output)
    ensure_dir(out_path.parent)
    out.to_csv(out_path, index=False)

    # 输出诊断表，便于排查震级异常
    if diag:
        diag_path = out_path.with_suffix(".magnitude_diag.csv")
        pd.DataFrame(diag).to_csv(diag_path, index=False)
        log(f"Magnitude diagnostics written to {diag_path}")

    n_with_mag = int((out["magnitude"].notna()
                      & (out["magnitude"].astype(str) != "")).sum())
    emit({
        "status": "ok",
        "magnitude_available": True,
        "n_events": len(catalog),
        "n_events_with_magnitude": n_with_mag,
        "median_magnitude": float(
            out["magnitude"].dropna().astype(float).median()
        ) if n_with_mag else None,
        "catalog_file": str(out_path),
        "diagnostics_file": str(diag_path) if diag else None,
    })


if __name__ == "__main__":
    run_main(main)