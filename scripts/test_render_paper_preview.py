"""Offline path and preview-cleanup tests; Poppler calls use tiny image fixtures."""

from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "paper_preview", Path(__file__).with_name("render_paper_preview.py"))
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


class PaperPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.build = self.root / "IEEE_conference_template/build"
        self.script = self.root / "scripts/render_paper_preview.py"

    def fixture(self, language, pages=2):
        output = self.build / f"paper_{language}"
        output.mkdir(parents=True)
        (output / f"paper_{language}.pdf").write_bytes(b"mock PDF")
        figure = self.build / "paper_zh/figures/overall_framework.pdf"
        figure.parent.mkdir(parents=True, exist_ok=True)
        figure.write_bytes(b"mock Figure 3")
        return output

    def render(self, language, pages=2):
        def fake_poppler(command, **kwargs):
            prefix = Path(command[-1])
            if "-singlefile" in command:
                paths = [Path(str(prefix) + ".png")]
            else:
                digits = len(str(pages))
                paths = [prefix.with_name(f"{prefix.name}-{index:0{digits}d}.png")
                         for index in range(1, pages + 1)]
            for path in paths:
                preview.Image.new("RGB", (16, 24), "white").save(path)
            return subprocess.CompletedProcess(command, 0)

        output = io.StringIO()
        with patch.object(preview, "__file__", str(self.script)), \
                patch("sys.argv", [str(self.script), "--language", language]), \
                patch.object(preview.subprocess, "check_output", return_value=f"Pages: {pages}\n") as info, \
                patch.object(preview.subprocess, "run", side_effect=fake_poppler) as run, \
                redirect_stdout(output):
            preview.main()
        return info, run, output.getvalue()

    def test_chinese_preview_uses_manuscript_build(self):
        output = self.fixture("zh")
        info, run, message = self.render("zh")
        self.assertEqual(info.call_args.args[0], ["pdfinfo", str(output / "paper_zh.pdf")])
        self.assertEqual(run.call_args_list[0].args[0][-2:],
                         [str(output / "paper_zh.pdf"), str(output / "preview/page")])
        self.assertEqual(run.call_args_list[1].args[0][-2:],
                         [str(self.build / "paper_zh/figures/overall_framework.pdf"),
                          str(output / "preview/overall_framework")])
        self.assertTrue((output / "page_overview.png").is_file())
        self.assertIn(str(output / "page_overview.png"), message)
        self.assertFalse((self.root / "build").exists())

    def test_english_preview_uses_english_output_and_shared_chinese_figure(self):
        output = self.fixture("en")
        info, run, _ = self.render("en")
        self.assertEqual(info.call_args.args[0][1], str(output / "paper_en.pdf"))
        self.assertEqual(run.call_args_list[0].args[0][-1], str(output / "preview/page"))
        self.assertEqual(run.call_args_list[1].args[0][-2],
                         str(self.build / "paper_zh/figures/overall_framework.pdf"))
        self.assertTrue((output / "page_overview.png").is_file())
        self.assertFalse((self.root / "build").exists())

    def test_cleanup_removes_only_obsolete_numbered_page_renders(self):
        output = self.fixture("zh")
        directory = output / "preview"
        directory.mkdir()
        for name in ("page-9.png", "page-01.png", "page-review.png", "author-notes.txt"):
            (directory / name).write_bytes(b"retained unless stale numbered page")
        self.render("zh")
        self.assertFalse((directory / "page-9.png").exists())
        self.assertFalse((directory / "page-01.png").exists())
        self.assertTrue((directory / "page-review.png").exists())
        self.assertTrue((directory / "author-notes.txt").exists())
        self.assertTrue((directory / "page-1.png").exists())
        self.assertTrue((directory / "page-2.png").exists())

    def test_missing_manuscript_build_stops_without_creating_root_build(self):
        with patch.object(preview, "__file__", str(self.script)), \
                patch("sys.argv", [str(self.script)]), \
                patch.object(preview.subprocess, "run") as run, \
                self.assertRaisesRegex(SystemExit, "make paper"):
            preview.main()
        run.assert_not_called()
        self.assertFalse((self.root / "build").exists())
        self.assertFalse(self.build.exists())


if __name__ == "__main__":
    unittest.main()
