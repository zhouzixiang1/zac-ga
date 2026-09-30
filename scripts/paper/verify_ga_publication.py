#!/usr/bin/env python3
"""Verify the frozen GA publication after relocating its exact baseline loader.

Only one registered historical source path is remapped. The original exporter,
protocol, result files, hashes, and logical provenance remain unchanged. This
wrapper is read-only and never compiles a circuit or writes publication files.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from paper_paths import PROJECT_ROOT

BASELINE_LOGICAL = Path("IEEE_conference_template/writing/generate_default_initial_values.py")
BASELINE_PHYSICAL = Path("scripts/paper/frozen/generate_default_initial_values.py")
BASELINE_SHA256 = "749ea90bdfd28c598fd94338a0d2dd5c29374b99b6ee6838031221b79b5478b9"
EXPORTER_RELATIVE = Path("ZAC_zzx/experiments_v2/export_physical_ga_main.py")
PROTOCOL_RELATIVE = Path("ZAC_zzx/results/physical_ga_main_v1/protocol.json")


def frozen_bytes(project_root=PROJECT_ROOT):
    path = Path(project_root).resolve() / BASELINE_PHYSICAL
    if path.is_symlink() or not path.is_file():
        raise ValueError("registered frozen baseline loader is missing or symlinked: " + str(path))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != BASELINE_SHA256:
        raise ValueError("registered frozen baseline loader SHA256 drift: " + str(path))
    return data


def load_exporter(project_root=PROJECT_ROOT):
    path = Path(project_root).resolve() / EXPORTER_RELATIVE
    spec = importlib.util.spec_from_file_location("_relocated_ga_publication_exporter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def relocated_loader(exporter, project_root=PROJECT_ROOT):
    """Keep original source validation while substituting one physical location."""
    project_root = Path(project_root).resolve()
    logical = project_root / BASELINE_LOGICAL
    physical = project_root / BASELINE_PHYSICAL
    data = frozen_bytes(project_root)
    original_sources, original_module_at = exporter.Sources, exporter.module_at

    class RelocatedSources(original_sources):
        def file(self, reference):
            ref = {"path": str(reference)} if isinstance(reference, (str, Path)) else reference
            path = Path(ref["path"])
            path = (self.repo / path).resolve() if not path.is_absolute() else path.resolve()
            if path != logical:
                return super().file(reference)
            if ref.get("sha256", BASELINE_SHA256) != BASELINE_SHA256:
                raise ValueError("unregistered baseline loader SHA256: " + str(path))
            # The parent binds size, digest, and mutation checks to actual bytes.
            physical_ref = super().file({**ref, "path": str(physical), "sha256": BASELINE_SHA256})
            return {**physical_ref, "path": str(logical)}

        def finish(self):
            records = super().finish()
            records = [{**ref, "path": str(logical)} if ref["path"] == str(physical)
                       else ref for ref in records]
            return sorted(records, key=lambda ref: ref["path"])

    def module_at(path, name):
        candidate = Path(path)
        candidate = (project_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if candidate != logical:
            return original_module_at(path, name)
        # Verify again at import time. A logical __file__ preserves the frozen
        # loader's default repository root without modifying its source bytes.
        source = frozen_bytes(project_root)
        spec = importlib.util.spec_from_file_location(name, logical)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        exec(compile(source, str(logical), "exec"), module.__dict__)
        return module

    exporter.Sources, exporter.module_at = RelocatedSources, module_at
    try:
        yield {"logical_path": str(logical), "physical_path": str(physical),
               "sha256": BASELINE_SHA256, "bytes": len(data)}
    finally:
        exporter.Sources, exporter.module_at = original_sources, original_module_at


def audit(protocol=None, output_root=None):
    exporter = load_exporter()
    protocol = Path(protocol or PROJECT_ROOT / PROTOCOL_RELATIVE).resolve()
    destination = Path(output_root or protocol.parent / "paper_exports").resolve()
    with relocated_loader(exporter) as mapping:
        outputs, _ = exporter.compute_exports(protocol)
        report = exporter.write_or_check(outputs, destination, protocol_path=protocol, check=True)
    return {**report, "verification": "original_exporter_check_with_registered_source_relocation",
            "source_relocations": [mapping]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--check", action="store_true", help="Accepted for compatibility; verification is always read-only")
    args = parser.parse_args(argv)
    try:
        report = audit(args.protocol, args.output_root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {"status": "fail", "read_only": True, "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
