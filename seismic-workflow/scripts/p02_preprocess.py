#!/usr/bin/env python3
"""Phase 2: preprocessing — detrend, demean, response removal,
bandpass filter, resample."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, log, run_main, warn  # noqa: E402

WAVEFORM_EXTS = {".mseed", ".miniseed", ".seed", ".sac", ".msd"}


def parse_filter(spec: str):
    try:
        lo, hi = spec.split("-")
        return float(lo), float(hi)
    except ValueError:
        fail(f"--filter must be '<low>-<high>' Hz, got: {spec}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2: preprocess waveforms")
    parser.add_argument("--input", required=True, help="raw waveform dir")
    parser.add_argument("--output", required=True, help="preprocessed output dir")
    parser.add_argument("--stations", help="StationXML for response removal")
    parser.add_argument("--remove-response", dest="remove_response",
                        action="store_true", help="remove instrument response")
    parser.add_argument("--detrend", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="remove linear trend (default: on)")
    parser.add_argument("--demean", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="remove mean (default: on)")
    parser.add_argument("--filter", default="0.5-20", help="bandpass in Hz: low-high")
    parser.add_argument("--resample", type=float, default=100.0, help="target Hz")
    args = parser.parse_args()

    src = Path(args.input)
    files = [f for f in sorted(src.rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files under {src}")

    from obspy import Stream, read
    inventory = None
    # 参数校验放在创建输出目录之前：失败时不应留下空目录，避免下游误以为
    # 该阶段已经跑过。
    if args.remove_response:
        if not args.stations or not Path(args.stations).exists():
            # 静默跳过去响应会让 p07 拿到未物理化的数据并算出错误震级，
            # 所以这里直接失败，让 p99 暴露问题而不是产出看似正常的错结果。
            fail(f"--remove-response requires an existing StationXML, got: "
                 f"{args.stations!r}")
            return
        from obspy import read_inventory
        inventory = read_inventory(args.stations)
        log(f"Loaded station inventory from {args.stations}")

    f_lo, f_hi = parse_filter(args.filter)
    out = ensure_dir(args.output)

    n_in = 0
    n_ok = 0
    n_resp_removed = 0
    failed = []
    for f in files:
        try:
            st = read(str(f))
        except Exception as exc:
            warn(f"unreadable file skipped: {f} ({exc})")
            failed.append(f.name)
            continue
        n_in += len(st)
        drop = []
        for tr in st:
            try:
                if args.detrend:
                    tr.detrend("linear")  # 去除线性趋势
                if args.demean:
                    tr.detrend("demean")  # 去除均值
                if inventory is not None and args.remove_response:  # 去仪器响应
                    tr.remove_response(inventory=inventory, output="VEL", water_level=60)
                    # obspy 不会自动设置 units；手动标记，供下游阶段参考。
                    # 注意 MSEED 不保存该属性，p07 必须显式声明 --assume-units。
                    tr.stats.units = "M/S"
                    n_resp_removed += 1
                tr.filter("bandpass", freqmin=f_lo, freqmax=f_hi, corners=4, #带通滤波
                          zerophase=True)
                # resample via decimation/interpolation to target rate
                if abs(tr.stats.sampling_rate - args.resample) > 0.01:
                    tr.resample(args.resample)
                n_ok += 1
            except Exception as exc:
                warn(f"trace failed: {tr.id} ({exc})")
                failed.append(f"{tr.id}")
                drop.append(tr)
        # 循环中删除元素会让 Stream 迭代跳过后续 trace，必须延后统一删除
        for tr in drop:
            st.remove(tr)
        if len(st) > 0:
            sub_out = out / f.name.replace(Path(f.name).suffix, ".mseed")
            st.write(str(sub_out), format="MSEED")

    log(f"Processed {n_ok}/{n_in} traces -> {out}; "
        f"response removed on {n_resp_removed} traces")
    frac_failed = (len(failed) / n_in) if n_in else 1.0
    status = "ok"
    if frac_failed > 0.10:
        status = "error"
        fail(f"{len(failed)}/{n_in} traces failed (>10%)",
             n_traces_input=n_in, n_traces_processed=n_ok,
             n_traces_failed=len(failed), failed=failed[:50])
        return

    # response_removed 反映"实际成功去响应的 trace 比例"，而不是"inventory 是否加载成功"
    response_removed = bool(n_resp_removed and n_resp_removed == n_ok)
    if args.remove_response and n_resp_removed == 0:
        fail("--remove-response requested but no trace was deconvolved",
             n_traces_input=n_in, n_traces_processed=n_ok,
             n_traces_failed=len(failed), failed=failed[:50])
        return

    if not args.remove_response:
        output_units = "COUNTS"
    elif response_removed:
        output_units = "M/S"
    else:
        # 部分 trace 去响应失败 -> 输出目录里混了两种单位，必须显式暴露，
        # 否则 p07 会把 counts 当成 m/s 继续算（震级必然错）。
        output_units = "MIXED"

    emit({
        "status": status,
        "n_traces_input": n_in,
        "n_traces_processed": n_ok,
        "n_traces_failed": len(failed),
        "failed": failed[:50],
        "filter_hz": [f_lo, f_hi],
        "resample_hz": args.resample,
        "remove_response_requested": args.remove_response,
        "n_traces_response_removed": n_resp_removed,
        "response_removed": response_removed,
        "output_units": output_units,
        "detrend": args.detrend,
        "demean": args.demean,
        "output_dir": str(out),
    })


if __name__ == "__main__":
    run_main(main)
