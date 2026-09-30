"""Figure data preserves all paired horizon observations and aggregation."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import generate_horizon_figure as figure
from paper_paths import artifact_path, tool_path


class HorizonFigureTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads(artifact_path(figure.ROOT, figure.SOURCE).read_text())
        self.macros = artifact_path(figure.ROOT, figure.MACROS).read_text()

    def test_isolated_project_uses_metadata_and_separate_tool_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            paper_root = Path(temporary) / "IEEE_conference_template"
            for relative in (figure.SOURCE, figure.MACROS):
                path = artifact_path(paper_root, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(artifact_path(figure.ROOT, relative), path)
            script = tool_path(paper_root, figure.__file__)
            script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(figure.__file__, script)
            self.assertFalse((paper_root / figure.SOURCE).exists())
            output = figure.generate(paper_root)
            self.assertIn(paper_root / "figures/data/horizon_trends.dat", output)
            self.assertIn(Path(temporary) / "docs/paper/metadata/figures/data/horizon_trends.provenance.json", output)
            self.assertEqual(list(output.values()), list(figure.generate().values()))

    def test_all_circuits_and_real_horizon_spacing_are_preserved(self):
        rows, circuits, checks = figure.derive(self.source, self.macros)
        self.assertEqual([r["horizon"] for r in rows], [0, 1, 2, 4, 8])
        self.assertEqual(len(circuits), 10)
        self.assertEqual(len(checks), 10)
        self.assertTrue(all(rows[-1][f"{m}{i}"] == 1 for m in ("f", "b") for i in range(10)))
        self.assertLess(rows[0]["f9"], .3)
        for path, content in figure.generate().items():
            self.assertEqual(path.read_text(), content)

    def test_missing_seed_is_rejected(self):
        self.source["job_outcomes"] = [r for r in self.source["job_outcomes"]
            if not (r["circuit"] == "ghz_n23" and r["horizon"] == 0 and r["seed"] == 1)]
        with self.assertRaisesRegex(ValueError, "exactly three seeds"):
            figure.derive(self.source, self.macros)

    def test_wrong_median_or_ratio_is_rejected(self):
        for field in ("median_fidelity", "fidelity_ratio_vs_h8"):
            source = copy.deepcopy(self.source)
            source["per_circuit"][0]["horizons"]["0"][field] += .1
            with self.assertRaisesRegex(ValueError, "differs"):
                figure.derive(source, self.macros)

    def test_aggregate_macro_drift_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Displayed aggregate"):
            figure.derive(self.source, self.macros.replace("0.7824", "0.7825"))
        self.source["values"]["HorizonExtZeroFidelityRatio"] += .1
        with self.assertRaisesRegex(ValueError, "Geometric mean"):
            figure.derive(self.source, self.macros)


if __name__ == "__main__":
    unittest.main()
