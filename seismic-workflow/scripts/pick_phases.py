#!/usr/bin/env python3
"""Phase 3: deep-learning phase picking via SeisBench (PhaseNet default)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import emit, ensure_dir, fail, log, run_main, warn  # noqa: E402

WAVEFORM_EXTS = {".mseed", ".miniseed", ".seed", ".sac", ".msd"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3: pick P/S phases")
    parser.add_argument("--input", required=True, help="preprocessed waveform dir")
    parser.add_argument("--model", default="phasenet",
                        choices=["phasenet", "eqtransformer"])
    parser.add_argument("--output", required=True, help="picks CSV output path")
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=32)
    parser.add_argument("--device", default=None,
                        help="'cpu' or 'cuda' (auto if omitted)")
    parser.add_argument("--threshold-p", dest="threshold_p", type=float, default=0.5)
    parser.add_argument("--threshold-s", dest="threshold_s", type=float, default=0.5)
    args = parser.parse_args()

    src = Path(args.input)
    files = [f for f in sorted(src.rglob("*"))
             if f.suffix.lower() in WAVEFORM_EXTS]
    if not files:
        fail(f"no waveform files under {src}")

    try:
        from seisbench.models import EQTransformer, PhaseNet
        import torch
    except ImportError as exc:
        fail(f"seisbench/torch unavailable: {exc}",
             hint="install per docs/install.md; weights cache at ~/.cache/seisbench")

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    log(f"Loading {args.model} model (device={device})...")
    try:
        if args.model == "phasenet":
            model = PhaseNet.from_pretrained("original")
        else:
            model = EQTransformer.from_pretrained("original")
    except Exception as exc:
        fail(f"model weight load/download failed: {exc}",
             hint="offline: copy ~/.cache/seisbench from an online machine "
                  "(see docs/install.md)")
        return

    model.to(device)
    model.eval()

    from obspy import read

    picks = []
    n_traces = 0
    for i, f in enumerate(files, 1):
        try:
            stream = read(str(f))
        except Exception as exc:
            warn(f"skipped unreadable file: {f} ({exc})")
            continue
        n_traces += len(stream)
        try:
            # classify() accepts the obspy Stream directly; thresholds are
            # passed as "<phase>_threshold" kwargs (e.g. P_threshold).
            annotations = model.classify(
                stream,
                batch_size=args.batch_size,
                P_threshold=args.threshold_p,
                S_threshold=args.threshold_s)
        except Exception as exc:
            warn(f"classify failed for {f.name}: {exc}")
            continue
        for pick in annotations.picks:
            picks.append({
                "station": pick.trace_id,
                "phase": pick.phase.upper(),
                "time": str(pick.peak_time),
                "probability": float(pick.peak_value),
                "amplitude": "",
            })
        if i % 10 == 0:
            log(f"[{i}/{len(files)}] files processed, {len(picks)} picks so far...")

    out_path = Path(args.output)
    ensure_dir(out_path.parent)
    import pandas as pd
    df = pd.DataFrame(picks)
    df.to_csv(out_path, index=False)

    elapsed_note = "CPU mode: expect 5-10x slower than GPU" if device == "cpu" else ""
    if elapsed_note:
        warn(elapsed_note)

    emit({
        "status": "ok",
        "model": args.model,
        "device": device,
        "n_files": len(files),
        "n_traces": n_traces,
        "n_picks": len(picks),
        "n_p_picks": sum(1 for p in picks if p["phase"] == "P"),
        "n_s_picks": sum(1 for p in picks if p["phase"] == "S"),
        "picks_file": str(out_path),
    })


if __name__ == "__main__":
    run_main(main)
