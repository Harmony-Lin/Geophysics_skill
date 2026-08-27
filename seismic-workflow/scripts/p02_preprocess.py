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
    parser.add_argument("--detrend", action="store_true", default=True)
    parser.add_argument("--demean", action="store_true", default=True)
    parser.add_argument("--filter", default="0.5-20", help="bandpass in Hz: low-high")
    parser.add_argument("--resample", type=float, default=100.0, help="target Hz")
    args = parser.parse_args()

    src = Path(args.input)
    files = [f for f in sorted(src.rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files under {src}")

    out = ensure_dir(args.output)
    f_lo, f_hi = parse_filter(args.filter)

    from obspy import Stream, read
    inventory = None
    if args.remove_response:
        if not args.stations or not Path(args.stations).exists():
            warn("--remove-response requested but no StationXML; "
                 "response removal will be SKIPPED (ML magnitude unavailable)")
        else:
            from obspy import read_inventory
            inventory = read_inventory(args.stations)

    n_in = 0
    n_ok = 0
    failed = []
    for f in files:
        try:
            st = read(str(f))
        except Exception as exc:
            warn(f"unreadable file skipped: {f} ({exc})")
            failed.append(f.name)
            continue
        n_in += len(st)
        for tr in st:
            try:
                tr.detrend("linear") #去除线性趋势
                tr.detrend("demean") #去除均值
                if inventory is not None and args.remove_response: #去仪器响应
                    tr.remove_response(inventory=inventory, output="VEL", water_level=60)
                elif args.remove_response:
                    tr.remove_response(output="VEL", water_level=60)
                tr.filter("bandpass", freqmin=f_lo, freqmax=f_hi, corners=4, #带通滤波
                          zerophase=True)
                # resample via decimation/interpolation to target rate
                if abs(tr.stats.sampling_rate - args.resample) > 0.01:
                    tr.resample(args.resample)
                n_ok += 1
            except Exception as exc:
                warn(f"trace failed: {tr.id} ({exc})")
                failed.append(f"{tr.id}")
                st.remove(tr)
        if len(st) > 0:
            sub_out = out / f.name.replace(Path(f.name).suffix, ".mseed")
            st.write(str(sub_out), format="MSEED")

    log(f"Processed {n_ok}/{n_in} traces -> {out}")
    frac_failed = (len(failed) / n_in) if n_in else 1.0
    status = "ok"
    if frac_failed > 0.10:
        status = "error"
        fail(f"{len(failed)}/{n_in} traces failed (>10%)",
             n_traces_input=n_in, n_traces_processed=n_ok,
             n_traces_failed=len(failed), failed=failed[:50])
        return

    emit({
        "status": status,
        "n_traces_input": n_in,
        "n_traces_processed": n_ok,
        "n_traces_failed": len(failed),
        "failed": failed[:50],
        "filter_hz": [f_lo, f_hi],
        "resample_hz": args.resample,
        "response_removed": inventory is not None,
        "output_dir": str(out),
    })


if __name__ == "__main__":
    run_main(main)
