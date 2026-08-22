"""Run one compiler variant in a clean Python process."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler-root", type=Path, required=True)
    parser.add_argument("--qasm", type=Path, required=True)
    parser.add_argument("--arch-spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--setting", type=Path, required=True)
    args = parser.parse_args()

    compiler_root = args.compiler_root.resolve()
    qasm = args.qasm.resolve()
    arch_path = args.arch_spec.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("code", "time", "fidelity"):
        (output / name).mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = {"status": "error", "compiler": compiler_root.name, "circuit": qasm.stem}
    try:
        setting_spec = json.loads(args.setting.read_text(encoding="utf-8"))
        if "zac_setting" in setting_spec:
            settings = setting_spec["zac_setting"]
            setting = dict(settings[0] if isinstance(settings, list) else settings)
        else:
            setting = dict(setting_spec)
        setting["name"] = qasm.stem
        setting["dir"] = str(output).replace("\\", "/") + "/"
        import importlib.machinery
        import importlib.util

        package = importlib.util.module_from_spec(importlib.machinery.ModuleSpec("zac", loader=None, is_package=True))
        package.__path__ = [str(compiler_root)]
        sys.modules["zac"] = package
        sys.path.insert(0, str(compiler_root.parent))
        from zac.ds.architecture import Architecture
        from zac.simulator.simulator import Simulator
        from zac.zac import ZAC

        spec = json.loads(arch_path.read_text(encoding="utf-8"))
        architecture = Architecture(spec)
        architecture.preprocessing()
        compiler = ZAC()
        compiler.parse_setting(setting)
        compiler.set_architecture_spec_path(str(arch_path))
        compiler.set_architecture(architecture)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            compiler.set_program(str(qasm))
            compiler.solve(save_file=True)
        code_path = Path(compiler.code_filename)
        simulator = Simulator()
        simulator.set_arch_spec(spec)
        simulator.parse(str(code_path))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fidelity = simulator.simulate()
        result.update({
            "status": "ok",
            "qubits": compiler.n_q,
            "two_qubit_gates": compiler.n_g,
            "move_batches": int(sum(1 for instruction in compiler.result_json["instructions"]
                                   if instruction["type"] == "rearrangeJob")),
            "compile_wall_seconds": time.perf_counter() - started,
            "compiler_runtime": compiler.runtime_analysis,
            "simulation": fidelity,
            "code_path": str(code_path),
            "time_path": str(output / "time" / f"{qasm.stem}_time.json"),
            "fidelity_path": str(output / "fidelity" / f"{qasm.stem}_fidelity.json"),
        })
        (output / "fidelity" / f"{qasm.stem}_fidelity.json").write_text(
            json.dumps(fidelity, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        result.update({
            "compile_wall_seconds": time.perf_counter() - started,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
    (output / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
