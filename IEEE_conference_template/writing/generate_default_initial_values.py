#!/usr/bin/env python3
"""Independently export the completed default-initialization quality study.

No accepted paper_zh_v2 file is rewritten. New default results are paired with
the two SHA-pinned original baselines, using the accepted aggregation order:
three-seed per-field medians, then QMAP canonical-cluster means, then geometric
mean fidelity and arithmetic mean physical costs on the common valid cohort.
The old SA-initialized dynamic ablations and serial timing are not relabelled.
Recovered worker outputs require a separately sealed independent quality audit;
their unknown exit codes and interrupted supervision never become timing data.

Usage from the repository root (no compiler is started):
  python IEEE_conference_template/writing/generate_default_initial_values.py --audit-only --summary PATH
    Verify a partial/final summary; print diagnostics, never paper statistics.
  python IEEE_conference_template/writing/generate_default_initial_values.py --output-root PATH
    After all jobs are terminal, verify the final quality_summary.json and write
    an independent immutable directory under IEEE_conference_template/build/
    default_initial_v1 or ZAC_zzx/results/default_initial_v1.
  python IEEE_conference_template/writing/generate_default_initial_values.py --output-root PATH --check
    Read-only byte-for-byte verification of a previously generated export.
No command changes accepted results, existing paper values, or a different
existing export. Omit --output-root to use the study's build/paper_exports.
The known six-worker execution amendment must be explicitly supplied using
--execution-amendment PATH (repeatable) or summary execution_amendments pins.
Formal exports require both supervisor completions and all affected-job evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
from typing import Any, Mapping, Sequence


PAPER = Path(__file__).resolve().parents[1]
REPO = PAPER.parent
ACCEPTED = Path("ZAC_zzx/results/paper_zh_v2/final_manifest.json")
ACCEPTED_SHA256 = "a58a6d953ed282d1a44bf0dfb096ef8790b5d05fbfa7397322af43023c5454e6"
DEFAULT_PROTOCOL = Path("ZAC_zzx/results/default_initial_v1/protocol.json")
DEFAULT_PROTOCOL_SHA256 = "56bf06493df746e7efa6f0ddec805dc6e88086fa000afb673634430ef274071d"
REQUIRED_EXECUTION_AMENDMENTS = {
    "f2adc3bf85ee3a8b1ec51aa8b88fb89e3a780693bdc14b84b90718da34a10b63":
        "IEEE_conference_template/build/default_initial_v1/expansions/extra-two-54csbfpr/amendment.json"}
PROTOCOL_ID = "default-initial-canonical169-seed012-v1"
FIXED_INITIALIZATION = {"horizon": 2, "candidates": 4, "rho": .7, "rollout_evaluations": 32}
EXPORT_SCHEMA = "default-initial-paper-values-v1"
DATASETS = ("zac18", "qmap154")
METHODS = ("M1", "M2", "Default")
SEEDS = (0, 1, 2)
COMPONENTS = ("log_atom_transfer", "log_idle_excitation", "log_coherence_linear")
FIELDS = ("log_fidelity", "move_batches", "move_time_us", "transfers", "idle_exposures", *COMPONENTS)
TERMINAL_FAILURES = {"timeout", "failed", "error", "runner_error", "oom",
                     "compiler_error", "verifier_fail", "scorer_error", "memory_limit", "interrupted"}
RECOVERY_CHECKS = {"result_stdout_sha", "native_trace_sha", "input_sha", "native_identity",
                   "id_seed", "instruction_sha", "selected_mapping_sha", "config", "candidate_hashes",
                   "selected_candidate_mapping", "physics_exact", "logical_exact", "score_exact",
                   "canonical_gate_count", "layers_count", "quality"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verified_json(reference: Mapping[str, Any], repo: Path) -> tuple[dict, dict]:
    if not isinstance(reference, Mapping) or not is_sha256(reference.get("sha256")):
        raise ValueError("every evidence reference requires an explicit SHA256")
    path = Path(reference["path"])
    path = (repo / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(repo.resolve()) or not path.is_file():
        raise ValueError(f"evidence is missing or outside the repository: {path}")
    if sha256(path) != reference["sha256"]:
        raise ValueError(f"evidence SHA256 drift: {path}")
    if "bytes" in reference and path.stat().st_size != reference["bytes"]:
        raise ValueError(f"evidence size drift: {path}")
    def invalid_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError(f"evidence JSON must be an object: {path}")
    return value, {"path": str(path.relative_to(repo.resolve())), "sha256": reference["sha256"]}


def finite(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite and numeric")
    if nonnegative and value < 0:
        raise ValueError(f"{label} cannot be negative")
    return float(value)


class Evidence:
    """Hash each shared large trace once; JSON reports remain independently read."""
    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self.pins = {}

    def file(self, reference: Mapping[str, Any]) -> tuple[Path, dict]:
        if not isinstance(reference, Mapping) or not is_sha256(reference.get("sha256")):
            raise ValueError("unsealed source reference")
        path = Path(reference["path"])
        path = (self.repo / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.repo) or not path.is_file():
            raise ValueError(f"source missing or outside repository: {path}")
        name = str(path.relative_to(self.repo))
        if name not in self.pins:
            self.pins[name] = {"path": name, "sha256": sha256(path), "bytes": path.stat().st_size}
        pin = self.pins[name]
        if pin["sha256"] != reference["sha256"] or ("bytes" in reference and pin["bytes"] != reference["bytes"]):
            raise ValueError(f"source hash/size drift: {path}")
        return path, pin

    def json(self, reference: Mapping[str, Any]) -> dict:
        path, _ = self.file(reference)
        return json.loads(path.read_text(encoding="utf-8"))

    def trace(self, reference: Mapping[str, Any]) -> dict:
        path, _ = self.file(reference)
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                return json.load(stream)
        return json.loads(path.read_text(encoding="utf-8"))


def validate_default_score(result: Mapping[str, Any], job: Mapping[str, Any], model: Mapping[str, Any]) -> dict:
    """Recheck the same fixed linear model from reported counts and idle times.

    This is a verification, not rescoring with an alternative model. Original
    F=null remains null whenever at least one atom is outside the model domain.
    """
    if (result.get("status") != "success" or result.get("input_sha256") != job["canonical_sha256"]
            or result.get("seed") != job["seed"] or result.get("n_qubits") != job["qubits"]
            or result.get("canonical_job_id") != job["job_id"]):
        raise ValueError("new result identity/status mismatch")
    validation, logical = result.get("validation", {}), result.get("logical_validation", {})
    if validation.get("ok") is not True or validation.get("ghost_hits") != 0 or logical.get("ok") is not True:
        raise ValueError("new result lacks physical/logical validation")
    for expected, observed in (("expected_gate_ledger_sha256", "observed_gate_ledger_sha256"),
                               ("compiled_layer_ledger_sha256", "observed_layer_ledger_sha256")):
        if not is_sha256(logical.get(expected)) or logical[expected] != logical.get(observed):
            raise ValueError("new result operation/layer ledger mismatch")
    score = result.get("score", {})
    counts = score.get("counts", {})
    if counts.get("one_qubit_gates") != job["gates_1q"] or counts.get("two_qubit_gates") != job["gates_2q"]:
        raise ValueError("new result gate counts differ from the canonical input")
    if validation.get("move_batches") != score.get("move_batches"):
        raise ValueError("physics/scorer movement batch counts disagree")
    idle = score.get("idle_time_us")
    if not isinstance(idle, list) or len(idle) != job["qubits"]:
        raise ValueError("new score lacks one idle time per atom")
    idle = [finite(value, "idle_time_us", nonnegative=True) for value in idle]
    expected_logs = {}
    for name, count, parameter in (("one_qubit_gate", "one_qubit_gates", "one_qubit_fidelity"),
                                   ("two_qubit_gate", "two_qubit_gates", "two_qubit_fidelity"),
                                   ("atom_transfer", "transfers", "transfer_fidelity"),
                                   ("idle_excitation", "idle_excitations", "idle_excitation_fidelity")):
        n = finite(counts.get(count), count, nonnegative=True)
        if not n.is_integer():
            raise ValueError("raw operation counts must be integers")
        expected_logs[name] = n * math.log(model[parameter])
    ood = any(value >= model["coherence_time_us"] for value in idle)
    if type(score.get("ood")) is not bool or score["ood"] != ood:
        raise ValueError("model OOD flag disagrees with per-atom idle times")
    expected_logs["coherence_linear"] = None if ood else math.fsum(
        math.log(1 - value / model["coherence_time_us"]) for value in idle)
    components = score.get("components", {})
    for name, expected in expected_logs.items():
        actual = components.get(name, {}).get("log_fidelity")
        if expected is None:
            if actual is not None:
                raise ValueError("OOD coherence term was replaced")
        elif not math.isclose(finite(actual, name), expected, rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError("reported fidelity component differs from the accepted model")
    if not ood and not math.isclose(finite(score.get("log_fidelity"), "log_fidelity"),
                                   math.fsum(expected_logs.values()), rel_tol=1e-10, abs_tol=1e-10):
        raise ValueError("reported total log fidelity differs from the accepted model")
    if ood and not any(str(w).startswith("linear coherence model out of domain for atoms: ")
                       for w in score.get("warnings", [])):
        raise ValueError("OOD score lacks the original linear-model diagnostic")
    return validated_record({"dataset": job["dataset"], "circuit": job["circuit"], "seed": job["seed"],
                             "canonical_sha256": job["canonical_sha256"], "canonical_job_id": job["job_id"],
                             "status": "success", "fidelity_ood": ood,
                             "metrics": {"log_fidelity": score.get("log_fidelity"), "fidelity": score.get("fidelity"),
                                         "move_batches": score.get("move_batches"), "move_time_us": score.get("move_time_us"),
                                         "transfers": counts.get("transfers"), "idle_exposures": counts.get("idle_excitations"),
                                         **{"log_" + name: components[name]["log_fidelity"]
                                            for name in ("atom_transfer", "idle_excitation", "coherence_linear")}}})


def validated_record(record: Mapping[str, Any]) -> dict:
    """Check a normalized observation; missing or failed scores stay missing."""
    row = dict(record)
    if row.get("dataset") not in DATASETS or not row.get("circuit"):
        raise ValueError("invalid dataset/circuit identity")
    if not is_sha256(row.get("canonical_sha256")):
        raise ValueError("invalid canonical SHA256")
    if type(row.get("seed")) is not int or row["seed"] not in SEEDS:
        raise ValueError("invalid seed")
    status = row.get("status")
    if status != "success":
        if status not in TERMINAL_FAILURES:
            raise ValueError("quality matrix contains a non-terminal observation")
        if row.get("metrics") not in (None, {}):
            raise ValueError("failed observation cannot supply usable metrics")
        row["metrics"] = {}
        row["fidelity_valid"] = False
        row["fidelity_underflow"] = False
        return row
    if type(row.get("fidelity_ood")) is not bool:
        raise ValueError("successful observation lacks an explicit OOD classification")
    metrics = dict(row.get("metrics", {}))
    for key in ("move_batches", "move_time_us", "transfers", "idle_exposures"):
        metrics[key] = finite(metrics.get(key), key, nonnegative=True)
    if row["fidelity_ood"]:
        if metrics.get("log_fidelity") is not None or metrics.get("fidelity") is not None:
            raise ValueError("OOD fidelity must remain null; no replacement model is allowed")
        metrics["log_fidelity"] = metrics["fidelity"] = None
        row["fidelity_valid"] = False
        row["fidelity_underflow"] = False
    else:
        log_f = finite(metrics.get("log_fidelity"), "log_fidelity")
        f = finite(metrics.get("fidelity"), "fidelity", nonnegative=True)
        if log_f > 1e-12 or f > 1 + 1e-12 or not math.isclose(f, math.exp(log_f), rel_tol=1e-9, abs_tol=0):
            raise ValueError("linear-model fidelity and log fidelity disagree")
        for key in COMPONENTS:
            metrics[key] = finite(metrics.get(key), key)
        row["fidelity_valid"] = True
        row["fidelity_underflow"] = f == 0.0
    row["metrics"] = metrics
    return row


def baseline_observation(raw: Mapping[str, Any], dataset: str, circuit: str,
                         method: str, canonical: str) -> dict:
    if (raw.get("dataset"), raw.get("circuit"), raw.get("method"),
            raw.get("seed"), raw.get("repetition")) != (dataset, circuit, method, 0, 0):
        raise ValueError("accepted baseline manifest identity mismatch")
    if raw.get("input_sha256") != canonical:
        raise ValueError("accepted baseline canonical SHA mismatch")
    ledger = raw.get("expected_gate_ledger_sha256")
    if not is_sha256(ledger):
        raise ValueError("accepted baseline lacks canonical gate ledger")
    status = raw.get("status")
    metrics = {}
    if status == "success":
        if raw.get("verifier_ok") is not True or raw.get("observed_gate_ledger_sha256") != ledger:
            raise ValueError("accepted baseline logical verification failed")
        components = raw.get("fidelity_components", {})
        metrics = {
            "log_fidelity": raw.get("log_fidelity"), "fidelity": raw.get("fidelity"),
            "move_batches": raw.get("move_batches"), "move_time_us": raw.get("move_time_us"),
            "transfers": raw.get("transfers") if raw.get("transfers") is not None else components.get("transfers"),
            "idle_exposures": raw.get("idle_exposures"),
            **{field: components.get(field) for field in COMPONENTS},
        }
    return validated_record({"dataset": dataset, "circuit": circuit, "method": method,
                             "canonical_sha256": canonical, "seed": 0, "status": status,
                             "fidelity_ood": raw.get("fidelity_ood"), "metrics": metrics})


def load_accepted_baselines(repo: Path = REPO, *, accepted_reference: Mapping[str, Any] | None = None):
    """Follow the original final-manifest closure instead of old aggregate means.

    Original baselines are scored as emitted, exactly as in paper_zh_v2. Their
    recorded ghost counts are retained in provenance, not filtered by a new
    compiler's feasibility policy. Only new default results require ghost=0.
    """
    final, final_pin = verified_json(accepted_reference or
                                     {"path": str(ACCEPTED), "sha256": ACCEPTED_SHA256}, repo)
    if final.get("protocol") != "paper-zh-v2-final-manifest-v1" or set(final.get("datasets", {})) != set(DATASETS):
        raise ValueError("unrecognized accepted main-result closure")
    source, source_pin = verified_json(final["quality_source_manifest"], repo)
    frozen, freeze_pin = verified_json(final["paper_freeze"], repo)
    if set(source.get("baselines", {})) != {"M1", "M2"}:
        raise ValueError("accepted source inventory is not the two original baselines")
    if source.get("freeze_id") != frozen.get("freeze_id") or final["paper_freeze"].get("freeze_id") != frozen.get("freeze_id"):
        raise ValueError("accepted source/final/freeze identity mismatch")
    inventory, observations, pins = {dataset: {} for dataset in DATASETS}, [], []
    expected_keys = {f"{dataset}/{circuit}" for dataset in DATASETS for circuit in final["datasets"][dataset]}
    for method in ("M1", "M2"):
        if set(source["baselines"][method]) != expected_keys:
            raise ValueError("accepted baseline has missing or extra circuits")
    for dataset in DATASETS:
        canonical_inputs = frozen["canonical_suites"][dataset]["canonical_inputs"]
        if set(canonical_inputs) != set(final["datasets"][dataset]):
            raise ValueError("accepted canonical/final circuit inventories disagree")
        for circuit in sorted(final["datasets"][dataset]):
            sizes, gate_ledgers = set(), set()
            canonical = canonical_inputs[circuit]
            for method in ("M1", "M2"):
                reference = source["baselines"][method][f"{dataset}/{circuit}"]
                raw, pin = verified_json(reference, repo)
                if raw.get("status") != reference.get("status"):
                    raise ValueError("accepted baseline source/raw status mismatch")
                observations.append(baseline_observation(raw, dataset, circuit, method, canonical))
                if all(raw.get(key) is not None for key in ("qubits", "expected_gates_1q", "expected_gates_2q")):
                    sizes.add((int(raw["qubits"]), int(raw["expected_gates_1q"]), int(raw["expected_gates_2q"])))
                gate_ledgers.add(raw["expected_gate_ledger_sha256"])
                pins.append({**pin, "dataset": dataset, "circuit": circuit, "method": method,
                             "status": raw["status"], "original_ghost_hits": raw.get("ghost_hits")})
            if len(sizes) != 1 or len(gate_ledgers) != 1:
                raise ValueError("two baseline circuit sizes or gate ledgers disagree")
            n, gates_one, gates = sizes.pop()
            inventory[dataset][circuit] = {"canonical_sha256": canonical, "qubits": n,
                                           "gates_1q": gates_one, "gates_2q": gates}
    provenance = {"accepted_final_manifest": final_pin, "accepted_quality_source": source_pin,
                  "accepted_freeze": freeze_pin, "accepted_model_sha256": frozen["model"]["sha256"],
                  "accepted_architecture_sha256": frozen["architecture"]["sha256"], "baseline_records": pins,
                  "baseline_scoring": "original_native_outputs_no_new_ghost_filter"}
    return observations, inventory, provenance


def same_score(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Original scorer values may differ only within its published roundoff tolerance."""
    if set(first) != set(second):
        return False
    def equal(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return same_score(a, b)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        if isinstance(a, (float, int)) and not isinstance(a, bool) and isinstance(b, (float, int)) and not isinstance(b, bool):
            return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
        return type(a) is type(b) and a == b
    return all(equal(first[key], second[key]) for key in first)


def verify_default_result(result, job, protocol, model, evidence, *, expected_origin=None):
    observation = validate_default_score(result, job, model)
    origin = expected_origin or job["origin"]
    if result.get("origin") != origin:
        raise ValueError("result origin was relabelled")
    native = result.get("native", {})
    for key in ("native_abi_version", "native_wheel_sha256", "extension_sha256"):
        if native.get(key) != protocol["native"][key]:
            raise ValueError("result native runtime differs from sealed protocol")
    if native.get("wheel_registered") is not True:
        raise ValueError("new result did not use a registered native wheel")
    selection = result.get("selection", {})
    if (selection.get("policy_id") != "physical-prefix-initial-v1"
            or selection.get("config") != {**FIXED_INITIALIZATION, "seed": job["seed"]}
            or not is_sha256(result.get("selected_mapping_sha256"))
            or result["selected_mapping_sha256"] != selection.get("selected_mapping_sha256")):
        raise ValueError("result initializer differs from the frozen H2/K4/rho0.7/B32 policy")
    trace = evidence.trace(result["native_trace"])
    if stable_hash(trace["instructions"]) != result.get("native_instruction_sha256"):
        raise ValueError("actual native instructions differ from result declaration")
    init = [instruction for instruction in trace["instructions"] if instruction["type"] == "init"]
    if len(init) != 1:
        raise ValueError("native trace must initialize exactly once")
    mapping = [row[1:] for row in sorted(init[0]["init_locs"])]
    if stable_hash(mapping) != result["selected_mapping_sha256"]:
        raise ValueError("selected mapping is not the mapping in the native output")
    del trace
    if origin == "reused":
        pins = job.get("reuse", {}).get("evidence")
        if not isinstance(pins, dict) or result.get("source_evidence") != pins:
            raise ValueError("reused result lacks its exact sealed source-evidence closure")
        required = {"phase_protocol", "phase_summary", "receipt", "protocol", "result",
                    "native_trace", "selection", "choices", "base_mapping", "summary"}
        if set(pins) != required:
            raise ValueError("reused source-evidence inventory differs from protocol")
        for reference in pins.values():
            evidence.file(reference)
        original = evidence.json(pins["result"])
        original_protocol = evidence.json(pins["protocol"])
        original_selection = evidence.json(pins["selection"])
        original_summary = evidence.json(pins["summary"])
        original_receipt = evidence.json(pins["receipt"])
        if (original.get("status") != "success" or original.get("variant") != "h2"
                or original.get("horizon") != 2 or original_receipt.get("status") != "success"
                or original_summary.get("source_stable") is not True
                or original_protocol.get("input_sha256") != job["canonical_sha256"]
                or original_protocol.get("dynamic_setting", {}).get("seed") != job["seed"]):
            raise ValueError("reused source identity/status is not the original successful H2 run")
        if original_selection.get("config") != {**FIXED_INITIALIZATION, "seed": job["seed"]}:
            raise ValueError("reused initializer controls differ")
        if original_protocol.get("architecture_sha256") != protocol["architecture"]["sha256"]:
            raise ValueError("reused architecture differs from the new default")
        for key in ("native_abi_version", "native_wheel_sha256", "extension_sha256", "version",
                    "rng_version", "rich_boundary_wire_version"):
            if key not in original_protocol.get("native", {}) or original_protocol["native"][key] != protocol["native"].get(key):
                raise ValueError("reused native implementation/protocol differs: " + key)
        choices = evidence.json(pins["choices"])
        h2 = [row for row in choices.get("choices", []) if row.get("variant") == "h2"]
        if (choices.get("selection_uses_full_compile_results") is not False or len(h2) != 1
                or h2[0].get("mapping_sha256") != result["selected_mapping_sha256"]):
            raise ValueError("reused mapping was not sealed before full compilation")
        ignored = {"name", "dir", "seed", "arch_spec", "init_strategy", "initial_lookahead"}
        old_setting = {k: v for k, v in original_protocol["dynamic_setting"].items() if k not in ignored}
        new_setting = {k: v for k, v in protocol["dynamic_setting"].items() if k not in ignored}
        if old_setting != new_setting:
            raise ValueError("reused dynamic controls differ from the new default")
        if (not same_score(original["score"], result["score"])
                or original.get("selected_mapping_sha256") != result["selected_mapping_sha256"]
                or original.get("native_instruction_sha256") != result["native_instruction_sha256"]
                or result["native_trace"] != pins["native_trace"]):
            raise ValueError("reused quality/mapping/instructions were changed")
        if "end_to_end_ns" in result or "end_to_end_cpu_ns" in result:
            raise ValueError("historical timing was relabelled as the new public-entry timing")
    else:
        if "source_evidence" in result:
            raise ValueError("fresh result unexpectedly carries a reuse source")
        for field in ("end_to_end_ns", "end_to_end_cpu_ns"):
            finite(result.get(field), field, nonnegative=True)
    classification = "model_out_of_domain" if observation["fidelity_ood"] else "valid_fidelity"
    if result.get("quality_classification") != classification:
        raise ValueError("result quality classification contradicts the original model")
    observation["origin"] = origin
    return observation


def verify_recovery(receipt, result, job, protocol, evidence, expected_protocol_sha256):
    """Admit independently audited quality, without reconstructing process success.

    The audit is external evidence produced by replaying the saved trace through
    the frozen validators and scorer. The normal result checks still run below.
    Original worker timing fields remain in the sealed result as diagnostics;
    no missing supervisor measurement or return code is inferred from them.
    """
    if job["origin"] != "fresh":
        raise ValueError("only fresh interrupted workers use quality recovery")
    for field in ("returncode", "elapsed_s", "ended_at", "peak_observed_rss_bytes"):
        if field not in receipt or receipt[field] is not None:
            raise ValueError("recovered supervisor exit/timing/RSS must remain explicitly unknown")
    recovery = receipt.get("recovery", {})
    if (recovery.get("supervision_gap") is not True or recovery.get("returncode_observed") is not False
            or recovery.get("timing_eligible") is not False):
        raise ValueError("recovered quality must disclose supervision gap and exclude timing")
    audit = evidence.json(recovery.get("audit"))
    if audit.get("audit_schema") != 1 or audit.get("audit_type") != "interrupted_worker_quality_recovery":
        raise ValueError("unrecognized independent recovery audit")
    protocol_pin = audit.get("protocol", {})
    if protocol_pin.get("sha256") != expected_protocol_sha256:
        raise ValueError("recovery audit protocol identity differs")
    evidence.file(protocol_pin)
    if "auditor" in audit:
        evidence.file(audit["auditor"])
    source = audit.get("source_checks", {})
    if (any(source.get(name) is not True for name in
            ("driver", "config", "architecture", "accepted_manifest", "python", "native_extension"))
            or ("native_wheel" in source and source["native_wheel"] is not True)
            or source.get("frozen_source_file_count") != len(protocol["frozen_source_files"])
            or source.get("frozen_source_mismatches") != []):
        raise ValueError("independent recovery audit lacks complete frozen source checks")
    audited = [entry for entry in audit.get("jobs", []) if entry.get("job_id") == job["job_id"]]
    if len(audited) != 1:
        raise ValueError("recovery audit must identify the job exactly once")
    entry = audited[0]
    if (entry.get("canonical_sha256") != job["canonical_sha256"] or entry.get("seed") != job["seed"]
            or entry.get("result") != receipt.get("result") or entry.get("native_trace") != result.get("native_trace")
            or entry.get("started_receipt") != recovery.get("started_receipt")):
        raise ValueError("recovery audit does not bind the exact input/result/trace/start evidence")
    if ("observed_returncode" not in entry or entry["observed_returncode"] is not None
            or entry.get("exit_reason") != "unknown" or entry.get("timing_eligible") is not False):
        raise ValueError("recovery audit cannot infer process exit or eligible timing")
    if any(entry.get("checks", {}).get(name) is not True for name in RECOVERY_CHECKS):
        raise ValueError("independent recovery quality checks are missing or failed")
    started = evidence.json(entry.get("started_receipt"))
    if started.get("job_id") != job["job_id"] or not isinstance(started.get("at"), str) or not started["at"]:
        raise ValueError("recovery audit lacks the original job start marker")
    stdout = evidence.json(entry.get("worker_stdout"))
    if stdout.get("result") != receipt["result"] or stdout.get("quality_classification") != result.get("quality_classification"):
        raise ValueError("recovered result differs from the worker's sealed completion output")
    if "compiler_log" in entry:
        evidence.file(entry["compiler_log"])
    return {"audit": recovery["audit"], "started_receipt": entry["started_receipt"],
            "worker_stdout": entry["worker_stdout"], "returncode": None,
            "supervision_gap": True, "timing_eligible": False}


def verify_terminal_receipt(receipt, row, job, protocol, model, evidence, expected_protocol_sha256):
    """Keep observed process completion distinct from audited recovered quality."""
    status = row.get("status")
    phase = "reuse" if job["origin"] == "reused" else "quality"
    if (receipt.get("job_id") != job["job_id"] or receipt.get("phase") != phase
            or receipt.get("protocol_sha256") != expected_protocol_sha256 or receipt.get("status") != status
            or receipt.get("result") != row.get("result")):
        raise ValueError("summary does not match its original terminal receipt")
    if status in {"success", "recovered_success"}:
        if status == "success" and (type(receipt.get("returncode")) is not int or receipt["returncode"] != 0
                                    or "recovery" in receipt):
            raise ValueError("successful receipt requires an observed zero process return code")
        result = evidence.json(row["result"])
        recovery = None
        if status == "recovered_success":
            recovery = verify_recovery(receipt, result, job, protocol, evidence, expected_protocol_sha256)
        observation = verify_default_result(result, job, protocol, model, evidence)
        if row.get("quality_classification") != result["quality_classification"] or receipt.get("quality_classification") != result["quality_classification"]:
            raise ValueError("summary/receipt quality classification mismatch")
        if recovery is not None:
            observation["recovery"] = recovery
    else:
        if row.get("result") is not None:
            raise ValueError("failed run cannot contribute a successful result")
        observation = validated_record({"dataset": job["dataset"], "circuit": job["circuit"], "seed": job["seed"],
                                        "canonical_sha256": job["canonical_sha256"], "canonical_job_id": job["job_id"],
                                        "status": status, "metrics": {}})
        observation["origin"] = job["origin"]
    observation["receipt_status"] = status
    return observation


def execution_time(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution timestamp must be an ISO datetime") from exc
    if result.tzinfo is None:
        raise ValueError("execution timestamps must specify their timezone")
    return result


def audit_execution_amendments(references, protocol, protocol_path, jobs, verified_jobs, evidence,
                               *, allow_partial=False, required=REQUIRED_EXECUTION_AMENDMENTS):
    """Audit explicitly supplied execution changes; never discover a latest run.

    Worker contention can change timeout coverage for both supervisors. Closed
    intervals therefore require every fresh job's start/terminal evidence, not
    just extra-worker events. Unknown end times are conservatively included.
    """
    protocol_path = Path(protocol_path).resolve()
    def read_path(path):
        path = Path(path).resolve()
        reference = {"path": str(path), "sha256": sha256(path)}
        return evidence.json(reference), reference

    reports, seen = [], set()
    for reference in references:
        amendment = evidence.json(reference)
        digest = reference["sha256"]
        if digest in seen:
            continue
        seen.add(digest)
        amendment_path, amendment_pin = evidence.file(reference)
        sidecar, _ = read_path(amendment_path.with_suffix(".sha256.json"))
        sidecar_path, _ = evidence.file(sidecar)
        if sidecar_path != amendment_path or sidecar["sha256"] != digest:
            raise ValueError("execution amendment sidecar differs")
        if (amendment.get("amendment_type") != "author_requested_concurrency_expansion"
                or (amendment.get("original_workers"), amendment.get("extra_workers"), amendment.get("total_workers")) != (4, 2, 6)
                or protocol.get("max_workers") != 4
                or amendment.get("timeout_s") != protocol.get("timeout_s") or amendment.get("timeout_s") != 600
                or amendment.get("rss_limit_bytes") != protocol.get("rss_limit_bytes")
                or amendment.get("rss_limit_bytes") != 3 * 1024**3
                or amendment.get("computational_controls_changed") is not False
                or amendment.get("timing_comparison_eligible") is not False
                or amendment.get("existing_results_preserved") is not True
                or not amendment.get("coverage_caveat") or not amendment.get("user_authorization")):
            raise ValueError("execution amendment changes unsupported controls or hides coverage/timing limits")
        pinned_protocol, _ = evidence.file(amendment["protocol"])
        if pinned_protocol != protocol_path or amendment["protocol"]["sha256"] != sha256(protocol_path):
            raise ValueError("execution amendment refers to a different sealed protocol")
        evidence.file(protocol["driver"])
        evidence.file(amendment["supervisor"])
        primary = evidence.json(amendment["primary_execution"])
        primary_dir = Path(amendment["primary_run_dir"]).resolve()
        primary_path, _ = evidence.file(amendment["primary_execution"])
        if primary_path != primary_dir / "execution.json":
            raise ValueError("primary execution path differs from amendment")
        launch_ref, _ = read_path(primary_dir / "launch.sha256.json")
        launch = evidence.json(launch_ref)
        evidence.file(launch["supervisor"])
        if (launch.get("protocol") != amendment["protocol"] or primary.get("protocol") != amendment["protocol"]
                or launch.get("workers") != 4 or primary.get("workers") != 4
                or primary.get("timeout_s") != 600 or primary.get("rss_limit_bytes") != 3 * 1024**3):
            raise ValueError("primary execution changed sealed per-job limits or source")
        selected = primary.get("selected_jobs", [])
        if len(selected) != len(set(selected)) or any(job_id not in jobs or jobs[job_id]["origin"] != "fresh" for job_id in selected):
            raise ValueError("primary execution has invalid or duplicate job identities")
        report = {"amendment": amendment_pin, "workers": {"original": 4, "extra": 2, "total": 6},
                  "per_job_timeout_s": 600, "per_job_rss_limit_bytes": 3 * 1024**3,
                  "computational_controls_changed": False, "timing_comparison_eligible": False,
                  "coverage_may_change_for_primary_and_extra_workers": True,
                  "coverage_caveat": amendment["coverage_caveat"], "complete": False, "missing_closure": []}
        execution_path = amendment_path.parent / "execution.json"
        completion_path = amendment_path.parent / "completion.json"
        primary_completion_path = primary_dir / "completion.json"
        for name, path in (("execution", execution_path), ("completion", completion_path),
                           ("primary_completion", primary_completion_path)):
            if not path.is_file():
                report["missing_closure"].append(name)
        if not execution_path.is_file():
            reports.append(report)
            continue
        execution, report["execution"] = read_path(execution_path)
        if (execution.get("amendment", {}).get("sha256") != digest
                or execution.get("primary_launch") != launch or execution.get("primary_pid") != primary.get("pid")
                or execution.get("extra_workers") != 2 or execution.get("total_ceiling") != 6):
            raise ValueError("extra execution does not match its amendment and primary launch")
        linked_amendment, _ = evidence.file(execution["amendment"])
        if linked_amendment != amendment_path:
            raise ValueError("extra execution amendment path differs")
        queue = execution.get("reverse_queue", [])
        if len(queue) != len(set(queue)) or not set(queue) <= set(selected):
            raise ValueError("extra queue is not a unique subset of the primary selection")
        begin = execution_time(execution["started_at"])
        report["started_at"] = execution["started_at"]
        if report["missing_closure"]:
            reports.append(report)
            continue
        completion, report["completion"] = read_path(completion_path)
        primary_completion, report["primary_completion"] = read_path(primary_completion_path)
        end = execution_time(completion["ended_at"])
        if (completion.get("started_at") != execution["started_at"] or end < begin
                or completion.get("includes_primary_jobs") is not True
                or completion.get("requires_final_independent_audit") is not True
                or primary_completion.get("completed_dispatches") != len(selected)):
            raise ValueError("supervisor completion does not close its execution")
        if execution_time(primary_completion["at"]) < execution_time(primary["started_at"]):
            raise ValueError("primary completion predates its execution")
        report["ended_at"] = completion["ended_at"]
        errors = []
        for job_id in queue:
            event_path = amendment_path.parent / "events" / (job_id + ".json")
            if not event_path.is_file():
                report["missing_closure"].append("extra_event:" + job_id)
                continue
            event, _ = read_path(event_path)
            if event.get("job_id") != job_id or not begin <= execution_time(event["at"]) <= end:
                raise ValueError("extra event identity/time differs from its execution")
            if event.get("action") == "supervisor_error":
                errors.append(job_id)
        if set(errors) != set(completion.get("errors", [])):
            raise ValueError("extra supervisor errors differ from completion")
        extra_event_ids = {path.stem for path in (amendment_path.parent / "events").glob("*.json")}
        if extra_event_ids - set(queue):
            raise ValueError("extra supervisor events contain jobs outside its declared queue")
        primary_errors = primary_completion.get("errors", [])
        if len(primary_errors) != len(set(primary_errors)) or not set(primary_errors) <= set(selected):
            raise ValueError("primary supervisor completion contains invalid error identities")
        for job_id in primary_errors:
            error_path = primary_dir / "errors" / (job_id + ".json")
            if not error_path.is_file():
                report["missing_closure"].append("primary_error:" + job_id)
                continue
            error, _ = read_path(error_path)
            if (error.get("job_id") != job_id
                    or not execution_time(primary["started_at"]) <= execution_time(error["at"]) <= execution_time(primary_completion["at"])):
                raise ValueError("primary error identity/time differs from its execution")
        affected, uncertain, statuses = [], [], Counter()
        for job_id, job in sorted(jobs.items()):
            if job["origin"] != "fresh":
                continue
            start_path = protocol_path.parent / "quality/receipts" / (job_id + ".started.json")
            row, observation = verified_jobs[job_id]
            if not start_path.is_file() or observation is None:
                report["missing_closure"].append("fresh_start_or_terminal:" + job_id)
                continue
            start, _ = read_path(start_path)
            if start.get("job_id") != job_id:
                raise ValueError("fresh start marker identity differs")
            receipt = evidence.json(row["receipt"])
            started = execution_time(start["at"])
            ended = execution_time(receipt["ended_at"]) if receipt.get("ended_at") is not None else None
            if ended is not None and ended < started:
                raise ValueError("fresh terminal predates its start")
            if started <= end and (ended is None or ended >= begin):
                affected.append(job_id)
                statuses[row["status"]] += 1
                if ended is None:
                    uncertain.append(job_id)
        declared = completion.get("contention_affected_job_ids", [])
        if len(declared) != len(set(declared)) or not set(declared) <= jobs.keys():
            raise ValueError("invalid contention affected identity list")
        if not report["missing_closure"] and set(declared) != set(affected):
            raise ValueError("contention affected ids omit or add jobs from either supervisor")
        report.update(complete=not report["missing_closure"], potentially_affected_job_ids=affected,
                      affected_status_counts=dict(statuses), unknown_end_conservatively_included=uncertain,
                      extra_supervisor_errors=errors, primary_supervisor_errors=primary_errors,
                      interval_rule="start <= expansion_end and (end unknown or end >= expansion_start)")
        reports.append(report)
    missing = sorted(set(required) - seen)
    complete = not missing and all(report["complete"] for report in reports)
    result = {"complete": complete, "missing_required_amendment_sha256": missing, "amendments": reports,
              "coverage_comparability": "Worker contention may alter success/timeout coverage for both supervisors; per-job limits remain fixed.",
              "timing_comparison_eligible": False}
    if not complete and not allow_partial:
        raise ValueError("execution amendment closure is incomplete; use audit-only until both supervisors and all receipts close")
    return result


def load_default_results(inventory, baseline_provenance, *, repo=REPO, protocol_path=None,
                         summary_path=None, expected_protocol_sha256=DEFAULT_PROTOCOL_SHA256,
                         allow_partial=False, execution_amendments=()):
    repo = Path(repo).resolve()
    protocol_path = Path(protocol_path or repo / DEFAULT_PROTOCOL).resolve()
    summary_path = Path(summary_path or protocol_path.parent / "quality_summary.json").resolve()
    evidence = Evidence(repo)
    protocol = evidence.json({"path": str(protocol_path), "sha256": expected_protocol_sha256})
    sidecar = json.loads(protocol_path.with_name("protocol.sha256.json").read_text())
    if Path(sidecar.get("path", "")).resolve() != protocol_path or sidecar.get("sha256") != expected_protocol_sha256:
        raise ValueError("protocol sidecar differs from the externally fixed seal")
    if protocol.get("protocol_id") != PROTOCOL_ID or protocol.get("seeds") != list(SEEDS):
        raise ValueError("unrecognized default study/seeds")
    if protocol.get("fixed_initialization") != FIXED_INITIALIZATION:
        raise ValueError("fixed initialization controls changed")
    if protocol["accepted_manifest"]["sha256"] != baseline_provenance["accepted_final_manifest"]["sha256"]:
        raise ValueError("new study does not reference the accepted baseline evidence")
    evidence.file(protocol["accepted_manifest"])
    config = evidence.json(protocol["config"])
    if config.get("zac_setting") != [protocol.get("dynamic_setting")]:
        raise ValueError("public configuration and sealed effective setting differ")
    if (protocol["dynamic_setting"].get("init_strategy") != "physical_prefix"
            or protocol["dynamic_setting"].get("initial_lookahead") != FIXED_INITIALIZATION):
        raise ValueError("new public configuration is not default-on")
    evidence.file(protocol["driver"])
    evidence.file(protocol["architecture"])
    if protocol["architecture"]["sha256"] != baseline_provenance["accepted_architecture_sha256"]:
        raise ValueError("new default and baselines use different architectures")
    for name, digest in protocol["frozen_source_files"].items():
        evidence.file({"path": str(Path(protocol["frozen_source"]) / name), "sha256": digest})
    model = evidence.json({"path": "ZAC_zzx/evaluation/fidelity_model_zac.json",
                           "sha256": baseline_provenance["accepted_model_sha256"]})
    if protocol["native"].get("native_abi_version") != 9:
        raise ValueError("default study requires the frozen ABI9 runtime")
    evidence.file({"path": protocol["native"]["extension_path"], "sha256": protocol["native"]["extension_sha256"]})
    if sha256(Path(protocol["python"])) != protocol["python_sha256"]:
        raise ValueError("sealed Python executable changed")
    expected = {(dataset, circuit, seed) for dataset in DATASETS for circuit in inventory[dataset] for seed in SEEDS}
    jobs, plan_rows = {}, {}
    canonical_keys = set()
    for job in protocol["jobs"]:
        key = (job["canonical_sha256"], job["seed"])
        if job["job_id"] in jobs or key in canonical_keys or job["seed"] not in SEEDS:
            raise ValueError("duplicate/invalid actual experiment identity")
        canonical_keys.add(key)
        jobs[job["job_id"]] = job
        if job["origin"] not in {"fresh", "reused"}:
            raise ValueError("unrecognized fresh/reused status")
        evidence.file(job["input"])
        if job["input"]["sha256"] != job["canonical_sha256"]:
            raise ValueError("job/input canonical hash mismatch")
        for label in job["labels"]:
            identity = (label["dataset"], label["circuit"], job["seed"])
            declared = inventory.get(label["dataset"], {}).get(label["circuit"])
            if not declared or {k: label[k] for k in declared} != declared or label["canonical_sha256"] != job["canonical_sha256"]:
                raise ValueError("canonical aliases differ from accepted input inventory")
            if identity in plan_rows:
                raise ValueError("display alias appears in multiple actual experiments")
            plan_rows[identity] = (job, label)
    if set(plan_rows) != expected:
        raise ValueError("new planned aliases do not cover the full accepted suites")
    if (protocol["display_count"] * 3 != len(expected) or protocol["canonical_count"] * 3 != len(jobs)):
        raise ValueError("protocol declared experiment/display counts are inconsistent")
    smoke_pins = []
    if len(set(protocol["smoke_job_ids"])) != 4:
        raise ValueError("the four prespecified public-entry parity smokes are missing")
    for job_id in protocol["smoke_job_ids"]:
        path = protocol_path.parent / "smoke/receipts" / (job_id + ".json")
        receipt_pin = {"path": str(path), "sha256": sha256(path)}
        receipt = evidence.json(receipt_pin)
        if (receipt.get("job_id") != job_id or receipt.get("phase") != "smoke"
                or receipt.get("protocol_sha256") != expected_protocol_sha256
                or receipt.get("status") != "success" or receipt.get("returncode") != 0):
            raise ValueError("public-entry parity smoke did not complete successfully")
        result = evidence.json(receipt["result"])
        verify_default_result(result, jobs[job_id], protocol, model, evidence, expected_origin="fresh")
        original = evidence.json(jobs[job_id]["reuse"]["evidence"]["result"])
        if (result.get("historical_h2_parity") is not True or not same_score(result["score"], original["score"])
                or result["selected_mapping_sha256"] != original["selected_mapping_sha256"]
                or result["native_instruction_sha256"] != original["native_instruction_sha256"]):
            raise ValueError("public-default/manual-H2 parity is not established")
        smoke_pins.append({"receipt": receipt_pin, "result": receipt["result"]})
    if not summary_path.is_file():
        raise FileNotFoundError("complete default_initial_v1 quality_summary.json is not ready")
    summary_pin = {"path": str(summary_path), "sha256": sha256(summary_path)}
    summary = evidence.json(summary_pin)
    if (summary.get("protocol_sha256") != expected_protocol_sha256 or summary.get("protocol_id") != PROTOCOL_ID
            or summary.get("canonical_jobs") != len(jobs) or summary.get("display_records") != len(expected)):
        raise ValueError("quality summary is not tied to the complete sealed matrix")
    if not allow_partial and (summary.get("new_jobs_pending") != 0 or summary_path.name != "quality_summary.json"):
        raise ValueError("partial study cannot export paper statistics")
    record_map = {}
    for row in summary.get("records", []):
        identity = (row["dataset"], row["circuit"], row["seed"])
        if identity in record_map:
            raise ValueError("duplicate summary display identity")
        record_map[identity] = row
    if set(record_map) != expected:
        raise ValueError("quality summary has missing or extra display identities")
    observations, verified_jobs, unique_status = [], {}, Counter()
    for identity, (job, label) in sorted(plan_rows.items()):
        row = record_map[identity]
        alias = job["circuit"] if label["circuit"] != job["circuit"] else None
        if ({k: row.get(k) for k in label} != label or row.get("canonical_job_id") != job["job_id"]
                or row.get("origin") != job["origin"] or row.get("alias_of") != alias):
            raise ValueError("summary source identity/alias was relabelled")
        if job["job_id"] not in verified_jobs:
            status = row.get("status")
            if status == "pending":
                if not allow_partial or row.get("receipt") is not None or row.get("result") is not None:
                    raise ValueError("pending result cannot enter final evidence")
                verified_jobs[job["job_id"]] = (row, None)
                unique_status[status] += 1
                continue
            receipt = evidence.json(row["receipt"])
            observation = verify_terminal_receipt(receipt, row, job, protocol, model, evidence,
                                                  expected_protocol_sha256)
            verified_jobs[job["job_id"]] = (row, observation)
            unique_status[status] += 1
        first, observation = verified_jobs[job["job_id"]]
        for key in ("receipt", "result", "status", "quality_classification", "origin"):
            if row.get(key) != first.get(key):
                raise ValueError("aliases do not share the same actual result/receipt")
        if observation is not None:
            observations.append({**observation, "dataset": label["dataset"], "circuit": label["circuit"]})
    if dict(unique_status) != summary.get("unique_status_counts") or unique_status["pending"] != summary.get("new_jobs_pending"):
        raise ValueError("declared completion counts differ from actual terminal receipts")
    amendment_references = summary.get("execution_amendments", [])
    if not isinstance(amendment_references, list):
        raise ValueError("summary execution_amendments must be an explicit pin list")
    amendment_references = list(amendment_references)
    for path in execution_amendments:
        path = Path(path)
        path = (repo / path).resolve() if not path.is_absolute() else path.resolve()
        amendment_references.append({"path": str(path), "sha256": sha256(path)})
    execution_conditions = audit_execution_amendments(amendment_references, protocol, protocol_path, jobs,
                                                      verified_jobs, evidence, allow_partial=allow_partial)
    provenance = {"protocol": {"path": str(protocol_path.relative_to(repo)), "sha256": expected_protocol_sha256},
                  "quality_summary": {"path": str(summary_path.relative_to(repo)), "sha256": summary_pin["sha256"]},
                  "actual_jobs": len(jobs), "display_records": len(expected),
                  "planned_origin_counts": dict(Counter(j["origin"] for j in jobs.values())),
                  "new_main_compilations_planned": sum(j["origin"] == "fresh" for j in jobs.values()),
                  "reused_quality_validations_planned": sum(j["origin"] == "reused" for j in jobs.values()),
                  "additional_parity_compilations": len(smoke_pins),
                  "actual_status_counts": dict(unique_status), "quality_matrix_complete": unique_status["pending"] == 0,
                  "execution_conditions": execution_conditions,
                  "complete": unique_status["pending"] == 0 and execution_conditions["complete"],
                  "recovered_quality_jobs": [{"job_id": job_id, "receipt": row["receipt"], "result": row["result"],
                                               **observation["recovery"]}
                                              for job_id, (row, observation) in sorted(verified_jobs.items())
                                              if observation is not None and "recovery" in observation],
                  "no_recovered_timing_claim": True,
                  "physical_and_logical_reports_checked": True, "same_fixed_model_recomputed_from_counts_and_idle_times": True,
                  "available_native_instructions_and_initial_mappings_hash_checked": True, "parity_smokes": smoke_pins,
                  "reuse_not_new_compilation": True, "no_reused_or_concurrent_timing_speedup_claim": True,
                  "verified_files": list(evidence.pins.values())}
    return observations, provenance


def seed_cell(records: Sequence[Mapping[str, Any]], seeds: Sequence[int]) -> dict:
    indexed = {row["seed"]: validated_record(row) for row in records}
    if len(indexed) != len(records) or set(indexed) != set(seeds):
        raise ValueError("missing, duplicate or extra seed in completed matrix")
    canonical = {row["canonical_sha256"] for row in records}
    if len(canonical) != 1:
        raise ValueError("seed records do not identify the same canonical input")
    success = [indexed[seed] for seed in seeds if indexed[seed]["status"] == "success"]
    f_valid = [row for row in success if row["fidelity_valid"]]
    cell = {"N": len(seeds), "valid": len(success), "fidelity_valid": len(f_valid),
            "complete": len(success) == len(seeds),
            "complete_fidelity": len(f_valid) == len(seeds),
            "seed_status": [{"seed": seed, "status": indexed[seed]["status"]} for seed in seeds]}
    for field in FIELDS:
        selected = f_valid if field == "log_fidelity" or field in COMPONENTS else success
        values = [row["metrics"][field] for row in selected]
        cell[field] = statistics.median(values) if values else None
    cell["fidelity"] = math.exp(cell["log_fidelity"]) if cell["log_fidelity"] is not None else None
    return cell


def build_main_rows(baselines: Sequence[Mapping[str, Any]], defaults: Sequence[Mapping[str, Any]],
                    inventory: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[dict]:
    expected_baselines = {(dataset, circuit, method, 0) for dataset in DATASETS
                          for circuit in inventory[dataset] for method in ("M1", "M2")}
    expected_defaults = {(dataset, circuit, seed) for dataset in DATASETS
                         for circuit in inventory[dataset] for seed in SEEDS}
    base_keys = [(r["dataset"], r["circuit"], r["method"], r["seed"]) for r in baselines]
    new_keys = [(r["dataset"], r["circuit"], r["seed"]) for r in defaults]
    if len(set(base_keys)) != len(base_keys) or set(base_keys) != expected_baselines:
        raise ValueError("baseline matrix identity drift")
    if len(set(new_keys)) != len(new_keys) or set(new_keys) != expected_defaults:
        raise ValueError("default matrix incomplete or contains duplicate/extra identities")
    groups = defaultdict(list)
    for record in [*baselines, *defaults]:
        dataset, circuit = record["dataset"], record["circuit"]
        if record["canonical_sha256"] != inventory[dataset][circuit]["canonical_sha256"]:
            raise ValueError("default/baseline canonical identity mismatch")
        groups[(dataset, circuit, record.get("method", "Default"))].append(record)
    rows = []
    for dataset in DATASETS:
        for circuit, identity in sorted(inventory[dataset].items()):
            row = {"dataset": dataset, "circuit": circuit, **identity}
            for method in METHODS:
                row[method] = seed_cell(groups[(dataset, circuit, method)],
                                        SEEDS if method == "Default" else (0,))
            row["common_fidelity"] = all(row[method]["complete_fidelity"] for method in METHODS)
            rows.append(row)
    return rows


def analysis_units(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Same accepted order: eligibility per file, then canonical alias mean."""
    groups = defaultdict(list)
    for row in rows:
        if row["common_fidelity"]:
            groups[(row["dataset"], row["canonical_sha256"])].append(row)
    result = []
    for (dataset, digest), members in sorted(groups.items()):
        if dataset == "zac18" and len(members) != 1:
            raise ValueError("ZAC18 unexpectedly contains duplicate canonical circuits")
        sizes = {(row["qubits"], row["gates_2q"]) for row in members}
        if len(sizes) != 1:
            raise ValueError("canonical aliases disagree on circuit size")
        unit = {"dataset": dataset, "canonical_sha256": digest,
                "circuits": sorted(row["circuit"] for row in members), "member_N": len(members),
                "qubits": members[0]["qubits"], "gates_2q": members[0]["gates_2q"]}
        for method in METHODS:
            cell = {field: statistics.fmean(row[method][field] for row in members) for field in FIELDS}
            cell["fidelity"] = math.exp(cell["log_fidelity"])
            unit[method] = cell
        result.append(unit)
    return result


def summarize(rows: Sequence[Mapping[str, Any]], units: Sequence[Mapping[str, Any]]) -> dict:
    result = {}
    for dataset in DATASETS:
        file_rows = [row for row in rows if row["dataset"] == dataset]
        selected = [unit for unit in units if unit["dataset"] == dataset]
        if not selected:
            raise ValueError(f"no common fidelity-valid cohort for {dataset}")
        summary = {"input_file_N": len(file_rows), "common_file_N": sum(u["member_N"] for u in selected),
                   "common_canonical_N": len(selected), "methods": {}, "comparisons": {}}
        for method in METHODS:
            summary["methods"][method] = {
                "log_fidelity_mean": statistics.fmean(u[method]["log_fidelity"] for u in selected),
                "fidelity_geometric_mean": math.exp(statistics.fmean(u[method]["log_fidelity"] for u in selected)),
                "move_batches_arithmetic_mean": statistics.fmean(u[method]["move_batches"] for u in selected),
                "move_time_us_arithmetic_mean": statistics.fmean(u[method]["move_time_us"] for u in selected),
                "complete_files": sum(row[method]["complete"] for row in file_rows),
                "fidelity_valid_files": sum(row[method]["complete_fidelity"] for row in file_rows),
            }
        current = summary["methods"]["Default"]
        for baseline in ("M1", "M2"):
            reference = summary["methods"][baseline]
            delta = [u["Default"]["log_fidelity"] - u[baseline]["log_fidelity"] for u in selected]
            reference_b = reference["move_batches_arithmetic_mean"]
            summary["comparisons"][baseline] = {
                "fidelity_gain_percent": 100 * math.expm1(statistics.fmean(delta)),
                "mean_batch_reduction_percent": 100 * (1 - current["move_batches_arithmetic_mean"] / reference_b)
                    if reference_b else None,
                "wins": sum(d > 1e-12 for d in delta), "ties": sum(abs(d) <= 1e-12 for d in delta),
                "losses": sum(d < -1e-12 for d in delta),
            }
        summary["best_methods"] = {}
        for field, maximize in (("fidelity_geometric_mean", True), ("move_batches_arithmetic_mean", False),
                                ("move_time_us_arithmetic_mean", False)):
            comparison_field = "log_fidelity_mean" if field == "fidelity_geometric_mean" else field
            best = (max if maximize else min)(cell[comparison_field] for cell in summary["methods"].values())
            summary["best_methods"][field] = [method for method in METHODS
                                             if math.isclose(summary["methods"][method][comparison_field], best, rel_tol=1e-12, abs_tol=1e-15)]
        result[dataset] = summary
    return result


def representative_cases(units: Sequence[Mapping[str, Any]]) -> list[dict]:
    selected = []
    for dataset in DATASETS:
        records = []
        for unit in units:
            if unit["dataset"] != dataset:
                continue
            baseline = max(("M1", "M2"), key=lambda method: unit[method]["log_fidelity"])
            records.append({**unit, "baseline_method": baseline,
                            "delta_log_fidelity": unit["Default"]["log_fidelity"] - unit[baseline]["log_fidelity"]})
        records.sort(key=lambda row: (row["delta_log_fidelity"], row["canonical_sha256"], row["circuits"][0]))
        if not records:
            raise ValueError("representative selection has an empty cohort")
        median = records[math.ceil(len(records) / 2) - 1]["canonical_sha256"]
        eligible = [row for row in records if row[row["baseline_method"]]["fidelity"] >= .05
                    and row["delta_log_fidelity"] > 0 and row["canonical_sha256"] != median]
        eligible.sort(key=lambda row: (-row["delta_log_fidelity"], row["canonical_sha256"], row["circuits"][0]))
        # Do not invent additional favorable examples if the prespecified
        # eligibility rule yields fewer than two. This requires author review.
        selected.extend(eligible[:2])
    return selected


def mechanism_rows(units: Sequence[Mapping[str, Any]]) -> list[dict]:
    rows = []
    for unit in units:
        for baseline in ("M1", "M2"):
            row = {"dataset": unit["dataset"], "canonical_sha256": unit["canonical_sha256"],
                   "circuits": unit["circuits"], "baseline_method": baseline}
            for field in ("transfers", "idle_exposures", *COMPONENTS):
                row[f"baseline_{field}"] = unit[baseline][field]
                row[f"default_{field}"] = unit["Default"][field]
                row[f"delta_{field}"] = unit["Default"][field] - unit[baseline][field]
            rows.append(row)
    return rows


def numeric_macros(datasets: Mapping[str, Any], mechanisms: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    macros = {}
    for dataset, prefix in (("zac18", "DefaultZAC"), ("qmap154", "DefaultQMAP")):
        summary = datasets[dataset]
        macros[prefix + "StrictN"] = str(summary["common_canonical_N"])
        macros[prefix + "StrictFileN"] = str(summary["common_file_N"])
        for method, suffix in (("M1", "MOne"), ("M2", "MTwo"), ("Default", "MFour")):
            cell = summary["methods"][method]
            macros[prefix + suffix + "F"] = f"{cell['fidelity_geometric_mean']:.6e}"
            macros[prefix + suffix + "B"] = f"{cell['move_batches_arithmetic_mean']:.2f}"
            macros[prefix + suffix + "T"] = f"{cell['move_time_us_arithmetic_mean'] / 1000:.3f}"
            macros[prefix + suffix + "V"] = f"{cell['complete_files']}/{summary['input_file_N']}"
        for baseline, suffix in (("M1", "ZAC"), ("M2", "ICCAD")):
            comparison = summary["comparisons"][baseline]
            macros[prefix + "Vs" + suffix + "FidelityGain"] = f"{comparison['fidelity_gain_percent']:.2f}"
            value = comparison["mean_batch_reduction_percent"]
            macros[prefix + "Vs" + suffix + "BatchReduction"] = f"{value:.2f}" if value is not None else r"\textemdash{}"
    comparisons = [cell for summary in datasets.values() for cell in summary["comparisons"].values()]
    macros["DefaultMaxDatasetFidelityGain"] = f"{max(cell['fidelity_gain_percent'] for cell in comparisons):.2f}"
    batch = [cell["mean_batch_reduction_percent"] for cell in comparisons if cell["mean_batch_reduction_percent"] is not None]
    macros["DefaultMaxDatasetBatchReduction"] = f"{max(batch):.2f}" if batch else r"\textemdash{}"
    qft = [row for row in mechanisms if row["dataset"] == "zac18"
           and row["circuits"] == ["qft_n18_transpiled"] and row["baseline_method"] == "M1"]
    if len(qft) > 1:
        raise ValueError("duplicate QFT-18 mechanism record")
    if qft:
        row = qft[0]
        macros["DefaultMechanismQFTBaseTransfers"] = f"{row['baseline_transfers']:g}"
        macros["DefaultMechanismQFTGATransfers"] = f"{row['default_transfers']:g}"
        for suffix, component in zip(("Transfer", "Excitation", "Coherence"), COMPONENTS):
            value = row["delta_" + component]
            macros["DefaultMechanismQFT" + suffix + "Gain"] = f"{value:.15g}"
            macros["DefaultMechanismQFT" + suffix + "GainRounded"] = f"{value:.4f}"
    return macros


def csv_text(rows: Sequence[Mapping[str, Any]]) -> str:
    keys = list(dict.fromkeys(key for row in rows for key in row))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=keys, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
                         if isinstance(value, (list, dict)) else value for key, value in row.items()})
    return stream.getvalue()


def render_exports(baselines, defaults, inventory, provenance):
    rows = build_main_rows(baselines, defaults, inventory)
    units = analysis_units(rows)
    datasets = summarize(rows, units)
    cases = representative_cases(units)
    mechanisms = mechanism_rows(units)
    macros = numeric_macros(datasets, mechanisms)
    attention = []
    if len(cases) != 4:
        attention.append("fewer_than_four_eligible_representative_cases")
    if "DefaultMechanismQFTBaseTransfers" not in macros:
        attention.append("QFT18_not_in_default_common_valid_cohort")
    comparisons = [{"dataset": dataset, "baseline_method": method, **comparison}
                   for dataset, summary in datasets.items() for method, comparison in summary["comparisons"].items()]
    maxima = {"fidelity": max(comparisons, key=lambda row: row["fidelity_gain_percent"]),
              "mean_batch_reduction": max((row for row in comparisons if row["mean_batch_reduction_percent"] is not None),
                                          key=lambda row: row["mean_batch_reduction_percent"], default=None)}
    if maxima["fidelity"]["fidelity_gain_percent"] <= 0:
        attention.append("no_dataset_mean_fidelity_improvement_do_not_claim_improvement")
    underflow = [{"dataset": row["dataset"], "circuit": row["circuit"], "seed": row["seed"],
                  "method": row.get("method", "Default"), "canonical_sha256": row["canonical_sha256"]}
                 for row in [*baselines, *defaults] if validated_record(row)["fidelity_underflow"]]
    metadata = {"export_schema": EXPORT_SCHEMA, "provenance": provenance,
                "publication_status": "independently_verified_complete_amended_quality_matrix",
                "aggregation": {"seeds": list(SEEDS), "within_file": "per_field_median",
                                "qmap_aliases": "mean_fields_after_file_eligibility",
                                "common_cohort": "M1/M2/Default_complete_and_fidelity_valid",
                                "fidelity": "exp_mean_log_fidelity", "physical_costs": "arithmetic_mean",
                                "baseline_seeds": [0], "coverage": "all_input_files",
                                "exp_underflow_policy": "retain_finite_logF_non_OOD; never divide displayed_zero_fidelities"},
                "fidelity_underflow_display_records": underflow,
                "unchanged_old_studies": {"dynamic_ablation_initializer": "original_SA",
                                          "strict_timing_initializer": "original_SA",
                                          "not_results_of_new_default": True},
                "representative_policy": {"per_dataset": 2, "minimum_best_baseline_fidelity": .05,
                                          "order": "descending_delta_logF_then_canonical",
                                          "nearest_rank_median_excluded": True},
                "mechanism_semantics": "differences of per-field medians, not a single-run additive identity",
                "datasets": datasets, "abstract_maximum_comparisons": maxima,
                "representative_cases": cases, "macros": macros,
                "author_attention": attention}
    tex = "% Generated independently for default_initial_v1; old paper values are unchanged.\n"
    tex += "".join(rf"\newcommand{{\{key}}}{{{value}}}" + "\n" for key, value in macros.items())
    table = "% Same prespecified high-gain rule, evaluated on the new common cohort.\n"
    table += r"\newcommand{\DefaultRepresentativeCircuitRows}{%" + "\n"
    for row in cases:
        name = row["circuits"][0].removesuffix("_transpiled").replace("_", r"\_")
        values = {method: f"{row[method]['fidelity']:.4f}" for method in METHODS}
        best = max(METHODS, key=lambda method: row[method]["fidelity"])
        values[best] = r"\textbf{" + values[best] + "}"
        table += (f"{'ZAC18' if row['dataset'] == 'zac18' else 'QMAP154'} & \\texttt{{{name}}} & "
                  f"{row['qubits']}/{row['gates_2q']} & {values['M1']} & {values['M2']} & {values['Default']} \\\\%\n")
    table += "}\n"
    return {"default_initial_values.json": canonical_json(metadata), "default_initial_values.tex": tex,
            "main_rows.csv": csv_text(rows), "analysis_units.csv": csv_text(units),
            "mechanism.csv": csv_text(mechanisms), "representative_cases.tex": table}


def export_completed(*, repo=REPO, summary_path=None, allow_partial=False, execution_amendments=()):
    baselines, inventory, baseline_provenance = load_accepted_baselines(repo)
    defaults, new_provenance = load_default_results(inventory, baseline_provenance, repo=repo,
                                                   summary_path=summary_path, allow_partial=allow_partial,
                                                   execution_amendments=execution_amendments)
    provenance = {"baselines": baseline_provenance, "new_default": new_provenance,
                  "generator": {"path": str(Path(__file__).relative_to(REPO)), "sha256": sha256(Path(__file__))}}
    if allow_partial:
        return None, {"status": "verified_available_evidence", "complete": new_provenance["complete"],
                      "statistics_exported": False, "actual_status_counts": new_provenance["actual_status_counts"],
                      "execution_conditions": new_provenance.get("execution_conditions"),
                      "verified_default_display_records": len(defaults),
                      "verified_baseline_records": len(baselines), "provenance": provenance}
    if not new_provenance["complete"]:
        raise ValueError("partial matrix cannot export paper statistics")
    return render_exports(baselines, defaults, inventory, provenance), provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--summary", type=Path, help="Final quality_summary.json; partial summaries only with --audit-only")
    parser.add_argument("--audit-only", action="store_true", help="Verify available evidence without exporting any statistic")
    parser.add_argument("--execution-amendment", type=Path, action="append", default=[],
                        help="Explicit amendment.json path; repeat for multiple execution changes. Required known changes must be declared here or by summary pins.")
    parser.add_argument("--output-root", type=Path,
                        default=PAPER / "build/default_initial_v1/paper_exports")
    parser.add_argument("--check", action="store_true", help="Read-only comparison with previously generated exports")
    args = parser.parse_args(argv)
    outputs, provenance = export_completed(summary_path=args.summary, allow_partial=args.audit_only,
                                          execution_amendments=args.execution_amendment)
    if args.audit_only:
        compact = {key: value for key, value in provenance.items() if key != "provenance"}
        print(canonical_json(compact), end="")
        return 0
    destination = args.output_root.resolve()
    allowed = (PAPER / "build/default_initial_v1", REPO / "ZAC_zzx/results/default_initial_v1")
    if not any(destination.is_relative_to(parent.resolve()) and destination != parent.resolve() for parent in allowed):
        raise ValueError("exports must use a new subdirectory beneath default_initial_v1 build/results")
    differences = [name for name, content in outputs.items() if not (destination / name).is_file()
                   or (destination / name).read_text(encoding="utf-8") != content]
    if args.check:
        print(canonical_json({"status": "fail" if differences else "pass", "read_only": True,
                              "different_exports": differences}), end="")
        return bool(differences)
    # Immutable publication: a different previous export is not overwritten.
    conflicts = [name for name in differences if (destination / name).exists()]
    if conflicts:
        raise FileExistsError("use a new versioned export directory: " + ", ".join(conflicts))
    destination.mkdir(parents=True, exist_ok=True)
    for name in differences:
        with (destination / name).open("x", encoding="utf-8") as stream:
            stream.write(outputs[name])
    print(canonical_json({"status": "pass", "output_root": str(destination), "files": {
        name: sha256(destination / name) for name in outputs}}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
