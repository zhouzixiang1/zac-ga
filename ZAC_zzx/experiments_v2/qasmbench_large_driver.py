"""Private exact Large M3/M4 driver used by the QASMBench orchestrator.

M3/M4 consume the disk-backed exact Schema-2 placement stream.  This
module deliberately does not import ``streaming.large_compiler``: that module
is a development proxy and is not eligible for the four-method experiment.
M1/M2 remain on :mod:`experiments_v2.method_driver`, preserving their original
paper implementations without a ghost repair/split wrapper.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import traceback
from typing import Any, Mapping

from streaming.formal_large_zac_compiler import compile_formal_large_zac
from streaming.qasm_sqlite import build_layer_store

from .plan import effective_zac_setting


def _load_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return dict(value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run_exact_large_zac(
    *, method: str, input_path: Path, config_path: Path,
    architecture_path: Path, output_root: Path,
) -> Mapping[str, Any]:
    method = method.upper()
    if method not in {"M3", "M4"}:
        raise ValueError("exact Large QASMBench driver accepts only M3/M4")
    output_root.mkdir(parents=True, exist_ok=True)
    layer_store = output_root / "layers.sqlite"
    layer_metadata = build_layer_store(input_path, layer_store)
    setting = effective_zac_setting(_load_object(config_path))
    if setting.get("backend") != "native":
        raise ValueError(f"formal {method} requires backend=native")
    formal_output = output_root / "formal"
    result = compile_formal_large_zac(
        method=method,
        layer_store_path=layer_store,
        architecture_path=architecture_path,
        output_directory=formal_output,
        setting=setting,
        progress_every=1000,
        resume=False,
    )
    payload: dict[str, Any] = {
        "status": "success",
        "implementation_status": "formal_exact_zac_sqlite_v1",
        "layer_store": dict(layer_metadata),
        "result": result.to_dict(),
    }
    from zzx.native_backend import build_info

    payload["native_identity"] = {
        "algorithm_revision": setting["algorithm_revision"],
        "backend": "native",
        "native_abi_version": int(setting["native_abi_version"]),
        "native_wheel_sha256": str(setting["native_wheel_sha256"]),
        "tuning_protocol_id": str(setting["tuning_protocol_id"]),
        "rng_version": str(setting["rng_version"]),
        "compiler_and_flags": build_info(
            require_registered_wheel=True,
            expected_wheel_sha256=str(setting["native_wheel_sha256"]),
        ),
        "python_fallback": False,
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", required=True, choices=("M3", "M4"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--architecture", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    result_path = args.output_root / "driver_result.json"
    try:
        payload = run_exact_large_zac(
            method=args.method,
            input_path=args.input.resolve(),
            config_path=args.config.resolve(),
            architecture_path=args.architecture.resolve(),
            output_root=args.output_root.resolve(),
        )
    except BaseException as error:
        args.output_root.mkdir(parents=True, exist_ok=True)
        _write_json(result_path, {
            "status": "compiler_error",
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
        })
        return 1
    _write_json(result_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_exact_large_zac"]
