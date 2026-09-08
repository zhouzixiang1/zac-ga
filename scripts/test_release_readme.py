"""Keep GitHub result summaries bound to the distributed manuscript data."""
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseReadmeTests(unittest.TestCase):
    def test_comparison_rows_match_generated_values(self):
        readme = (ROOT / "README.md").read_text()
        values = json.loads((ROOT / "ZAC_zzx/results/default_initial_v1/paper_exports/default_initial_values.json").read_text())
        for suite, name in (("zac18", "ZAC18"), ("qmap154", "QMAP154")):
            data = values["datasets"][suite]
            for method, label in (("M1", "Reuse-aware ZAC"), ("M2", "Routing-aware placement")):
                row = data["comparisons"][method]
                expected = (f"| {name} | {data['common_canonical_N']} | {label} | "
                            f"{row['fidelity_gain_percent']:.2f}% | "
                            f"{row['mean_batch_reduction_percent']:.2f}% |")
                self.assertIn(expected, readme)

    def test_relative_readme_links_resolve(self):
        readme = (ROOT / "README.md").read_text()
        for target in re.findall(r"\]\(([^)\s]+)\)", readme):
            if target.startswith(("http:", "https:", "#")):
                continue
            path = target.split("#", 1)[0]
            with self.subTest(target=target):
                self.assertTrue((ROOT / path).exists(), target)

    def test_old_workbook_is_not_labeled_as_updated_study(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("not** a workbook of the updated initialization study", readme)
        self.assertIn("not per-circuit guarantees or compiler speedups", readme)
        self.assertIn("pending translation snapshot", readme)


if __name__ == "__main__":
    unittest.main()
