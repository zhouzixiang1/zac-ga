"""Command-line entry point for normalization and physical scoring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .adapters import normalize_na, normalize_zair
from .model import FidelityModel
from .scorer import score_trace


def _json_file(path: str | None):
    if path is None:
        return None
    return json.loads(Path(path).read_text())


def _source(path: str) -> str | Path:
    return sys.stdin.read() if path == "-" else Path(path)


def _write_json(value, output: str | None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output:
        Path(output).write_text(text)
    else:
        sys.stdout.write(text)


def _write_events(events, output: str | None) -> None:
    text = "".join(json.dumps(event.to_dict(), sort_keys=True) + "\n" for event in events)
    if output:
        Path(output).write_text(text)
    else:
        sys.stdout.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation",
        description="Normalize ZAIR/NA schedules and score them with the frozen ZAC physical model.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("normalize", "score"):
        command = subparsers.add_parser(name)
        command.add_argument("input", help="native schedule path, or '-' for stdin")
        command.add_argument("--format", choices=("zair", "na"), required=True)
        command.add_argument("--architecture", help="ZAC/QMAP architecture JSON")
        command.add_argument("--model", help="FidelityModel JSON override")
        command.add_argument("--gate-pairs", help="optional per-CZ pair ledger JSON for NA")
        command.add_argument("--output", help="output path; stdout when omitted")
        if name == "score":
            command.add_argument("--events-output", help="also archive normalized events as JSONL")
            command.add_argument("--n-qubits", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model_value = _json_file(args.model)
    model = FidelityModel.from_mapping(model_value) if model_value is not None else FidelityModel()
    architecture = _json_file(args.architecture)
    gate_pairs = _json_file(args.gate_pairs)
    native_source = _source(args.input)

    if args.format == "zair":
        if gate_pairs is not None:
            raise SystemExit("--gate-pairs is only valid for --format na")
        events = list(normalize_zair(native_source, architecture=architecture, model=model))
    else:
        events = list(
            normalize_na(
                native_source,
                architecture=architecture,
                model=model,
                gate_pairs=gate_pairs,
            )
        )

    if args.command == "normalize":
        _write_events(events, args.output)
    else:
        if args.events_output:
            _write_events(events, args.events_output)
        result = score_trace(events, model, n_qubits=args.n_qubits)
        _write_json(result.to_dict(), args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
