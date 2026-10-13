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

# Wood-Anderson 地震仪极点/零点/灵敏度。
# 取值与 obspy.signal.invsim.WOODANDERSON 完全一致：simulate() 中 gain 是与频率
# 无关的乘性因子、sensitivity 再单独相乘，而 WA 的静态放大倍率已经编码在极零点里。
# 早期版本把 2800 当作 "静态放大倍率" 放进 gain、并把 sensitivity 设为 1，实测
# 振幅偏大 1.346 倍（≈ +0.13 级）；二者在 obspy 中是可交换的乘性因子，容易写错。
#
# 输入域：WA 的 paz 只有一个原点零点，因此 simulate() 的输入必须是地面速度，
# 这与 obspy 在 estimate_magnitude 内部「先除以 VEL 响应、再乘 WA 极零点」一致。
WA_POLES = [-6.283 + 4.7124j, -6.283 - 4.7124j]
WA_ZEROS = [0j]
WA_GAIN = 1.0
WA_SENSITIVITY = 2080.0

# 标定式：Bakun & Joyner (1984)，与 obspy.signal.invsim.estimate_magnitude 同源，
# 也是公开目录（tests/tmp_data，Templeton CA）中 ML 所用的尺度（见 tests/verify_ml.py）。
#   ML = log10(A) + log10(D/100) + 0.00301*(D - 100) + 3.0
# 其中 A 为 WA 零到峰位移振幅 (mm)，D 为震源距 (km)。
# 如果你的研究区域超出该标定范围，请整体替换 _ml_from_wa_amplitude()，
# 保持 A(mm) / D(km) 的入参约定即可。
ML_ATTEN_COEF = 0.00301
ML_CONST = 3.0


def _ml_from_wa_amplitude(amplitude_mm: float, hypocentral_dist_km: float) -> float:
    """由单台 WA 零到峰位移振幅 (mm) 与震源距 (km) 计算 ML。

    Bakun & Joyner (1984), BSSA 74, 1827-1843.
    """
    a = max(amplitude_mm, 1e-12)
    d = max(hypocentral_dist_km, 1.0)
    return (np.log10(a)
            + np.log10(d / 100.0)
            + ML_ATTEN_COEF * (d - 100.0)
            + ML_CONST)


def _to_velocity(tr, assume_units: str = "VEL") -> bool:
    """把单条 trace 换算到 WA paz 要求的输入域——地面速度 (m/s)。

    MSEED 不保存 units 元数据：p02 写入的 tr.stats.units 读回时会丢失，
    所以这里只认调用方显式给出的 --assume-units，绝不反过来改写 stats.units
    （否则该参数形同虚设）。
    """
    units = str(assume_units).upper().strip()
    if units in ("M/S", "M/SEC", "VEL", "VELOCITY"):
        return True
    if units in ("M", "METERS", "DISP", "DISPLACEMENT"):
        # 位移输入：WA 的 paz 只有一个原点零点，必须先微分回速度
        tr.detrend("demean")
        tr.detrend("linear")
        tr.taper(max_percentage=0.05, type="hann")
        tr.differentiate()
        return True
    warn(f"trace {tr.id} 单位 '{units}' 不受支持，跳过")
    return False


def _wa_amplitude_mm(tr) -> float:
    """窗口内 WA 零到峰位移振幅 (mm)。

    取整段峰峰值的一半，与 obspy estimate_magnitude 的振幅约定一致，且对残余
    偏置不敏感。obspy 约定下 simulate(WOODANDERSON) 输出的数值乘以 1000 即为
    estimate_magnitude 使用的毫米振幅。
    """
    if tr.data.size == 0:
        return 0.0
    return float(tr.data.max() - tr.data.min()) / 2.0 * 1000.0


def _is_horizontal_channel(channel: str) -> bool:
    """判断通道是否为水平分量（E/N/1/2）"""
    if not channel or len(channel) < 3:
        return False
    return channel[-1] in ("E", "N", "1", "2")


def _station_code(row) -> str:
    """从 stations.csv 的一行提取纯台站代码。

    stations.csv 的 station 列形如 "AK.CCB."（带尾点），直接
    split(".")[-1] 会得到空字符串；必须先去掉尾点。
    优先使用显式的 station_code 列。
    """
    if "station_code" in row.index and str(row["station_code"]).strip():
        return str(row["station_code"]).strip()
    return str(row["station"]).strip().rstrip(".").split(".")[-1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 7: compute ML magnitudes (Wood-Anderson)")
    parser.add_argument("--catalog", required=True, help="located catalog CSV")
    parser.add_argument("--waveforms", required=True,
                        help="waveform dir (p02 output, i.e. velocity m/s)")
    parser.add_argument("--stations", help="StationXML with instrument responses")
    parser.add_argument("--output", required=True)
    parser.add_argument("--assume-units", default="VEL",
                        choices=["VEL", "DISP", "COUNTS"],
                        help="waveform units of --waveforms; MSEED drops the "
                             "units metadata so it must be stated explicitly. "
                             "VEL = ground velocity m/s (p02 output='VEL', "
                             "default); DISP = displacement m; "
                             "COUNTS = raw counts, response removed here")
    args = parser.parse_args()

    import pandas as pd
    from obspy import Stream, read, read_inventory

    catalog = pd.read_csv(args.catalog)
    files = [f for f in sorted(Path(args.waveforms).rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files under {args.waveforms}")
        return

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

    # Station coordinates, expected next to the StationXML (p01 writes both).
    stas = None
    csv_path = args.stations.replace(".xml", ".csv")
    if Path(csv_path).exists():
        from common import load_stations
        stas = load_stations(csv_path)
        log(f"Loaded station list from {csv_path} ({len(stas)} stations)")
    else:
        warn(f"station CSV not found at {csv_path}: epicentral distances are "
             "unavailable, so no magnitude can be computed")

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

    # ── 统一到 WA paz 的输入域（地面速度）──
    # 上游 p02 以 output="VEL" 去响应，所以 --assume-units 默认 VEL。
    # 只有明确声明输入仍是原始 COUNTS 时才在这里去响应；注意必须是 VEL，
    # 不能是 DISP（WA 的 paz 需要速度输入）。
    if args.assume_units.upper() == "COUNTS":
        log("Removing instrument response (output=VEL)...")
        try:
            stream.remove_response(inventory=inventory, output="VEL",
                                   water_level=60)
        except Exception as exc:
            fail(f"response removal failed ({exc}); "
                 "cannot compute reliable magnitudes without physical units")
            return
        for tr in stream:
            tr.stats.units = "M/S"
    else:
        log(f"Assuming waveforms are in '{args.assume_units}' "
            "(p02 remove_response); skipping repeated response removal")
        keep = Stream()
        for tr in stream:
            if _to_velocity(tr, args.assume_units):
                tr.stats.units = "M/S"
                keep.append(tr)
        n_dropped = len(stream) - len(keep)
        if n_dropped:
            warn(f"{n_dropped}/{len(stream)} traces dropped "
                 f"(unsupported units '{args.assume_units}')")
        stream = keep
        if len(stream) == 0:
            fail(f"no usable traces for units '{args.assume_units}'")
            return

    # ── 模拟 Wood-Anderson 地震仪 ──
    log("Simulating Wood-Anderson response...")
    stream.simulate(paz_remove=None,
                    paz_simulate={
                        "poles": WA_POLES,
                        "zeros": WA_ZEROS,
                        "gain": WA_GAIN,
                        "sensitivity": WA_SENSITIVITY,
                    })

    # ── 按事件计算震级 ──
    from common import haversine_km
    from obspy import UTCDateTime

    diag = []
    mags = []
    log(f"Total traces loaded: {len(stream)}")

    sta_codes = None
    if stas is not None:
        sta_codes = stas.apply(_station_code, axis=1).astype(str)

    n_matched = 0
    n_unmatched = 0
    for _, ev in catalog.iterrows():
        ev_time = pd.Timestamp(ev["time"])
        origin = UTCDateTime(ev_time.to_pydatetime())

        log(f"Event {ev.get('event_id', 'N/A')} @ "
            f"({ev['latitude']:.4f}, {ev['longitude']:.4f}), "
            f"depth={ev.get('depth_km', 0.0):.1f} km")

        depth_km = float(ev.get("depth_km", 0.0) or 0.0)
        ml_values = []

        for tr in stream:
            # Only horizontal components carry the S amplitude used here.
            if not _is_horizontal_channel(tr.stats.channel):
                continue
            if sta_codes is None:
                continue

            match_mask = sta_codes == tr.stats.station
            if not match_mask.any():
                # Without coordinates there is no epicentral distance, hence no
                # usable amplitude-to-magnitude conversion: skip, never guess.
                n_unmatched += 1
                continue
            n_matched += 1
            match_idx = stas.index[match_mask][0]
            r_epi_km = float(haversine_km(
                ev["latitude"], ev["longitude"],
                float(stas.loc[match_idx, "latitude"]),
                float(stas.loc[match_idx, "longitude"])))
            r_hyp_km = float(np.sqrt(r_epi_km ** 2 + depth_km ** 2))

            # Rough travel-time window: P onset minus 5 s, long enough to cover
            # the S phase and its coda at these distances.
            p_travel = max(r_epi_km / 6.0, 1.0)
            s_travel = max(r_epi_km / 3.5, 2.0)
            win_start = origin + p_travel - 5
            win_end = win_start + max(30.0, s_travel - p_travel + 20.0)

            windowed = tr.slice(win_start, win_end)
            if len(windowed.data) == 0:
                continue

            amp_mm = _wa_amplitude_mm(windowed)
            if amp_mm <= 0.0:
                continue

            ml = _ml_from_wa_amplitude(amp_mm, r_hyp_km)
            ml_values.append(ml)
            diag.append({
                "event_id": ev["event_id"],
                "trace_id": tr.id,
                "epicentral_km": round(r_epi_km, 2),
                "hypocentral_km": round(r_hyp_km, 2),
                "amplitude_mm": round(amp_mm, 8),
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

        # Median after dropping single-station values that deviate by >0.5
        ml_arr = np.array(ml_values)
        ml_filtered = ml_arr[np.abs(ml_arr - np.median(ml_arr)) <= 0.5]
        if len(ml_filtered) == 0:
            ml_filtered = ml_arr

        ml_final = float(np.median(ml_filtered))
        mags.append({
            "event_id": ev["event_id"],
            "magnitude": round(ml_final, 2),
            "magnitude_type": "ML",
            "n_stations_used": len(ml_filtered),
        })
        log(f"ML={ml_final:.2f} (from {len(ml_filtered)} traces, "
            f"raw range=[{ml_arr.min():.2f}, {ml_arr.max():.2f}])")

    log(f"Magnitude calculation: {n_matched} trace-event pairs used, "
        f"{n_unmatched} skipped (station absent from the station table)")

    # This phase is the authoritative source of magnitude: any magnitude the
    # upstream catalog carries (e.g. the published values fetch_templeton.py
    # writes) is replaced, not kept alongside.
    mag_df = pd.DataFrame(mags)
    out = catalog.drop(
        columns=[c for c in ("magnitude", "magnitude_type")
                 if c in catalog.columns])
    out = out.merge(mag_df, on="event_id", how="left")

    out_path = Path(args.output)
    ensure_dir(out_path.parent)
    out.to_csv(out_path, index=False)

    # Per-trace amplitudes for diagnosing odd magnitudes
    diag_path = None
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
        "ml_relation": "Bakun & Joyner (1984)",
        "input_units": args.assume_units,
        "response_removed_here": args.assume_units.upper() == "COUNTS",
        "catalog_file": str(out_path),
        "diagnostics_file": str(diag_path) if diag_path else None,
    })


if __name__ == "__main__":
    run_main(main)