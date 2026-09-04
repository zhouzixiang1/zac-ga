"""Explicit, hash-sealed entry point for the final two-sheet result inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import ZipFile

from .contracts import (RunStatus, load_run_manifest,
                        repository_snapshot, sha256_file)
from .final_results import (
    FINAL_RESULTS_CONTRACT_ID,
    SHEET_NAMES,
    aggregate_final_results,
    write_final_results,
)
from .plan import METHODS, effective_zac_setting, load_experiment_plan
from .protocol import FORMAL_QUALITY_SEEDS, FORMAL_TIMING_REPETITIONS


INDEX_SCHEMA = 1
AGGREGATION_PROVENANCE_SCHEMA = 1
FINAL_RENDERER = Path(__file__).with_name("render_final_workbook.mjs")


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _record_hash(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items()
                if key != "record_sha256"}
    return hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write one evidence record once, accepting only byte-equivalent reuse."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FileExistsError(
                f"refusing to replace unreadable immutable record: {path}") from error
        if existing != value:
            raise FileExistsError(f"refusing to replace immutable record: {path}")
        return
    _atomic_json(path, value)


def seal_manifest_index(
        paths: Sequence[str | os.PathLike[str]], *, run_kind: str,
        output_path: str | os.PathLike[str]) -> Mapping[str, Any]:
    if run_kind not in {"main", "timing"}:
        raise ValueError("final index run_kind must be main or timing")
    rows = []
    identities = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path.is_dir() or not path.is_file():
            raise ValueError(
                f"final index accepts explicit manifest files only: {path}")
        manifest = load_run_manifest(path, require_success_metrics=True)
        if manifest.run_kind != run_kind:
            raise ValueError(
                f"{path} has run_kind={manifest.run_kind}, expected {run_kind}")
        identity = (
            manifest.dataset, manifest.circuit, manifest.method,
            manifest.seed, manifest.repetition)
        if identity in identities:
            raise ValueError(f"duplicate final index identity: {identity}")
        identities.add(identity)
        rows.append({
            "path": str(path),
            "sha256": sha256_file(path),
            "run_id": manifest.run_id,
            "identity": list(identity),
        })
    if not rows:
        raise ValueError("cannot seal an empty final manifest index")
    rows.sort(key=lambda row: tuple(str(item) for item in row["identity"]))
    payload = {
        "index_schema": INDEX_SCHEMA,
        "run_kind": run_kind,
        "manifests": rows,
    }
    payload["record_sha256"] = _record_hash(payload)
    destination = Path(output_path).expanduser().resolve()
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if existing != payload:
            raise FileExistsError(f"refusing to replace final index: {destination}")
    else:
        _atomic_json(destination, payload)
    return payload


def _load_manifest_index_payload(
        path: str | os.PathLike[str], *, expected_run_kind: str
        ) -> tuple[Mapping[str, Any], list[Path]]:
    source = Path(path).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if (payload.get("index_schema") != INDEX_SCHEMA
            or payload.get("run_kind") != expected_run_kind
            or payload.get("record_sha256") != _record_hash(payload)):
        raise ValueError(f"final manifest index contract drift: {source}")
    rows = payload.get("manifests")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"final manifest index is empty: {source}")
    result, identities = [], set()
    for row in rows:
        manifest_path = Path(str(row.get("path", ""))).resolve()
        if (not manifest_path.is_file()
                or row.get("sha256") != sha256_file(manifest_path)):
            raise ValueError(
                f"indexed run manifest changed or disappeared: {manifest_path}")
        manifest = load_run_manifest(
            manifest_path, require_success_metrics=True)
        identity = (
            manifest.dataset, manifest.circuit, manifest.method,
            manifest.seed, manifest.repetition)
        if (row.get("run_id") != manifest.run_id
                or row.get("identity") != list(identity)
                or identity in identities):
            raise ValueError(
                f"indexed run identity drift: {manifest_path}")
        identities.add(identity)
        result.append(manifest_path)
    return payload, result


def load_manifest_index(
        path: str | os.PathLike[str], *, expected_run_kind: str
        ) -> list[Path]:
    _, manifests = _load_manifest_index_payload(
        path, expected_run_kind=expected_run_kind)
    return manifests


def _index_identity(path: str | os.PathLike[str], *, run_kind: str
                    ) -> tuple[Mapping[str, Any], list[Path]]:
    source = Path(path).expanduser().resolve()
    payload, manifests = _load_manifest_index_payload(
        source, expected_run_kind=run_kind)
    return {
        "path": str(source),
        "sha256": sha256_file(source),
        "record_sha256": payload["record_sha256"],
        "run_kind": run_kind,
        "manifest_count": len(manifests),
    }, manifests


def _formal_gate_evidence(plan: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Run the exact reproduction and selection gates used by ``run-main``."""
    # Local import avoids making experiments_v2.cli import this renderer path
    # while still keeping one canonical implementation of the formal gates.
    from .cli import _assert_formal_selection_gates, _assert_reproduction_gate

    selection = _assert_formal_selection_gates(plan)
    _assert_reproduction_gate(plan)
    repository = repository_snapshot(plan.repo_root)
    if (repository.get("dirty") is not False
            or repository.get("commit") in (None, "", "unknown")):
        raise RuntimeError(
            "final aggregation requires the current clean formal Git commit")
    return selection, repository


def _validate_manifests_against_current_plan(
        plan: Any, manifests: Sequence[Path], *, run_kind: str,
        repository: Mapping[str, Any]) -> None:
    """Reject a cohort produced from any pre-selection or stale plan state.

    Failed attempts remain useful coverage evidence.  Their native executable
    may never have emitted compiler metadata, so every attempt is bound through
    the exact resolved native config hash; successful M3/M4 attempts must also
    carry the complete runtime-attested native identity.
    """
    architecture_sha256 = sha256_file(plan.architecture_path)
    model_sha256 = sha256_file(plan.model_path)
    commit = str(repository["commit"])
    circuits: dict[tuple[str, str], Any] = {}
    experiment_ids: dict[str, str] = {}
    for dataset_name in ("zac18", "qmap154"):
        if dataset_name not in plan.datasets:
            raise ValueError(f"final plan is missing dataset {dataset_name}")
        dataset = plan.datasets[dataset_name]
        experiment_ids[dataset_name] = plan.experiment_id(dataset)
        for canonical in plan.load_suite(dataset):
            circuit = Path(canonical.canonical_path).stem
            identity = (dataset_name, circuit)
            if identity in circuits:
                raise ValueError(f"duplicate current canonical circuit: {identity}")
            circuits[identity] = canonical

    resolved_hashes: dict[tuple[str, int], str] = {}
    native_settings = {
        method: effective_zac_setting(plan.methods[method].payload)
        for method in ("M3", "M4")
    }
    for path in manifests:
        run = load_run_manifest(path, require_success_metrics=True)
        if run.run_kind != run_kind or run.ablation_variant:
            raise ValueError(
                f"non-final run identity in cohort: {run.run_id}")
        if run.method not in METHODS:
            raise ValueError(f"unknown method in final cohort: {run.run_id}")
        canonical = circuits.get((run.dataset, run.circuit))
        if canonical is None:
            raise ValueError(
                f"manifest is absent from the current frozen suite: {run.run_id}")
        if run.git_dirty or run.git_commit != commit:
            raise ValueError(
                f"manifest is not from current clean commit {commit}: {run.run_id}")
        expected = {
            "experiment_id": experiment_ids[run.dataset],
            "input_sha256": canonical.canonical_sha256,
            "architecture_sha256": architecture_sha256,
            "model_sha256": model_sha256,
        }
        key = (run.method, run.seed)
        if key not in resolved_hashes:
            resolved_hashes[key] = sha256_file(
                plan.resolved_config(run.method, run.seed))
        expected["config_sha256"] = resolved_hashes[key]
        drift = {
            field: (getattr(run, field), value)
            for field, value in expected.items()
            if getattr(run, field) != value
        }
        if drift:
            raise ValueError(
                f"manifest differs from current final resolved plan for "
                f"{run.run_id}: {drift}")

        if run.method not in native_settings:
            continue
        setting = native_settings[run.method]
        native_expected = {
            "algorithm_revision": setting["algorithm_revision"],
            "backend": "native",
            "native_abi_version": setting["native_abi_version"],
            "native_wheel_sha256": setting["native_wheel_sha256"],
            "tuning_protocol_id": setting["tuning_protocol_id"],
            "rng_version": setting["rng_version"],
        }
        # Any attested field must agree even for a failed attempt.  Success is
        # stricter: all fields and the frozen compiler flags are mandatory.
        native_drift = {
            field: (getattr(run, field), value)
            for field, value in native_expected.items()
            if (run.status == RunStatus.SUCCESS.value
                or getattr(run, field) not in (None, ""))
            and getattr(run, field) != value
        }
        if native_drift:
            raise ValueError(
                f"manifest native provenance differs from final config for "
                f"{run.run_id}: {native_drift}")
        if (run.status == RunStatus.SUCCESS.value
                or bool(run.compiler_and_flags)):
            required_flags = {
                "cxx_standard": 17, "openmp": False, "fast_math": False,
            }
            flag_drift = {
                key: (run.compiler_and_flags.get(key), value)
                for key, value in required_flags.items()
                if run.compiler_and_flags.get(key) != value
            }
            if flag_drift:
                raise ValueError(
                    f"manifest native compiler flags drift for {run.run_id}: "
                    f"{flag_drift}")


def _selection_identity(selection: Mapping[str, Any]) -> Mapping[str, Any]:
    initial_path = Path(str(selection["initial_selection"])).resolve()
    tuning_path = Path(str(selection["tuning_selection"])).resolve()
    for label, path in (("initial", initial_path), ("tuning", tuning_path)):
        if not path.is_file():
            raise FileNotFoundError(
                f"formal {label} selection disappeared after validation: {path}")
    return {
        "initial": {
            "path": str(initial_path),
            "sha256": sha256_file(initial_path),
            "record_sha256": selection["initial_selection_record_sha256"],
            "selected_engine": selection["initial_placement_engine"],
        },
        "tuning": {
            "path": str(tuning_path),
            "sha256": sha256_file(tuning_path),
            "manifest_sha256": selection["tuning_selection_manifest_sha256"],
            "shared_candidate_id": selection["shared_candidate_id"],
        },
    }


def _revalidate_aggregation_sources(
        plan: Any, provenance: Mapping[str, Any]) -> None:
    """Close the gate-to-write race by rechecking every live source identity."""
    current_repository = repository_snapshot(plan.repo_root)
    if (current_repository.get("dirty") is not False
            or current_repository.get("commit") !=
            provenance["repository"]["commit"]):
        raise RuntimeError("repository changed during final aggregation")
    datasets = {
        name: plan.datasets[name] for name in ("zac18", "qmap154")
    }
    current = {
        "plan_sha256": sha256_file(plan.path),
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
        "experiment_ids": {
            name: plan.experiment_id(dataset)
            for name, dataset in datasets.items()
        },
        "initial_selection_sha256": sha256_file(
            provenance["formal_selection"]["initial"]["path"]),
        "tuning_selection_sha256": sha256_file(
            provenance["formal_selection"]["tuning"]["path"]),
        "quality_index_sha256": sha256_file(
            provenance["quality_index"]["path"]),
        "timing_index_sha256": sha256_file(
            provenance["timing_index"]["path"]),
    }
    expected = {
        "plan_sha256": provenance["plan"]["sha256"],
        "architecture_sha256": provenance["architecture_sha256"],
        "model_sha256": provenance["model_sha256"],
        "experiment_ids": provenance["experiment_ids"],
        "initial_selection_sha256":
            provenance["formal_selection"]["initial"]["sha256"],
        "tuning_selection_sha256":
            provenance["formal_selection"]["tuning"]["sha256"],
        "quality_index_sha256": provenance["quality_index"]["sha256"],
        "timing_index_sha256": provenance["timing_index"]["sha256"],
    }
    drift = {
        key: (current[key], value)
        for key, value in expected.items() if current[key] != value
    }
    if drift:
        raise RuntimeError(
            f"formal aggregation sources changed before sealing: {drift}")


def _promote_immutable_files(
        source_paths: Mapping[str, Path], destination: Path) -> Mapping[str, Path]:
    """Promote deterministic aggregate files without replacing prior evidence."""
    destination.mkdir(parents=True, exist_ok=True)
    targets = {
        label: destination / source.name
        for label, source in source_paths.items()
    }
    # Preflight the complete set before moving the first byte.  This makes a
    # retry with different evidence fail without producing a mixed directory.
    for label, source in source_paths.items():
        target = targets[label]
        if target.exists():
            if (not target.is_file()
                    or sha256_file(target) != sha256_file(source)):
                raise FileExistsError(
                    f"refusing to replace final aggregate artifact: {target}")
    for label, source in source_paths.items():
        target = targets[label]
        if not target.exists():
            os.replace(source, target)
    return targets


def aggregate_from_indices(
        *, plan_path: str | os.PathLike[str],
        quality_index: str | os.PathLike[str],
        timing_index: str | os.PathLike[str],
        output_directory: str | os.PathLike[str]) -> Mapping[str, str]:
    plan = load_experiment_plan(plan_path)
    selection, repository = _formal_gate_evidence(plan)
    datasets = {
        name: plan.datasets[name]
        for name in ("zac18", "qmap154")
    }
    suites = {
        name: [Path(item.canonical_path).stem
               for item in plan.load_suite(dataset)]
        for name, dataset in datasets.items()
    }
    experiment_ids = {
        name: plan.experiment_id(dataset)
        for name, dataset in datasets.items()
    }
    quality_identity, quality_paths = _index_identity(
        quality_index, run_kind="main")
    timing_identity, timing_paths = _index_identity(
        timing_index, run_kind="timing")
    _validate_manifests_against_current_plan(
        plan, quality_paths, run_kind="main", repository=repository)
    _validate_manifests_against_current_plan(
        plan, timing_paths, run_kind="timing", repository=repository)

    provenance = {
        "provenance_schema": AGGREGATION_PROVENANCE_SCHEMA,
        "experiment_schema": 2,
        "contract_id": "native-ga-v1-formal-aggregation-v1",
        "plan": {
            "path": str(plan.path.resolve()),
            "sha256": sha256_file(plan.path),
        },
        "repository": dict(repository),
        "experiment_ids": experiment_ids,
        "architecture_sha256": sha256_file(plan.architecture_path),
        "model_sha256": sha256_file(plan.model_path),
        "final_resolved_config_sha256": {
            method: {
                str(seed): sha256_file(plan.resolved_config(method, seed))
                for seed in (FORMAL_QUALITY_SEEDS
                             if method in {"M3", "M4"} else (0,))
            }
            for method in METHODS
        },
        "formal_selection": _selection_identity(selection),
        "quality_index": quality_identity,
        "timing_index": timing_identity,
    }
    provenance["record_sha256"] = _record_hash(provenance)
    result = aggregate_final_results(
        quality_paths, timing_paths,
        expected_experiment_ids=experiment_ids,
        frozen_suites=suites)
    # Bind every rendered cell to the exact formal evidence.  The provenance is
    # both inline (so the contract is self-contained) and a separately hashed
    # record consumed by the renderer.
    result["aggregation_provenance"] = provenance
    result["workbook_contract"]["aggregation_provenance"] = provenance
    result["workbook_contract"]["aggregation_provenance_file"] = (
        "aggregation_provenance.json")

    _revalidate_aggregation_sources(plan, provenance)

    output = Path(output_directory).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix=".final-aggregate-", dir=output.parent) as directory:
        staged = write_final_results(result, directory)
        provenance_path = Path(directory) / "aggregation_provenance.json"
        _atomic_json(provenance_path, provenance)
        staged = {**staged, "aggregation_provenance": provenance_path}
        paths = _promote_immutable_files(staged, output)
    return {key: str(value) for key, value in paths.items()}


def _xlsx_sheet_names(path: Path) -> list[str]:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
        namespace = {"main":
                     "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        return [str(element.attrib["name"])
                for element in root.findall("main:sheets/main:sheet", namespace)]


def _load_aggregation_provenance(
        contract_path: Path, contract: Mapping[str, Any],
        explicit_path: str | os.PathLike[str] | None
        ) -> tuple[Path, Mapping[str, Any]]:
    inline = contract.get("aggregation_provenance")
    if not isinstance(inline, Mapping):
        raise ValueError("final workbook contract lacks aggregation provenance")
    declared = contract.get("aggregation_provenance_file")
    if explicit_path is None:
        if not isinstance(declared, str) or not declared:
            raise ValueError(
                "aggregation provenance path was neither supplied nor declared")
        source = (contract_path.parent / declared).resolve()
    else:
        source = Path(explicit_path).expanduser().resolve()
        if isinstance(declared, str) and declared:
            expected = (contract_path.parent / declared).resolve()
            if source != expected:
                raise ValueError(
                    "explicit aggregation provenance differs from contract path")
    if not source.is_file():
        raise FileNotFoundError(f"aggregation provenance is missing: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if (payload != inline
            or payload.get("provenance_schema") != AGGREGATION_PROVENANCE_SCHEMA
            or payload.get("record_sha256") != _record_hash(payload)):
        raise ValueError("aggregation provenance/contract binding changed")
    required = {
        "plan", "repository", "experiment_ids", "formal_selection",
        "quality_index", "timing_index",
    }
    if not required.issubset(payload):
        raise ValueError("aggregation provenance lacks formal evidence bindings")
    repository = payload["repository"]
    if (not isinstance(repository, Mapping)
            or repository.get("dirty") is not False
            or repository.get("commit") in (None, "", "unknown")):
        raise ValueError("aggregation provenance is not a clean formal commit")
    return source, payload


def _render_input_binding(
        *, contract_path: Path, contract: Mapping[str, Any],
        output_path: Path, provenance_path: Path,
        provenance: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "experiment_schema": 2,
        "contract_id": contract["contract_id"],
        "renderer_sha256": sha256_file(FINAL_RENDERER),
        "contract_sha256": sha256_file(contract_path),
        "source_csv_sha256": {
            str(sheet["source_csv"]): sha256_file(
                contract_path.parent / str(sheet["source_csv"]))
            for sheet in contract["sheets"]
        },
        "aggregation_provenance_path": str(provenance_path),
        "aggregation_provenance_sha256": sha256_file(provenance_path),
        "aggregation_provenance_record_sha256":
            provenance["record_sha256"],
        # Keep the full compact record inline so final_manifest directly binds
        # the plan, commit, experiment IDs, both selections and both indices.
        "aggregation_provenance": dict(provenance),
        "plan_sha256": provenance["plan"]["sha256"],
        "git_commit": provenance["repository"]["commit"],
        "experiment_ids": provenance["experiment_ids"],
        "initial_selection": provenance["formal_selection"]["initial"],
        "tuning_selection": provenance["formal_selection"]["tuning"],
        "quality_index": provenance["quality_index"],
        "timing_index": provenance["timing_index"],
        "xlsx_path": str(output_path),
        "sheet_names": list(SHEET_NAMES),
        "charts": False,
    }


def _reuse_immutable_render(
        final_manifest: Path, expected: Mapping[str, Any],
        qa_directory: Path) -> Mapping[str, Any] | None:
    if not final_manifest.exists():
        return None
    try:
        manifest = json.loads(final_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FileExistsError(
            f"refusing to replace unreadable final manifest: {final_manifest}") from error
    if manifest.get("record_sha256") != _record_hash(manifest):
        raise FileExistsError("existing final manifest failed its immutable hash")
    drift = {
        key: (manifest.get(key), value)
        for key, value in expected.items()
        if manifest.get(key) != value
    }
    if drift:
        raise FileExistsError(
            f"refusing to replace final render with different inputs: {drift}")
    xlsx = Path(str(manifest.get("xlsx_path", ""))).resolve()
    if (not xlsx.is_file()
            or manifest.get("xlsx_sha256") != sha256_file(xlsx)
            or _xlsx_sheet_names(xlsx) != list(SHEET_NAMES)):
        raise FileExistsError("existing immutable XLSX changed or disappeared")
    qa_hashes = manifest.get("qa_files_sha256")
    if not isinstance(qa_hashes, Mapping) or not qa_hashes:
        raise FileExistsError("existing final manifest lacks QA file hashes")
    expected_qa = set(str(name) for name in qa_hashes)
    actual_qa = ({path.name for path in qa_directory.iterdir() if path.is_file()}
                 if qa_directory.is_dir() else set())
    if actual_qa != expected_qa:
        raise FileExistsError("existing immutable workbook QA set changed")
    for name, digest in qa_hashes.items():
        path = qa_directory / str(name)
        if not path.is_file() or sha256_file(path) != digest:
            raise FileExistsError(f"existing immutable QA file changed: {path}")
    return {**manifest, "final_manifest": str(final_manifest)}


def render_final_workbook(
        *, contract_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        qa_directory: str | os.PathLike[str],
        node_executable: str | os.PathLike[str],
        node_modules: str | os.PathLike[str],
        aggregation_provenance_path: str | os.PathLike[str] | None = None
        ) -> Mapping[str, Any]:
    """Render, inspect and hash the exact two-sheet final workbook."""
    contract_path = Path(contract_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    qa_directory = Path(qa_directory).expanduser().resolve()
    node = Path(node_executable).expanduser().resolve()
    modules = Path(node_modules).expanduser().resolve()
    if not contract_path.is_file() or not FINAL_RENDERER.is_file():
        raise FileNotFoundError("final workbook contract or renderer is missing")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if (contract.get("experiment_schema") != 2
            or contract.get("contract_id") != FINAL_RESULTS_CONTRACT_ID
            or contract.get("sheet_names") != list(SHEET_NAMES)
            or contract.get("exact_sheet_count") != 2
            or contract.get("charts") is not False):
        raise ValueError("refusing a non-final workbook contract")
    sheets = contract.get("sheets")
    if (not isinstance(sheets, list)
            or [sheet.get("name") for sheet in sheets
                if isinstance(sheet, Mapping)] !=
            list(SHEET_NAMES)
            or len(sheets) != 2):
        raise ValueError("final workbook sheet descriptors drifted")
    for sheet in sheets:
        source = contract_path.parent / str(sheet.get("source_csv", ""))
        if not source.is_file():
            raise FileNotFoundError(f"final workbook source CSV is missing: {source}")

    provenance_path, provenance = _load_aggregation_provenance(
        contract_path, contract, aggregation_provenance_path)
    input_binding = _render_input_binding(
        contract_path=contract_path, contract=contract,
        output_path=output_path, provenance_path=provenance_path,
        provenance=provenance)
    final_manifest = output_path.parent / "final_manifest.json"
    reused = _reuse_immutable_render(
        final_manifest, input_binding, qa_directory)
    if reused is not None:
        return reused

    if not node.is_file() or not os.access(node, os.X_OK):
        raise FileNotFoundError(f"bundled Node executable is unavailable: {node}")
    if not modules.is_dir():
        raise FileNotFoundError(f"bundled node_modules is unavailable: {modules}")

    # A missing manifest plus existing payload is a partial or foreign render,
    # never authority to overwrite it.
    if output_path.exists():
        raise FileExistsError(
            f"refusing to replace unsealed final workbook: {output_path}")
    if qa_directory.exists() and any(qa_directory.iterdir()):
        raise FileExistsError(
            f"refusing to replace unsealed workbook QA: {qa_directory}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    qa_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix=".final-workbook-", dir=output_path.parent) as directory:
        temporary = Path(directory)
        (temporary / "node_modules").symlink_to(
            modules, target_is_directory=True)
        script = temporary / FINAL_RENDERER.name
        shutil.copy2(FINAL_RENDERER, script)
        temporary_xlsx = temporary / "four_methods_results.xlsx"
        temporary_qa = temporary / "qa"
        completed = subprocess.run(
            [str(node), str(script), str(contract_path),
             str(temporary_xlsx), str(temporary_qa)],
            cwd=temporary, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"artifact-tool workbook render failed: {detail}")
        if not temporary_xlsx.is_file():
            raise RuntimeError("artifact-tool did not create the final XLSX")
        names = _xlsx_sheet_names(temporary_xlsx)
        if names != list(SHEET_NAMES):
            raise RuntimeError(f"rendered workbook sheet drift: {names}")
        with ZipFile(temporary_xlsx) as archive:
            charts = [name for name in archive.namelist()
                      if name.startswith("xl/charts/")]
        if charts:
            raise RuntimeError("final workbook unexpectedly contains charts")
        qa_path = temporary_qa / "workbook_qa.json"
        if not qa_path.is_file():
            raise RuntimeError("artifact-tool workbook QA record is missing")
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        if ([row.get("name") for row in qa.get("sheets", [])] != names
                or qa.get("formula_errors") != []):
            raise RuntimeError("final workbook QA did not pass all sheets")
        preview_paths = []
        for row in qa["sheets"]:
            preview = temporary_qa / str(row.get("preview", ""))
            if not preview.is_file() or preview.stat().st_size == 0:
                raise RuntimeError(f"missing rendered sheet preview: {preview}")
            preview_paths.append(preview)

        qa_outputs = {}
        # Check targets again immediately before the one-way promotion.  The
        # final manifest is written last and is the seal for the complete set.
        if output_path.exists() or any(qa_directory.iterdir()):
            raise FileExistsError("final render targets appeared during rendering")
        os.replace(temporary_xlsx, output_path)
        for source in [*preview_paths, qa_path]:
            destination = qa_directory / source.name
            os.replace(source, destination)
            qa_outputs[source.name] = sha256_file(destination)

    manifest = {
        **input_binding,
        "xlsx_sha256": sha256_file(output_path),
        "qa_files_sha256": qa_outputs,
    }
    manifest["record_sha256"] = _record_hash(manifest)
    _write_immutable_json(final_manifest, manifest)
    return {**manifest, "final_manifest": str(final_manifest)}


def _read_path_list(path: str | os.PathLike[str]) -> list[str]:
    source = Path(path).expanduser().resolve()
    values = [line.strip() for line in source.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    if not values:
        raise ValueError(f"manifest path list is empty: {source}")
    return values


def _formal_report_identities(plan: Any, *, run_kind: str
                              ) -> list[tuple[str, str, str, int, int]]:
    """Reconstruct the complete final cohort from the current frozen suites."""
    if run_kind not in {"main", "timing"}:
        raise ValueError("formal report run_kind must be main or timing")
    identities: list[tuple[str, str, str, int, int]] = []
    for dataset_name in ("zac18", "qmap154"):
        if dataset_name not in plan.datasets:
            raise ValueError(f"formal report plan is missing {dataset_name}")
        circuits = sorted(
            Path(item.canonical_path).stem
            for item in plan.load_suite(plan.datasets[dataset_name]))
        if not circuits or len(circuits) != len(set(circuits)):
            raise ValueError(
                f"formal report suite is empty or ambiguous: {dataset_name}")
        for circuit in circuits:
            if run_kind == "main":
                identities.extend((dataset_name, circuit, method, 0, 0)
                                  for method in ("M1", "M2"))
                identities.extend(
                    (dataset_name, circuit, method, seed, 0)
                    for method in ("M3", "M4")
                    for seed in FORMAL_QUALITY_SEEDS)
            else:
                identities.extend(
                    (dataset_name, circuit, method, 0, repetition)
                    for method in METHODS
                    for repetition in range(FORMAL_TIMING_REPETITIONS))
    identities.sort()
    if len(identities) != len(set(identities)):
        raise ValueError("formal report plan contains duplicate identities")
    return identities


def _cohort_paths_from_report(
        plan: Any, report: Mapping[str, Any], *, run_kind: str,
        repository: Mapping[str, Any]) -> list[Path]:
    expected = _formal_report_identities(plan, run_kind=run_kind)
    rows = report.get("cohort_manifests")
    if not isinstance(rows, list):
        raise ValueError("formal run report lacks cohort_manifests")
    if report.get("planned_jobs") != len(expected) or len(rows) != len(expected):
        raise ValueError(
            "formal run report cohort count differs from the current plan: "
            f"expected={len(expected)}, planned={report.get('planned_jobs')}, "
            f"registered={len(rows)}")

    identities: list[tuple[str, str, str, int, int]] = []
    paths: list[Path] = []
    manifests = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("formal cohort row must be an object")
        raw_identity = row.get("identity")
        if (not isinstance(raw_identity, list) or len(raw_identity) != 5
                or any(isinstance(value, bool) for value in raw_identity[3:])):
            raise ValueError("formal cohort row has an invalid identity")
        try:
            identity = (
                str(raw_identity[0]), str(raw_identity[1]),
                str(raw_identity[2]), int(raw_identity[3]),
                int(raw_identity[4]),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("formal cohort row has an invalid identity") from error
        if list(identity) != raw_identity:
            raise ValueError("formal cohort identity types drifted")
        path = Path(str(row.get("path", ""))).expanduser().resolve()
        if not path.is_file() or path.is_dir():
            raise ValueError(f"registered run manifest disappeared: {path}")
        if row.get("sha256") != sha256_file(path):
            raise ValueError(f"registered run manifest hash changed: {path}")
        manifest = load_run_manifest(path, require_success_metrics=True)
        actual_identity = (
            manifest.dataset, manifest.circuit, manifest.method,
            manifest.seed, manifest.repetition,
        )
        declared_path = (Path(manifest.artifact_dir) / "manifest.json").resolve()
        if (identity != actual_identity
                or row.get("run_id") != manifest.run_id
                or row.get("status") != manifest.status
                or path != declared_path):
            raise ValueError(f"registered run manifest identity drift: {path}")
        if manifest.run_kind != run_kind or manifest.ablation_variant:
            raise ValueError(f"registered run has the wrong run_kind: {path}")
        identities.append(identity)
        paths.append(path)
        manifests.append(manifest)

    if identities != expected or len(paths) != len(set(paths)):
        raise ValueError(
            "formal run report is not the sorted, unique, complete cohort")
    _validate_manifests_against_current_plan(
        plan, paths, run_kind=run_kind, repository=repository)

    if run_kind == "timing":
        from .runtime_benchmark import validate_balanced_schedule

        circuit_keys = sorted(
            f"{dataset}/{circuit}"
            for dataset, circuit, _, _, _ in set(expected))
        schedule_path = (plan.output_root / "timing" /
                         "randomized_schedule.json").resolve()
        if Path(str(report.get("schedule_path", ""))).resolve() != schedule_path:
            raise ValueError("timing report points at a foreign schedule")
        if not schedule_path.is_file():
            raise FileNotFoundError(f"timing schedule disappeared: {schedule_path}")
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        validate_balanced_schedule(
            schedule, circuit_keys,
            repetitions=FORMAL_TIMING_REPETITIONS,
            seed=plan.bootstrap_seed,
            methods=METHODS)
        schedule_sha = str(schedule["sha256"])
        timed = report.get("timed")
        if not isinstance(timed, Mapping):
            raise ValueError("timing report lacks the timed-run evidence")
        if (report.get("schedule_sha256") != schedule_sha
                or report.get("schedule_jobs") != len(expected)
                or report.get("schedule_circuits") != len(circuit_keys)
                or timed.get("phase") != "timing"
                or timed.get("dry_run") is not False
                or timed.get("schedule_sha256") != schedule_sha
                or timed.get("scheduled_jobs") != len(expected)
                or timed.get("planned_jobs") != len(expected)
                or timed.get("cohort_manifests") != rows):
            raise ValueError("timing report/schedule binding drifted")
        scheduled = {
            (
                str(job["circuit"]).split("/", 1)[0],
                str(job["circuit"]).split("/", 1)[1],
                str(job["method"]), 0, int(job["repetition"]),
            ): int(job["order"])
            for job in schedule["jobs"]
        }
        if set(scheduled) != set(expected):
            raise ValueError("timing schedule identity set differs from cohort")
        for row, identity, manifest in zip(rows, identities, manifests):
            order = scheduled[identity]
            if (row.get("schedule_order") != order
                    or row.get("schedule_sha256") != schedule_sha
                    or manifest.package_versions.get(
                        "timing_schedule_protocol") != schedule["protocol_id"]
                    or manifest.package_versions.get(
                        "timing_schedule_sha256") != schedule_sha
                    or manifest.package_versions.get(
                        "timing_schedule_order") != str(order)):
                raise ValueError(
                    f"timing manifest/schedule identity drift: {identity}")
    return paths


def seal_report_manifest_index(
        *, plan_path: str | os.PathLike[str],
        report_path: str | os.PathLike[str], run_kind: str,
        output_path: str | os.PathLike[str]) -> Mapping[str, Any]:
    """Seal a completed run report without scanning an artifact directory."""
    plan = load_experiment_plan(plan_path)
    selection, repository = _formal_gate_evidence(plan)
    source = Path(report_path).expanduser().resolve()
    if not source.is_file() or source.is_dir():
        raise ValueError(f"formal run report is not a file: {source}")
    report = json.loads(source.read_text(encoding="utf-8"))
    if (report.get("experiment_schema") != 2
            or report.get("phase") != run_kind
            or report.get("dry_run") is not False):
        raise ValueError("formal run report phase/run_kind contract drifted")
    if report.get("formal_selection") != selection:
        raise ValueError("formal run report selection differs from current gates")
    paths = _cohort_paths_from_report(
        plan, report, run_kind=run_kind, repository=repository)
    return seal_manifest_index(
        paths, run_kind=run_kind, output_path=output_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal and aggregate explicit final-result cohorts")
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal-index")
    seal.add_argument("--run-kind", choices=("main", "timing"), required=True)
    seal.add_argument("--manifest-list", required=True)
    seal.add_argument("--output", required=True)
    seal_report = commands.add_parser("seal-report-index")
    seal_report.add_argument("--plan", required=True)
    seal_report.add_argument("--report", required=True)
    seal_report.add_argument(
        "--run-kind", choices=("main", "timing"), required=True)
    seal_report.add_argument("--output", required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--plan", required=True)
    aggregate.add_argument("--quality-index", required=True)
    aggregate.add_argument("--timing-index", required=True)
    aggregate.add_argument("--output-directory", required=True)
    render = commands.add_parser("render")
    render.add_argument("--contract", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--qa-directory", required=True)
    render.add_argument("--node", required=True)
    render.add_argument("--node-modules", required=True)
    render.add_argument("--aggregation-provenance")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "seal-index":
        result = seal_manifest_index(
            _read_path_list(args.manifest_list), run_kind=args.run_kind,
            output_path=args.output)
    elif args.command == "seal-report-index":
        result = seal_report_manifest_index(
            plan_path=args.plan, report_path=args.report,
            run_kind=args.run_kind, output_path=args.output)
    elif args.command == "aggregate":
        result = aggregate_from_indices(
            plan_path=args.plan, quality_index=args.quality_index,
            timing_index=args.timing_index,
            output_directory=args.output_directory)
    elif args.command == "render":
        result = render_final_workbook(
            contract_path=args.contract, output_path=args.output,
            qa_directory=args.qa_directory,
            node_executable=args.node, node_modules=args.node_modules,
            aggregation_provenance_path=args.aggregation_provenance)
    else:  # pragma: no cover
        raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "aggregate_from_indices", "load_manifest_index", "render_final_workbook",
    "seal_manifest_index", "seal_report_manifest_index",
]
