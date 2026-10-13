#!/usr/bin/env python3
"""Phase 2: preprocessing — detrend, demean, response removal,
bandpass filter, resample."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (CumulativeTimer, emit, ensure_dir, fail, log,  # noqa: E402
                    run_main, warn)

WAVEFORM_EXTS = {".mseed", ".miniseed", ".seed", ".sac", ".msd"}

#: How many instrument responses to keep cached at once.  One entry holds a
#: complex spectrum of ``nfft // 2 + 1`` points (~262 KB at nfft = 32768), so the
#: default caps the cache at roughly 270 MB even for a large array.
RESPONSE_CACHE_MAX = 1024


class ResponseCache:
    """Reuse one evalresp evaluation per channel across all input files.

    obspy's evalresp is a pure-Python implementation: evaluating a multi-stage
    response on the ~16k frequencies of a 21000-sample trace costs ~70 ms, and
    ``Trace.remove_response`` does that once per *trace*.  The same stations come
    back in every event file (this dataset: 5002 traces but only 461 distinct
    channels), so recomputing is ~10x wasted work.

    Keyed on the response object identity plus everything the evaluation depends
    on, and stores the response object to keep that identity (and the ``id()``
    key) valid for as long as the entry lives.
    """

    def __init__(self, max_entries: int = RESPONSE_CACHE_MAX) -> None:
        self._by_key: dict[tuple, tuple] = {}
        self._max_entries = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, response, delta: float, nfft: int, output: str,
            water_level: float | None, invert):
        """Return the *inverted* frequency response for ``response``.

        ``invert`` is called once per distinct key to fill the cache; it must be
        a pure function of the key (it is ``Trace.remove_response``'s water-level
        inversion).
        """
        key = (id(response), delta, nfft, output, water_level)
        hit = self._by_key.get(key)
        if hit is None:
            if len(self._by_key) >= self._max_entries:
                # FIFO: the working set is one pass over the array, so dropping
                # the oldest entry is enough to bound memory.
                self._by_key.pop(next(iter(self._by_key)))
            hit = (response, invert())
            self._by_key[key] = hit
            self.misses += 1
        else:
            self.hits += 1
        return hit[1]

    def __len__(self) -> int:
        return len(self._by_key)


def build_cached_remover(inventory, cache: ResponseCache, output: str = "VEL",
                         water_level: float = 60.0, zero_mean: bool = True,
                         taper: bool = True, taper_fraction: float = 0.05):
    """Return ``remove(tr)``: ``Trace.remove_response`` with a cached response.

    Mirrors the evalresp branch of ``obspy.core.trace.Trace.remove_response``
    step for step — mean removal, cosine taper, ``rfft(nfft)``, multiply by the
    inverted response, DC/Nyquist fix-up, ``irfft`` back to ``npts`` — but the
    response itself is evaluated at most once per channel.  Verified to match
    obspy sample for sample.  The only intended difference is that obspy's
    ``_add_processing_info`` decorator also appends a ``stats.processing`` entry,
    which MSEED does not persist and nothing downstream reads.
    """
    import numpy as np
    from obspy.core.inventory.response import PolynomialResponseStage
    from obspy.signal.invsim import cosine_taper, invert_spectrum
    from obspy.signal.util import _npts2nfft

    def invert_response(response, delta, nfft):
        freq_response, _freqs = response.get_evalresp_response(
            delta, nfft, output=output)
        if water_level is None:
            # No water level: invert directly (first entry is 0 Hz → keep 0).
            freq_response[0] = 0.0
            freq_response[1:] = 1.0 / freq_response[1:]
        else:
            invert_spectrum(freq_response, water_level)
        return freq_response

    def remove(tr) -> None:
        response = inventory.get_response(tr.id, tr.stats.starttime)
        if (not response.response_stages
                or isinstance(response.response_stages[0],
                              PolynomialResponseStage)):
            # Polynomial/linear responses take a different obspy branch: let
            # obspy handle those (rare, and not cacheable this way).
            tr.remove_response(inventory=inventory, output=output,
                               water_level=water_level)
            return
        npts = len(tr.data)
        nfft = _npts2nfft(npts)
        inv_resp = cache.get(response, tr.stats.delta, nfft, output,
                             water_level,
                             lambda: invert_response(response, tr.stats.delta,
                                                     nfft))
        data = tr.data.astype(np.float64)
        if zero_mean:
            data -= data.mean()
        if taper:
            data *= cosine_taper(npts, taper_fraction, sactaper=True,
                                 halfcosine=False)
        spec = np.fft.rfft(data, n=nfft)
        spec *= inv_resp
        spec[-1] = abs(spec[-1]) + 0.0j
        tr.data = np.fft.irfft(spec)[0:npts]

    return remove


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

    from obspy import read
    inventory = None
    # Validated before the output directory is created: a failed run must not
    # leave an empty directory behind that downstream phases read as "done".
    if args.remove_response:
        if not args.stations or not Path(args.stations).exists():
            # Skipping the deconvolution silently would hand p07 counts while it
            # assumes m/s, i.e. a wrong magnitude that still looks plausible.
            fail(f"--remove-response requires an existing StationXML, got: "
                 f"{args.stations!r}")
            return
        from obspy import read_inventory
        inventory = read_inventory(args.stations)
        log(f"Loaded station inventory from {args.stations}")

    f_lo, f_hi = parse_filter(args.filter)
    out = ensure_dir(args.output)

    timer = CumulativeTimer()
    cache: ResponseCache | None = None
    remove_response = None
    if inventory is not None and args.remove_response:
        cache = ResponseCache()
        try:
            remove_response = build_cached_remover(inventory, cache)
        except ImportError as exc:  # pragma: no cover - obspy internals moved
            # Never silently change the result: fall back to obspy's own (slow)
            # implementation if its private helpers are unavailable.
            warn(f"cached response removal unavailable ({exc}); "
                 f"falling back to Trace.remove_response")

    n_in = 0
    n_ok = 0
    n_resp_removed = 0
    failed = []
    for f in files:
        with timer.step("read mseed"):
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
                    with timer.step("detrend linear"):
                        tr.detrend("linear")
                if args.demean:
                    with timer.step("demean"):
                        tr.detrend("demean")
                if inventory is not None and args.remove_response:
                    with timer.step("remove response"):
                        if remove_response is not None:
                            remove_response(tr)
                        else:
                            tr.remove_response(inventory=inventory,
                                               output="VEL", water_level=60)
                    # obspy does not set units itself; MSEED does not persist it,
                    # so p07 still has to be told --assume-units VEL explicitly.
                    tr.stats.units = "M/S"
                with timer.step("bandpass filter"):
                    tr.filter("bandpass", freqmin=f_lo, freqmax=f_hi, corners=4,
                              zerophase=True)
                # resample via decimation/interpolation to target rate
                if abs(tr.stats.sampling_rate - args.resample) > 0.01:
                    with timer.step("resample"):
                        tr.resample(args.resample)
                n_ok += 1
                if inventory is not None and args.remove_response:
                    n_resp_removed += 1
            except Exception as exc:
                warn(f"trace failed: {tr.id} ({exc})")
                failed.append(f"{tr.id}")
                drop.append(tr)
        # Removing while iterating makes Stream skip the following trace.
        for tr in drop:
            st.remove(tr)
        if len(st) > 0:
            sub_out = out / f.name.replace(Path(f.name).suffix, ".mseed")
            with timer.step("write mseed"):
                st.write(str(sub_out), format="MSEED")

    log(f"Processed {n_ok}/{n_in} traces -> {out}; "
        f"response removed on {n_resp_removed} traces")
    if cache is not None:
        # Only `misses` evalresp evaluations actually cost time; the rest is reuse
        log(f"instrument response cache: {cache.misses} evaluated, "
            f"{cache.hits} reused ({len(cache)} distinct channels)")
    timer.log_summary("Phase 2 内部步骤耗时")
    timing_payload = {"timings": timer.to_dict()}
    if cache is not None:
        timing_payload["response_cache"] = {
            "distinct_channels": len(cache),
            "responses_evaluated": cache.misses,
            "responses_reused": cache.hits,
        }
    frac_failed = (len(failed) / n_in) if n_in else 1.0
    if frac_failed > 0.10:
        fail(f"{len(failed)}/{n_in} traces failed (>10%)",
             n_traces_input=n_in, n_traces_processed=n_ok,
             n_traces_failed=len(failed), failed=failed[:50],
             **timing_payload)
        return

    response_removed = bool(args.remove_response)
    # A trace whose response cannot be removed fails the loop body and is
    # dropped, so every trace reaching the output directory carries the unit
    # below; traces lost that way are reported through n_traces_failed.
    output_units = "M/S" if args.remove_response else "COUNTS"

    emit({
        "status": "ok",
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
        **timing_payload,
    })


if __name__ == "__main__":
    run_main(main)
