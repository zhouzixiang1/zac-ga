"""Negative tests for the approved abstract and keyword protection."""
from pathlib import Path
import json
import tempfile
import unittest
from paper_paths import PAPER_ROOT, artifact_path
import verify_paper_zh as verifier


class ProtectedTextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "paper"
        self.root.mkdir()
        self.fixture = json.loads(artifact_path(PAPER_ROOT, "compact_protected_text.json").read_text())
        path = artifact_path(self.root, "compact_protected_text.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.fixture))
        for language in ("zh", "en"):
            self.write(language, self.fixture["protected"][language])

    def write(self, language, fields):
        (self.root / f"paper_{language}.tex").write_text("\n".join(
            r"\begin{" + key + "}" + value + r"\end{" + key + "}" for key, value in fields.items()))

    def errors(self, language):
        errors = []
        verifier._audit_protected_text(self.root, language, errors)
        return errors

    def test_original_text_and_only_approved_aod_shortening_pass(self):
        for language in ("zh", "en"):
            self.assertFalse(self.errors(language))
        fields = dict(self.fixture["protected"]["en"])
        fields["abstract"] = fields["abstract"].replace("acousto-optic deflectors (AODs)", "AODs")
        self.write("en", fields)
        self.assertFalse(self.errors("en"))

    def test_any_other_abstract_or_keyword_edit_fails(self):
        for language in ("zh", "en"):
            for field in ("abstract", "IEEEkeywords"):
                with self.subTest(language=language, field=field):
                    fields = dict(self.fixture["protected"][language])
                    fields[field] += " changed"
                    self.write(language, fields)
                    self.assertIn(f"protected_text_changed:{language}:{field}", self.errors(language))

    def test_aod_singular_is_not_the_approved_replacement(self):
        fields = dict(self.fixture["protected"]["en"])
        fields["abstract"] = fields["abstract"].replace("acousto-optic deflectors (AODs)", "AOD")
        self.write("en", fields)
        self.assertIn("protected_text_changed:en:abstract", self.errors("en"))

    def test_duplicate_abstract_is_rejected(self):
        path = self.root / "paper_en.tex"
        path.write_text(path.read_text() + r"\begin{abstract}duplicate\end{abstract}")
        self.assertIn("protected_text_changed:en:abstract", self.errors("en"))


if __name__ == "__main__":
    unittest.main()
