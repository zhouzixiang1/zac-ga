"""Synthetic Large RSS probe.

The normal command is intentionally small.  The explicit ``--million`` switch
builds and consumes one million dependent CZ layers without materialising their
QASM, SQLite rows or canonical trace in Python memory::

    python -m streaming.rss_probe --million --workspace /path/to/scratch
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import tracemalloc
from pathlib import Path
from typing import Any, Sequence

from evaluation import CanonicalTraceEvent, EventType, FidelityModel

from .checkpoint import EventStreamWriter
from .large_contract import LARGE_RSS_LIMIT_BYTES
from .qasm_sqlite import LayerStore, build_layer_store
from .trace_pipeline import (
    IncrementalTracePipeline,
    IncrementalTraceScorer,
    IncrementalTraceValidator,
)


def current_rss_bytes() -> int:
    """Best-effort current resident set size without optional dependencies."""

    statm = Path("/proc/self/statm")
    if statm.is_file():
        fields = statm.read_text(encoding="ascii").split()
        if len(fields) >= 2:
            return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    try:
        value = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return int(value) * 1024
    except (OSError, subprocess.CalledProcessError, ValueError):
        return peak_rss_bytes()


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux and the BSDs normally report KiB.
    return value if sys.platform == "darwin" else value * 1024


def write_synthetic_qasm(path: str | Path, layer_count: int,
                         *, qubits: int = 2) -> None:
    if layer_count < 0:
        raise ValueError("layer_count must be non-negative")
    if qubits < 2:
        raise ValueError("synthetic CZ stream requires at least two qubits")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "w", encoding="utf-8", buffering=1 << 20) as handle:
        handle.write('OPENQASM 2.0;\ninclude "qelib1.inc";\n')
        handle.write(f"qreg q[{qubits}];\n")
        for _ in range(layer_count):
            handle.write("cz q[0],q[1];\n")


def _run_probe(
    workspace: Path,
    *,
    layer_count: int,
    qubits: int,
    lookahead_horizon: int,
    sample_interval: int,
    write_trace: bool,
) -> dict[str, Any]:
    if lookahead_horizon < 0 or sample_interval <= 0:
        raise ValueError("horizon must be non-negative and sample_interval positive")
    workspace.mkdir(parents=True, exist_ok=True)
    qasm_path = workspace / "synthetic.qasm"
    sqlite_path = workspace / "synthetic.sqlite"
    trace_path = workspace / "synthetic.events.jsonl.gz"
    baseline_rss = current_rss_bytes()
    observed_peak = baseline_rss
    tracemalloc.start()
    try:
        write_synthetic_qasm(qasm_path, layer_count, qubits=qubits)
        metadata = build_layer_store(
            qasm_path, sqlite_path, commit_interval=max(1, min(10_000, sample_interval))
        )
        observed_peak = max(observed_peak, current_rss_bytes(), peak_rss_bytes())
        writer = EventStreamWriter(trace_path) if write_trace else None
        pipeline = IncrementalTracePipeline(
            IncrementalTraceValidator(
                qubits,
                expected_one_qubit_gates=0,
                expected_two_qubit_gates=layer_count,
            ),
            IncrementalTraceScorer(qubits, FidelityModel()),
            writer,
        )
        pipeline.consume(CanonicalTraceEvent(
            EventType.INIT,
            0.0,
            0.0,
            atoms=tuple(range(qubits)),
            end_positions=tuple((float(q), 0.0) for q in range(qubits)),
            end_regions=("storage",) * qubits,
        ))
        max_window_events = 0
        try:
            with LayerStore(sqlite_path) as store:
                cursor = store.connection.execute(
                    "SELECT seq, layer, two_qubit_layer, operation, q0, q1 "
                    "FROM events ORDER BY seq"
                )
                for row in cursor:
                    seq = int(row["seq"])
                    if row["operation"] != "cz" or row["q1"] is None:
                        raise RuntimeError("synthetic layer store contains a non-CZ event")
                    q0, q1 = int(row["q0"]), int(row["q1"])
                    begin = seq * 0.36
                    pipeline.consume(CanonicalTraceEvent(
                        EventType.TWO_QUBIT_GATE,
                        begin,
                        begin + 0.36,
                        atoms=(q0, q1),
                        gate_pairs=((q0, q1),),
                        region_atoms=(q0, q1),
                        gate_names=("cz",),
                        source_index=seq,
                        metadata={"ghost_hits": 0, "synthetic": True},
                    ))
                    if seq % sample_interval == 0 or seq + 1 == layer_count:
                        observed_peak = max(
                            observed_peak, current_rss_bytes(), peak_rss_bytes()
                        )
                        max_window_events = max(
                            max_window_events,
                            len(store.two_qubit_window(
                                int(row["two_qubit_layer"]),
                                lookahead_horizon,
                            )),
                        )
            result = pipeline.finalize()
        finally:
            if writer is not None:
                writer.close()
        _, python_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    report = {
        "format": "zac-large-rss-probe-v1",
        "layers_requested": layer_count,
        "layers_consumed": result["validation"]["two_qubit_gates"],
        "qubits": qubits,
        "lookahead_horizon": lookahead_horizon,
        "max_window_events": max_window_events,
        "baseline_rss_bytes": baseline_rss,
        "peak_rss_bytes": observed_peak,
        "rss_growth_bytes": max(0, observed_peak - baseline_rss),
        "python_peak_bytes": python_peak,
        "within_22_gib_contract": observed_peak <= LARGE_RSS_LIMIT_BYTES,
        "qasm_bytes": qasm_path.stat().st_size,
        "sqlite_bytes": sqlite_path.stat().st_size,
        "trace_bytes": trace_path.stat().st_size if trace_path.is_file() else 0,
        "layer_store_metadata": dict(metadata),
        "event_hash": writer.event_hash if writer is not None else None,
        "log_fidelity": result["fidelity"]["log_fidelity"],
        "workspace": str(workspace),
    }
    return report


def run_synthetic_rss_probe(
    *,
    layer_count: int = 10_000,
    qubits: int = 2,
    lookahead_horizon: int = 2,
    sample_interval: int = 1_000,
    workspace: str | Path | None = None,
    write_trace: bool = False,
) -> dict[str, Any]:
    """Run the probe, using an ephemeral workspace unless one is supplied."""

    if workspace is not None:
        return _run_probe(
            Path(workspace).resolve(),
            layer_count=layer_count,
            qubits=qubits,
            lookahead_horizon=lookahead_horizon,
            sample_interval=sample_interval,
            write_trace=write_trace,
        )
    with tempfile.TemporaryDirectory(prefix="zac-large-rss-") as directory:
        report = _run_probe(
            Path(directory),
            layer_count=layer_count,
            qubits=qubits,
            lookahead_horizon=lookahead_horizon,
            sample_interval=sample_interval,
            write_trace=write_trace,
        )
        # Do not expose a path that has already been removed.
        report["workspace"] = None
        return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layers", type=int, default=10_000)
    parser.add_argument(
        "--million", action="store_true",
        help="explicitly run the one-million-layer acceptance probe",
    )
    parser.add_argument("--qubits", type=int, default=2)
    parser.add_argument("--horizon", type=int, default=2)
    parser.add_argument("--sample-interval", type=int, default=1_000)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--write-trace", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-rss-growth-mib", type=float)
    args = parser.parse_args(argv)
    layers = 1_000_000 if args.million else args.layers
    report = run_synthetic_rss_probe(
        layer_count=layers,
        qubits=args.qubits,
        lookahead_horizon=args.horizon,
        sample_interval=args.sample_interval,
        workspace=args.workspace,
        write_trace=args.write_trace,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.max_rss_growth_mib is not None:
        limit = args.max_rss_growth_mib * (1 << 20)
        if report["rss_growth_bytes"] > limit:
            raise SystemExit(
                f"RSS growth {report['rss_growth_bytes']} exceeds {int(limit)} bytes"
            )
    if not report["within_22_gib_contract"]:
        raise SystemExit("probe exceeded the frozen 22 GiB RSS contract")


if __name__ == "__main__":
    main()


__all__ = [
    "current_rss_bytes",
    "peak_rss_bytes",
    "run_synthetic_rss_probe",
    "write_synthetic_qasm",
]
