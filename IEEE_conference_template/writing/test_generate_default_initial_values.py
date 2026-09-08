"""Narrow, synthetic tests; never start a compiler or touch accepted results."""
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("default_initial_values", HERE / "generate_default_initial_values.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def record(dataset="zac18", circuit="qft_n18_transpiled", *, seed=0, method=None,
           digest="a" * 64, log_f=-1., batches=10, status="success", ood=False):
    row = {"dataset": dataset, "circuit": circuit, "canonical_sha256": digest,
           "seed": seed, "status": status, "fidelity_ood": ood,
           "metrics": {"log_fidelity": None if ood else log_f,
                       "fidelity": None if ood else math.exp(log_f),
                       "move_batches": batches, "move_time_us": batches * 50,
                       "transfers": 4, "idle_exposures": 0,
                       "log_atom_transfer": -.004,
                       "log_idle_excitation": 0., "log_coherence_linear": -.1}}
    if method is not None:
        row["method"] = method
    if status != "success":
        row["metrics"] = {}
    return row


def fixture():
    inventory = {"zac18": {}, "qmap154": {}}
    baselines, defaults = [], []
    for d, dataset in enumerate(module.DATASETS):
        for i in range(4):
            name = "qft_n18_transpiled" if d == 0 and i == 0 else f"circuit_{d}_{i}"
            digest = f"{d * 4 + i + 1:064x}"
            inventory[dataset][name] = {"canonical_sha256": digest, "qubits": 18, "gates_2q": 306}
            for method in ("M1", "M2"):
                baselines.append(record(dataset, name, method=method, digest=digest, log_f=-1.))
            for seed in module.SEEDS:
                defaults.append(record(dataset, name, seed=seed, digest=digest,
                                       log_f=-1 + (i + 1) / 10, batches=8))
    return baselines, defaults, inventory


class AggregateTests(unittest.TestCase):
    def test_seed_median_is_not_mean_fidelity(self):
        rows = [record(seed=s, log_f=v, batches=b) for s, v, b in ((0, -4., 10), (1, -2., 2), (2, -1., 9))]
        cell = module.seed_cell(rows, (0, 1, 2))
        self.assertEqual(cell["log_fidelity"], -2.)
        self.assertEqual(cell["fidelity"], math.exp(-2.))
        self.assertEqual(cell["move_batches"], 9)
        self.assertNotAlmostEqual(cell["fidelity"], sum(r["metrics"]["fidelity"] for r in rows) / 3)

    def test_all_seeds_must_exist_once_even_when_failed(self):
        rows = [record(seed=s) for s in module.SEEDS]
        for broken in (rows[:2], [*rows, rows[0]], [rows[0], rows[0], rows[2]]):
            with self.subTest(count=len(broken)), self.assertRaises(ValueError):
                module.seed_cell(broken, module.SEEDS)

    def test_one_ood_seed_excludes_entire_file_not_coverage(self):
        base, default, inventory = fixture()
        first = default[0]
        default[0] = record(first["dataset"], first["circuit"], seed=0,
                            digest=first["canonical_sha256"], ood=True)
        rows = module.build_main_rows(base, default, inventory)
        affected = next(row for row in rows if row["circuit"] == first["circuit"])
        self.assertTrue(affected["Default"]["complete"])
        self.assertFalse(affected["common_fidelity"])
        self.assertEqual(affected["Default"]["fidelity_valid"], 2)
        result = module.summarize(rows, module.analysis_units(rows))["zac18"]
        self.assertEqual(result["common_canonical_N"], 3)
        self.assertEqual(result["methods"]["Default"]["complete_files"], 4)

    def test_timeout_remains_missing_not_zero(self):
        rows = [record(seed=0), record(seed=1), record(seed=2, status="timeout")]
        cell = module.seed_cell(rows, module.SEEDS)
        self.assertFalse(cell["complete"])
        self.assertEqual(cell["valid"], 2)
        self.assertEqual(cell["move_batches"], 10)
        self.assertFalse(cell["complete_fidelity"])

    def test_recomputes_baseline_means_on_new_common_domain(self):
        base, default, inventory = fixture()
        target = default[0]
        for row in base:
            if row["dataset"] == target["dataset"] and row["circuit"] == target["circuit"]:
                row["metrics"]["log_fidelity"] = -10
                row["metrics"]["fidelity"] = math.exp(-10)
        default[0] = record(target["dataset"], target["circuit"], seed=0,
                            digest=target["canonical_sha256"], status="timeout")
        rows = module.build_main_rows(base, default, inventory)
        summary = module.summarize(rows, module.analysis_units(rows))["zac18"]
        self.assertAlmostEqual(summary["methods"]["M1"]["fidelity_geometric_mean"], math.exp(-1))

    def test_qmap_aliases_are_meaned_then_counted_once(self):
        base, default, inventory = fixture()
        source = next(iter(inventory["qmap154"]))
        inventory["qmap154"]["alias"] = deepcopy(inventory["qmap154"][source])
        for rows in (base, default):
            rows.extend([{**deepcopy(row), "circuit": "alias"} for row in list(rows)
                         if row["dataset"] == "qmap154" and row["circuit"] == source])
        for row in default:
            if row["circuit"] == "alias":
                row["metrics"]["log_fidelity"] = -.5
                row["metrics"]["fidelity"] = math.exp(-.5)
        rows = module.build_main_rows(base, default, inventory)
        units = module.analysis_units(rows)
        unit = next(unit for unit in units if "alias" in unit["circuits"])
        self.assertEqual(unit["member_N"], 2)
        self.assertAlmostEqual(unit["Default"]["log_fidelity"], (-.9 - .5) / 2)
        summary = module.summarize(rows, units)["qmap154"]
        self.assertEqual(summary["common_file_N"], 5)
        self.assertEqual(summary["common_canonical_N"], 4)

    def test_matrix_rejects_missing_duplicate_and_input_drift(self):
        base, default, inventory = fixture()
        for corrupted in (default[:-1], [*default, default[0]]):
            with self.assertRaises(ValueError):
                module.build_main_rows(base, corrupted, inventory)
        bad = deepcopy(default)
        bad[0]["canonical_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "canonical"):
            module.build_main_rows(base, bad, inventory)

    def test_all_results_including_losses_remain_in_aggregate(self):
        base, default, inventory = fixture()
        for row in default:
            row["metrics"]["log_fidelity"] = -2.
            row["metrics"]["fidelity"] = math.exp(-2.)
            row["metrics"]["move_batches"] = 11
        outputs = module.render_exports(base, default, inventory, {})
        data = json.loads(outputs["default_initial_values.json"])
        comparison = data["datasets"]["qmap154"]["comparisons"]["M1"]
        self.assertEqual(comparison["losses"], 4)
        self.assertLess(comparison["fidelity_gain_percent"], 0)
        self.assertLess(comparison["mean_batch_reduction_percent"], 0)
        self.assertIn("fewer_than_four_eligible_representative_cases", data["author_attention"])
        self.assertEqual(data["representative_cases"], [])

    def test_no_old_study_is_relabelled_or_exported_as_new_default(self):
        base, default, inventory = fixture()
        outputs = module.render_exports(base, default, inventory, {"new_study": "fixture"})
        data = json.loads(outputs["default_initial_values.json"])
        self.assertTrue(data["unchanged_old_studies"]["not_results_of_new_default"])
        self.assertTrue(all(name.startswith("Default") for name in data["macros"]))
        self.assertNotIn("StrictMFourTime", outputs["default_initial_values.tex"])
        self.assertNotIn("AblationHRatio", outputs["default_initial_values.tex"])
        self.assertNotIn("results_values_zh.tex", outputs)

    def test_export_is_deterministic_under_input_permutation(self):
        base, default, inventory = fixture()
        expected = module.render_exports(base, default, inventory, {})
        self.assertEqual(expected, module.render_exports(list(reversed(base)), list(reversed(default)), inventory, {}))

    def test_qft_mechanism_is_explicit_zac_pair(self):
        base, default, inventory = fixture()
        for row in default:
            if row["circuit"] == "qft_n18_transpiled":
                row["metrics"]["transfers"] = 2
                row["metrics"]["log_atom_transfer"] = -.002
        outputs = module.render_exports(base, default, inventory, {})
        data = json.loads(outputs["default_initial_values.json"])
        self.assertEqual(data["macros"]["DefaultMechanismQFTBaseTransfers"], "4")
        self.assertEqual(data["macros"]["DefaultMechanismQFTGATransfers"], "2")
        self.assertEqual(data["macros"]["DefaultMechanismQFTTransferGain"], "0.002")

    def test_record_rejects_nonfinite_forged_ood_or_failed_metrics(self):
        invalid = []
        row = record(); row["metrics"]["log_fidelity"] = float("nan"); invalid.append(row)
        row = record(); row["fidelity_ood"] = True; invalid.append(row)
        row = record(); row["metrics"]["fidelity"] = .9; invalid.append(row)
        row = record(); row["status"] = "timeout"; invalid.append(row)
        row = record(); row["status"] = "running"; invalid.append(row)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                module.validated_record(row)

    def test_sha_reference_requires_content_match_and_repository_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "record.json"
            target.write_text('{"status":"success"}')
            pin = {"path": "record.json", "sha256": module.sha256(target)}
            data, verified = module.verified_json(pin, root)
            self.assertEqual(data["status"], "success")
            self.assertEqual(verified, pin)
            target.write_text('{"status":"failure"}')
            with self.assertRaisesRegex(ValueError, "drift"):
                module.verified_json(pin, root)
            with self.assertRaises(ValueError):
                module.verified_json({"path": str(HERE / "generate_default_initial_values.py"),
                                      "sha256": module.sha256(HERE / "generate_default_initial_values.py")}, root)

    def test_baseline_native_ghost_diagnostic_does_not_change_original_domain(self):
        raw = {"dataset": "zac18", "circuit": "qft_n18_transpiled", "method": "M1",
               "seed": 0, "repetition": 0, "input_sha256": "a" * 64, "status": "success",
               "expected_gate_ledger_sha256": "b" * 64, "observed_gate_ledger_sha256": "b" * 64,
               "verifier_ok": True, "ghost_hits": 3, "fidelity_ood": False,
               "log_fidelity": -1., "fidelity": math.exp(-1.), "transfers": None,
               "idle_exposures": 0, "move_batches": 10, "move_time_us": 500,
               "fidelity_components": {"transfers": 4, "log_atom_transfer": -.004,
                                       "log_idle_excitation": 0., "log_coherence_linear": -.1}}
        row = module.baseline_observation(raw, "zac18", "qft_n18_transpiled", "M1", "a" * 64)
        self.assertTrue(row["fidelity_valid"])
        self.assertEqual(row["metrics"]["transfers"], 4)
        raw["observed_gate_ledger_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "logical"):
            module.baseline_observation(raw, "zac18", "qft_n18_transpiled", "M1", "a" * 64)

    def test_baseline_loader_follows_pinned_raw_records_not_old_means(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def write(name, value):
                path = root / name
                path.write_text(module.canonical_json(value))
                return {"path": name, "sha256": module.sha256(path)}
            suites = {dataset: ["one"] for dataset in module.DATASETS}
            freeze = {"freeze_id": "fixture", "canonical_suites": {
                dataset: {"canonical_inputs": {"one": "a" * 64}} for dataset in module.DATASETS},
                "model": {"sha256": "d" * 64}, "architecture": {"sha256": "e" * 64}}
            source = {"freeze_id": "fixture", "baselines": {method: {} for method in ("M1", "M2")}}
            for dataset in module.DATASETS:
                for method in ("M1", "M2"):
                    raw = {"dataset": dataset, "circuit": "one", "method": method, "seed": 0, "repetition": 0,
                           "input_sha256": "a" * 64, "status": "success", "qubits": 2,
                           "expected_gates_1q": 0, "expected_gates_2q": 1,
                           "expected_gate_ledger_sha256": "b" * 64, "observed_gate_ledger_sha256": "b" * 64,
                           "verifier_ok": True, "fidelity_ood": False, "log_fidelity": -1., "fidelity": math.exp(-1.),
                           "transfers": 4, "idle_exposures": 0, "move_batches": 2, "move_time_us": 50,
                           "fidelity_components": {"log_atom_transfer": -.004, "log_idle_excitation": 0.,
                                                   "log_coherence_linear": -.1}}
                    source["baselines"][method][f"{dataset}/one"] = {
                        **write(f"{dataset}-{method}.json", raw), "status": "success"}
            final = {"protocol": "paper-zh-v2-final-manifest-v1", "datasets": suites,
                     "quality_source_manifest": write("source.json", source),
                     "paper_freeze": {**write("freeze.json", freeze), "freeze_id": "fixture"}}
            final_pin = write("final.json", final)
            rows, inventory, provenance = module.load_accepted_baselines(root, accepted_reference=final_pin)
            self.assertEqual(len(rows), 4)
            self.assertEqual(inventory["zac18"]["one"]["qubits"], 2)
            self.assertEqual(len(provenance["baseline_records"]), 4)
            (root / "zac18-M1.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "drift"):
                module.load_accepted_baselines(root, accepted_reference=final_pin)

    def test_finite_log_fidelity_underflow_is_retained_not_treated_as_ood(self):
        cell = module.seed_cell([record(seed=s, log_f=-1000.) for s in module.SEEDS], module.SEEDS)
        self.assertEqual(cell["fidelity"], 0.)
        self.assertTrue(cell["complete_fidelity"])
        self.assertEqual(cell["log_fidelity"], -1000.)
        self.assertTrue(module.validated_record(record(log_f=-1000.))["fidelity_underflow"])

    def test_memory_limit_is_terminal_and_cannot_supply_metrics(self):
        rows = [record(seed=0), record(seed=1), record(seed=2, status="memory_limit")]
        self.assertFalse(module.seed_cell(rows, module.SEEDS)["complete"])

    def test_underflow_keeps_log_domain_comparison_and_best_method(self):
        base, default, inventory = fixture()
        for row in base:
            row["metrics"]["log_fidelity"] = -1001.
            row["metrics"]["fidelity"] = 0.
        for row in default:
            row["metrics"]["log_fidelity"] = -1000.
            row["metrics"]["fidelity"] = 0.
        rows = module.build_main_rows(base, default, inventory)
        summary = module.summarize(rows, module.analysis_units(rows))["zac18"]
        self.assertEqual(summary["best_methods"]["fidelity_geometric_mean"], ["Default"])
        self.assertAlmostEqual(summary["comparisons"]["M1"]["fidelity_gain_percent"], 100 * math.expm1(1))


def score_fixture(*, ood=False):
    model = {"coherence_time_us": 1500000., "transfer_fidelity": .999,
             "idle_excitation_fidelity": .9975, "one_qubit_fidelity": .9997, "two_qubit_fidelity": .995}
    job = {"dataset": "zac18", "circuit": "one", "canonical_sha256": "a" * 64,
           "seed": 0, "job_id": "a-s0", "qubits": 2, "gates_1q": 1, "gates_2q": 1}
    logs = {"one_qubit_gate": math.log(.9997), "two_qubit_gate": math.log(.995),
            "atom_transfer": 4 * math.log(.999), "idle_excitation": 0.,
            "coherence_linear": None if ood else 2 * math.log(1 - 100 / 1500000)}
    total = None if ood else math.fsum(logs.values())
    score = {"components": {name: {"log_fidelity": value} for name, value in logs.items()},
             "fidelity": None if ood else math.exp(total), "log_fidelity": total,
             "counts": {"one_qubit_gates": 1, "two_qubit_gates": 1, "transfers": 4, "idle_excitations": 0},
             "idle_time_us": [1500000. if ood else 100., 100.], "move_batches": 2, "move_time_us": 50.,
             "ood": ood, "warnings": ["linear coherence model out of domain for atoms: 0"] if ood else []}
    result = {"status": "success", "input_sha256": "a" * 64, "seed": 0, "n_qubits": 2, "canonical_job_id": "a-s0",
              "validation": {"ok": True, "ghost_hits": 0, "move_batches": 2},
              "logical_validation": {"ok": True, "expected_gate_ledger_sha256": "b" * 64,
                                     "observed_gate_ledger_sha256": "b" * 64,
                                     "compiled_layer_ledger_sha256": "c" * 64,
                                     "observed_layer_ledger_sha256": "c" * 64}, "score": score}
    return result, job, model


class ModelEvidenceTests(unittest.TestCase):
    def test_same_model_recomputation_accepts_valid_score(self):
        result, job, model = score_fixture()
        row = module.validate_default_score(result, job, model)
        self.assertTrue(row["fidelity_valid"])

    def test_legitimate_ood_keeps_null_and_physical_success(self):
        result, job, model = score_fixture(ood=True)
        row = module.validate_default_score(result, job, model)
        self.assertEqual(row["status"], "success")
        self.assertIsNone(row["metrics"]["fidelity"])
        self.assertFalse(row["fidelity_valid"])

    def test_ood_cannot_be_invented_to_remove_an_unfavorable_valid_result(self):
        result, job, model = score_fixture(ood=True)
        result["score"]["idle_time_us"] = [100, 100]
        with self.assertRaisesRegex(ValueError, "OOD"):
            module.validate_default_score(result, job, model)

    def test_physics_gate_order_and_layer_checks_are_fail_closed(self):
        for field in ("physics", "gate", "layer"):
            result, job, model = score_fixture()
            if field == "physics":
                result["validation"]["ghost_hits"] = 1
            elif field == "gate":
                result["logical_validation"]["observed_gate_ledger_sha256"] = "f" * 64
            else:
                result["logical_validation"]["observed_layer_ledger_sha256"] = "f" * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.validate_default_score(result, job, model)

    def test_component_or_gate_count_drift_is_rejected(self):
        result, job, model = score_fixture()
        result["score"]["components"]["atom_transfer"]["log_fidelity"] = -.0001
        with self.assertRaisesRegex(ValueError, "component"):
            module.validate_default_score(result, job, model)
        result, job, model = score_fixture()
        result["score"]["counts"]["two_qubit_gates"] = 2
        with self.assertRaisesRegex(ValueError, "gate counts"):
            module.validate_default_score(result, job, model)

    def test_legacy_canonical_layer_diagnostic_is_not_an_extra_filter(self):
        result, job, model = score_fixture()
        result["logical_validation"]["canonical_and_compiled_layers_equal"] = False
        self.assertTrue(module.validate_default_score(result, job, model)["fidelity_valid"])

    def test_score_equivalence_keeps_null_semantics_and_checks_all_components(self):
        result, _, _ = score_fixture(ood=True)
        self.assertTrue(module.same_score(result["score"], deepcopy(result["score"])))
        changed = deepcopy(result["score"])
        changed["fidelity"] = 0.
        self.assertFalse(module.same_score(result["score"], changed))


def wrapper_fixture(*, reused=False, ood=False):
    result, job, model = score_fixture(ood=ood)
    native = {"native_abi_version": 9, "native_wheel_sha256": "d" * 64,
              "extension_sha256": "e" * 64, "version": "1", "rng_version": 1,
              "rich_boundary_wire_version": 1, "wheel_registered": True}
    setting = {"name": "public", "dir": "new", "seed": 0, "schema_version": 2,
               "init_strategy": "physical_prefix", "resident_search": "ga"}
    protocol = {"native": native, "architecture": {"sha256": "f" * 64}, "dynamic_setting": setting}
    trace = {"instructions": [{"type": "init", "init_locs": [[0, 0, 0, 0], [1, 0, 0, 1]]},
                               {"type": "1qGate", "gates": [{"name": "u2", "q": 0}]},
                               {"type": "rydberg", "gates": [{"q0": 0, "q1": 1}]}]}
    mapping = module.stable_hash([[0, 0, 0], [0, 0, 1]])
    selection = {"policy_id": "physical-prefix-initial-v1", "config": {**module.FIXED_INITIALIZATION, "seed": 0},
                 "selected_mapping_sha256": mapping}
    origin = "reused" if reused else "fresh"
    job["origin"] = origin
    result.update(origin=origin, native=native, selection=selection, selected_mapping_sha256=mapping,
                  native_instruction_sha256=module.stable_hash(trace["instructions"]),
                  quality_classification="model_out_of_domain" if ood else "valid_fidelity")
    evidence = mock.Mock()
    payloads = {"native_trace": trace}
    pin = lambda name: {"path": name, "sha256": "1" * 64}
    result["native_trace"] = pin("native_trace")
    if reused:
        names = {"phase_protocol", "phase_summary", "receipt", "protocol", "result", "native_trace",
                 "selection", "choices", "base_mapping", "summary"}
        pins = {name: pin(name) for name in names}
        job["reuse"] = {"evidence": pins}
        result["source_evidence"] = deepcopy(pins)
        payloads.update({"result": {**deepcopy(result), "variant": "h2", "horizon": 2},
                         "protocol": {"input_sha256": job["canonical_sha256"], "dynamic_setting": setting,
                                      "architecture_sha256": "f" * 64, "native": native},
                         "selection": selection, "summary": {"source_stable": True},
                         "receipt": {"status": "success"},
                         "choices": {"selection_uses_full_compile_results": False,
                                     "choices": [{"variant": "h2", "mapping_sha256": mapping}]}})
    else:
        result.update(end_to_end_ns=100, end_to_end_cpu_ns=100)
    evidence.json.side_effect = lambda reference: payloads[reference["path"]]
    evidence.trace.side_effect = lambda reference: payloads[reference["path"]]
    return result, job, protocol, model, evidence, payloads


class WrapperEvidenceTests(unittest.TestCase):
    def verify(self, data):
        return module.verify_default_result(*data[:5])

    def test_fresh_and_reused_valid_and_ood_remain_distinct(self):
        for reused in (False, True):
            for ood in (False, True):
                with self.subTest(reused=reused, ood=ood):
                    row = self.verify(wrapper_fixture(reused=reused, ood=ood))
                    self.assertEqual(row["fidelity_valid"], not ood)
                    self.assertEqual(row["origin"], "reused" if reused else "fresh")

    def test_native_origin_or_initializer_tampering_is_rejected(self):
        changes = [("origin", "reused"), ("native", {"native_abi_version": 8}),
                   ("selection", {"policy_id": "legacy"}), ("native_instruction_sha256", "0" * 64)]
        for field, value in changes:
            data = wrapper_fixture(); data[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(data)

    def test_missing_or_duplicate_init_and_wrong_executed_mapping_are_rejected(self):
        for variant in ("missing", "duplicate", "wrong_mapping"):
            data = wrapper_fixture()
            instructions = data[-1]["native_trace"]["instructions"]
            if variant == "missing":
                instructions.pop(0)
            elif variant == "duplicate":
                instructions.append(deepcopy(instructions[0]))
            else:
                instructions[0]["init_locs"][0][-1] = 5
            data[0]["native_instruction_sha256"] = module.stable_hash(instructions)
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                self.verify(data)

    def test_reused_runtime_architecture_and_dynamic_controls_are_not_silently_changed(self):
        for field in ("architecture", "native", "dynamic", "selection"):
            data = wrapper_fixture(reused=True)
            # Avoid fixture object sharing so only the historical evidence drifts.
            old = deepcopy(data[-1]["protocol"]); data[-1]["protocol"] = old
            if field == "architecture":
                old["architecture_sha256"] = "0" * 64
            elif field == "native":
                old["native"]["rng_version"] = 2
            elif field == "dynamic":
                old["dynamic_setting"]["resident_search"] = "greedy"
            else:
                data[-1]["choices"]["selection_uses_full_compile_results"] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(data)

    def test_reused_evidence_cannot_be_missing_or_timing_relabelled(self):
        for field in ("source_evidence", "end_to_end_ns", "score"):
            data = wrapper_fixture(reused=True)
            if field == "source_evidence":
                data[0][field].pop("choices")
            elif field == "end_to_end_ns":
                data[0][field] = 100
            else:
                data[-1]["result"]["score"]["move_time_us"] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(data)

    def test_partial_audit_never_calls_statistical_renderer(self):
        with mock.patch.object(module, "load_accepted_baselines", return_value=([], {}, {})), \
             mock.patch.object(module, "load_default_results", return_value=([], {"complete": False, "actual_status_counts": {"pending": 1}})), \
             mock.patch.object(module, "render_exports") as renderer:
            outputs, report = module.export_completed(allow_partial=True)
            self.assertIsNone(outputs)
            self.assertFalse(report["statistics_exported"])
            renderer.assert_not_called()
            with self.assertRaisesRegex(ValueError, "partial matrix"):
                module.export_completed()
            renderer.assert_not_called()


def receipt_fixture(*, recovered=True, ood=False):
    data = wrapper_fixture(ood=ood)
    result, job, protocol, model, evidence, payloads = data
    protocol["frozen_source_files"] = {"frozen.py": "2" * 64}
    pin = lambda name: {"path": name, "sha256": "1" * 64}
    status = "recovered_success" if recovered else "success"
    receipt = {"job_id": job["job_id"], "phase": "quality", "protocol_sha256": "3" * 64,
               "status": status, "result": pin("new_result"),
               "quality_classification": result["quality_classification"],
               "returncode": None if recovered else 0, "elapsed_s": None, "ended_at": None,
               "peak_observed_rss_bytes": None}
    row = {"status": status, "result": receipt["result"],
           "quality_classification": result["quality_classification"]}
    payloads["new_result"] = result
    if recovered:
        receipt["recovery"] = {"supervision_gap": True, "returncode_observed": False,
                               "timing_eligible": False, "started_receipt": pin("started"), "audit": pin("audit")}
        payloads["started"] = {"job_id": job["job_id"], "at": "2026-09-08T03:07:35+00:00"}
        payloads["stdout"] = {"result": receipt["result"], "quality_classification": result["quality_classification"]}
        payloads["audit"] = {"audit_schema": 1, "audit_type": "interrupted_worker_quality_recovery",
                             "protocol": {"path": "protocol", "sha256": "3" * 64},
                             "source_checks": {**{name: True for name in ("driver", "config", "architecture",
                                                                         "accepted_manifest", "python", "native_extension")},
                                               "frozen_source_file_count": 1, "frozen_source_mismatches": []},
                             "jobs": [{"job_id": job["job_id"], "canonical_sha256": job["canonical_sha256"],
                                       "seed": job["seed"], "result": receipt["result"], "native_trace": result["native_trace"],
                                       "started_receipt": pin("started"), "worker_stdout": pin("stdout"),
                                       "checks": {name: True for name in module.RECOVERY_CHECKS},
                                       "observed_returncode": None, "exit_reason": "unknown", "timing_eligible": False}]}
    return receipt, row, data


class RecoveryEvidenceTests(unittest.TestCase):
    def verify(self, fixture):
        receipt, row, data = fixture
        return module.verify_terminal_receipt(receipt, row, *data[1:5], "3" * 64)

    def test_audited_recovery_retains_quality_ood_and_receipt_distinction(self):
        for ood in (False, True):
            data = receipt_fixture(ood=ood)
            original = deepcopy(data[2][0])
            with self.subTest(ood=ood):
                row = self.verify(data)
                self.assertEqual(row["status"], "success")
                self.assertEqual(row["receipt_status"], "recovered_success")
                self.assertEqual(row["fidelity_valid"], not ood)
                self.assertIsNone(row["recovery"]["returncode"])
                self.assertFalse(row["recovery"]["timing_eligible"])
                self.assertEqual(original, data[2][0])

    def test_recovery_cannot_invent_supervisor_measurements(self):
        for field, value in (("returncode", 0), ("elapsed_s", 10), ("ended_at", "now"),
                             ("peak_observed_rss_bytes", 100)):
            for missing in (False, True):
                data = receipt_fixture()
                if missing:
                    del data[0][field]
                else:
                    data[0][field] = value
                with self.subTest(field=field, missing=missing), self.assertRaisesRegex(ValueError, "unknown"):
                    self.verify(data)

    def test_recovery_must_disclose_gap_unknown_returncode_and_no_timing(self):
        for field in ("supervision_gap", "returncode_observed", "timing_eligible"):
            data = receipt_fixture()
            data[0]["recovery"][field] = not data[0]["recovery"][field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "supervision gap"):
                self.verify(data)

    def test_recovery_requires_every_independent_quality_check(self):
        for check in module.RECOVERY_CHECKS:
            for value in (False, None, 1):
                data = receipt_fixture()
                data[2][-1]["audit"]["jobs"][0]["checks"][check] = value
                with self.subTest(check=check, value=value), self.assertRaisesRegex(ValueError, "quality checks"):
                    self.verify(data)

    def test_recovery_audit_must_bind_exact_job_input_result_and_trace(self):
        changes = {"canonical_sha256": "0" * 64, "seed": 2, "result": {}, "native_trace": {}, "started_receipt": {}}
        for field, value in changes.items():
            data = receipt_fixture()
            data[2][-1]["audit"]["jobs"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exact input/result"):
                self.verify(data)
        data = receipt_fixture()
        entries = data[2][-1]["audit"]["jobs"]
        entries.append(deepcopy(entries[0]))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.verify(data)

    def test_recovery_audit_keeps_unknown_exit_and_no_eligible_timing(self):
        for field, value in (("observed_returncode", 0), ("exit_reason", "success"), ("timing_eligible", True)):
            data = receipt_fixture()
            data[2][-1]["audit"]["jobs"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "infer process exit"):
                self.verify(data)

    def test_recovery_rejects_incomplete_frozen_source_audit(self):
        for field, value in (("driver", False), ("native_wheel", False), ("frozen_source_file_count", 0),
                             ("frozen_source_mismatches", ["changed.py"])):
            data = receipt_fixture()
            data[2][-1]["audit"]["source_checks"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "frozen source checks"):
                self.verify(data)
        data = receipt_fixture()
        data[2][-1]["audit"]["protocol"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "protocol identity"):
            self.verify(data)

    def test_optional_auditor_and_compiler_log_pins_join_verified_evidence(self):
        data = receipt_fixture()
        audit = data[2][-1]["audit"]
        audit["auditor"] = {"path": "auditor.py", "sha256": "4" * 64}
        audit["jobs"][0]["compiler_log"] = {"path": "compiler.log", "sha256": "5" * 64}
        self.verify(data)
        data[2][4].file.assert_any_call(audit["auditor"])
        data[2][4].file.assert_any_call(audit["jobs"][0]["compiler_log"])

    def test_recovery_requires_matching_start_and_worker_completion_evidence(self):
        for name, field, value in (("started", "job_id", "other-job"), ("stdout", "result", {}),
                                    ("stdout", "quality_classification", "model_out_of_domain")):
            data = receipt_fixture()
            data[2][-1][name][field] = value
            with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                self.verify(data)

    def test_ordinary_success_cannot_hide_an_unknown_or_recovered_exit(self):
        self.assertEqual(self.verify(receipt_fixture(recovered=False))["receipt_status"], "success")
        for code in (None, False, 1):
            data = receipt_fixture(recovered=False)
            data[0]["returncode"] = code
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "observed zero"):
                self.verify(data)
        data = receipt_fixture(recovered=False)
        data[0]["recovery"] = {}
        with self.assertRaisesRegex(ValueError, "observed zero"):
            self.verify(data)

    def test_interrupted_and_existing_failures_stay_terminal_without_quality(self):
        for status in ("interrupted", "memory_limit", "timeout", "runner_error"):
            data = receipt_fixture(recovered=False)
            for target in data[:2]:
                target.update(status=status, result=None, quality_classification=None)
            row = self.verify(data)
            with self.subTest(status=status):
                self.assertEqual(row["metrics"], {})
                self.assertFalse(row["fidelity_valid"])
                self.assertFalse(module.seed_cell([row, record(seed=1), record(seed=2)], module.SEEDS)["complete"])


def amendment_fixture(root, *, complete=True):
    def write(name, value):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(module.canonical_json(value))
        return {"path": str(target), "sha256": module.sha256(target)}
    at = lambda second: f"2026-09-08T00:00:{second:02d}+00:00"
    protocol = {"max_workers": 4, "timeout_s": 600, "rss_limit_bytes": 3 * 1024**3,
                "driver": write("driver.py", {})}
    protocol_pin = write("study/protocol.json", protocol)
    launch = {"protocol": protocol_pin, "supervisor": write("primary.py", {}), "workers": 4}
    write("primary/launch.sha256.json", write("primary/launch.json", launch))
    primary = {"pid": 1, "started_at": at(0), "protocol": protocol_pin, "workers": 4,
               "timeout_s": 600, "rss_limit_bytes": 3 * 1024**3, "selected_jobs": ["primary", "extra"]}
    primary_pin = write("primary/execution.json", primary)
    amendment = {"amendment_type": "author_requested_concurrency_expansion", "protocol": protocol_pin,
                 "supervisor": write("extra.py", {}), "primary_execution": primary_pin,
                 "primary_run_dir": str(root / "primary"), "original_workers": 4, "extra_workers": 2,
                 "total_workers": 6, "timeout_s": 600, "rss_limit_bytes": 3 * 1024**3,
                 "computational_controls_changed": False, "timing_comparison_eligible": False,
                 "existing_results_preserved": True, "coverage_caveat": "Both supervisors' timeout coverage may change.",
                 "user_authorization": "Increase concurrency."}
    amendment_pin = write("expansion/amendment.json", amendment)
    write("expansion/amendment.sha256.json", amendment_pin)
    write("expansion/execution.json", {"amendment": amendment_pin, "primary_launch": launch, "primary_pid": 1,
                                        "extra_workers": 2, "total_ceiling": 6, "reverse_queue": ["extra"], "started_at": at(10)})
    jobs, verified_jobs = {}, {}
    for job_id, start, end, status in (("before", 1, 2, "success"), ("primary", 5, 12, "timeout"),
                                        ("extra", 11, 15, "success"), ("recovered", 0, None, "recovered_success")):
        jobs[job_id] = {"origin": "fresh"}
        write(f"study/quality/receipts/{job_id}.started.json", {"job_id": job_id, "at": at(start)})
        pin = write(f"study/quality/receipts/{job_id}.json", {"ended_at": at(end) if end is not None else None})
        verified_jobs[job_id] = ({"receipt": pin, "status": status}, {})
    if complete:
        write("primary/completion.json", {"at": at(25), "completed_dispatches": 2, "errors": []})
        write("expansion/completion.json", {"started_at": at(10), "ended_at": at(20), "errors": [],
                                            "contention_affected_job_ids": ["primary", "extra", "recovered"],
                                            "includes_primary_jobs": True, "requires_final_independent_audit": True})
        write("expansion/events/extra.json", {"job_id": "extra", "action": "terminal_observed", "at": at(16)})
    return {"references": [amendment_pin], "protocol": protocol, "protocol_path": root / "study/protocol.json",
            "jobs": jobs, "verified_jobs": verified_jobs, "evidence": module.Evidence(root),
            "required": {amendment_pin["sha256"]: amendment_pin["path"]}}


class ExecutionAmendmentTests(unittest.TestCase):
    def test_closed_amendment_covers_primary_timeout_and_unknown_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            data = amendment_fixture(Path(directory))
            report = module.audit_execution_amendments(**data)
            self.assertTrue(report["complete"])
            amendment = report["amendments"][0]
            self.assertEqual(amendment["potentially_affected_job_ids"], ["extra", "primary", "recovered"])
            self.assertEqual(amendment["affected_status_counts"]["timeout"], 1)
            self.assertEqual(amendment["unknown_end_conservatively_included"], ["recovered"])
            self.assertTrue(amendment["coverage_may_change_for_primary_and_extra_workers"])
            self.assertFalse(amendment["timing_comparison_eligible"])

    def test_known_amendment_cannot_be_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            data = amendment_fixture(Path(directory)); data["references"] = []
            self.assertFalse(module.audit_execution_amendments(**data, allow_partial=True)["complete"])
            with self.assertRaisesRegex(ValueError, "closure is incomplete"):
                module.audit_execution_amendments(**data)

    def test_pending_both_supervisors_is_audit_only(self):
        with tempfile.TemporaryDirectory() as directory:
            data = amendment_fixture(Path(directory), complete=False)
            report = module.audit_execution_amendments(**data, allow_partial=True)
            self.assertFalse(report["complete"])
            self.assertEqual(report["amendments"][0]["missing_closure"], ["completion", "primary_completion"])
            with self.assertRaisesRegex(ValueError, "closure is incomplete"):
                module.audit_execution_amendments(**data)

    def test_either_missing_completion_or_event_blocks_formal_export(self):
        for name in ("primary/completion.json", "expansion/completion.json", "expansion/events/extra.json"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(name=name):
                root = Path(directory); data = amendment_fixture(root); (root / name).unlink()
                self.assertFalse(module.audit_execution_amendments(**data, allow_partial=True)["complete"])
                with self.assertRaisesRegex(ValueError, "closure is incomplete"):
                    module.audit_execution_amendments(**data)

    def test_primary_overlap_must_not_be_omitted_from_declared_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = amendment_fixture(root)
            target = root / "expansion/completion.json"
            completion = json.loads(target.read_text()); completion["contention_affected_job_ids"].remove("primary")
            target.write_text(module.canonical_json(completion))
            with self.assertRaisesRegex(ValueError, "either supervisor"):
                module.audit_execution_amendments(**data)

    def test_amendment_may_not_change_computational_controls_or_per_job_limits(self):
        for field, value in (("computational_controls_changed", True), ("timeout_s", 900),
                             ("rss_limit_bytes", 4 * 1024**3), ("total_workers", 8), ("timing_comparison_eligible", True)):
            with tempfile.TemporaryDirectory() as directory, self.subTest(field=field):
                root = Path(directory); data = amendment_fixture(root)
                target = root / "expansion/amendment.json"
                value_data = json.loads(target.read_text()); value_data[field] = value
                target.write_text(module.canonical_json(value_data))
                pin = {"path": str(target), "sha256": module.sha256(target)}
                (root / "expansion/amendment.sha256.json").write_text(module.canonical_json(pin))
                data["references"] = [pin]
                with self.assertRaisesRegex(ValueError, "unsupported controls"):
                    module.audit_execution_amendments(**data)

    def test_supervisor_source_hash_drift_is_rejected_even_in_audit_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = amendment_fixture(root)
            (root / "extra.py").write_text("changed")
            with self.assertRaisesRegex(ValueError, "drift"):
                module.audit_execution_amendments(**data, allow_partial=True)

    def test_cli_and_summary_can_repeat_identical_pin_without_double_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            data = amendment_fixture(Path(directory)); data["references"] *= 2
            report = module.audit_execution_amendments(**data)
            self.assertEqual(len(report["amendments"]), 1)


if __name__ == "__main__":
    unittest.main()
