"""Offline Overleaf export protections; no test contacts a Git server."""

import importlib.util
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "overleaf_preparation", Path(__file__).with_name("prepare_overleaf_sync.py"))
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class OverleafPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.checkout = self.root / "build/overleaf-sync"
        (self.checkout / ".git").mkdir(parents=True)
        self.tip = "7ee6faf19c41a249d93a7f3a689dda35d367119e"

    def mock_git(self, arguments, *, cwd, network=False, **kwargs):
        if arguments[0] == "status":
            output = ""
        elif arguments[:2] == ["remote", "get-url"]:
            output = sync.REMOTE_URL
        elif arguments[0] == "symbolic-ref":
            output = "main"
        elif arguments[0] == "rev-parse":
            output = self.tip
        else:
            output = ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    def test_generated_files_and_local_latexmkrc_are_excluded(self):
        for name in (".git/config", "build/paper.pdf", "tmp/a.png", "x.aux", "x.log",
                     "x.synctex.gz", "writing/__pycache__/a.pyc", ".latexmkrc",
                     "latexmkrc", "paper_zh.pdf", "paper_en.pdf", "figures/overall_framework.pdf"):
            with self.subTest(name=name):
                self.assertTrue(sync.excluded(PurePosixPath(name)))
        for name in ("paper_zh.tex", "paper_en.tex", "figures/data/results.dat", "template-A4.pdf",
                     "IEEEtran_HOWTO.pdf", ".gitignore"):
            with self.subTest(name=name):
                self.assertFalse(sync.excluded(PurePosixPath(name)))

    def test_export_only_changes_one_include_path(self):
        method = PurePosixPath("sections/03_method.tex")
        original = b"before {" + sync.LOCAL_FIGURE_PATH + b"} after"
        snapshot = {method: original, PurePosixPath("paper_zh.tex"): b"unchanged prose"}
        result = sync.export_payload(snapshot, b"verified figure")
        self.assertEqual(result[method], b"before {figures/overall_framework.pdf} after")
        self.assertEqual(result[PurePosixPath("paper_zh.tex")], b"unchanged prose")
        self.assertEqual(snapshot[method], original)

    def test_missing_or_duplicated_include_path_stops_export(self):
        for source in (b"wrong path", sync.LOCAL_FIGURE_PATH * 2):
            with self.subTest(source=source), self.assertRaises(sync.PreparationError):
                sync.export_payload({PurePosixPath("sections/03_method.tex"): source}, b"figure")

    def test_bilingual_export_only_changes_both_method_include_paths(self):
        methods = [PurePosixPath("sections/03_method.tex"),
                   PurePosixPath("sections_en/03_method.tex")]
        source = b"before {" + sync.LOCAL_FIGURE_PATH + b"} after"
        other = PurePosixPath("sections_en/01_introduction.tex")
        snapshot = {method: source for method in methods}
        snapshot.update({PurePosixPath("paper_zh.tex"): b"Chinese entry",
                         PurePosixPath("paper_en.tex"): b"English entry",
                         other: b"literal example: " + sync.LOCAL_FIGURE_PATH})
        original = dict(snapshot)
        result = sync.export_payload(snapshot, b"verified figure")
        for method in methods:
            self.assertEqual(result[method], b"before {figures/overall_framework.pdf} after")
        for relative in snapshot.keys() - set(methods):
            self.assertEqual(result[relative], snapshot[relative])
        self.assertEqual(snapshot, original)
        self.assertEqual(result[PurePosixPath("figures/overall_framework.pdf")], b"verified figure")

    def test_english_entry_or_sections_require_valid_english_method(self):
        method = PurePosixPath("sections_en/03_method.tex")
        for marker in (PurePosixPath("paper_en.tex"),
                       PurePosixPath("sections_en/01_introduction.tex")):
            for source in (None, b"wrong path", sync.LOCAL_FIGURE_PATH * 2):
                snapshot = {PurePosixPath("sections/03_method.tex"): sync.LOCAL_FIGURE_PATH,
                            marker: b"English source"}
                if source is not None:
                    snapshot[method] = source
                with self.subTest(marker=marker, source=source), \
                        self.assertRaisesRegex(sync.PreparationError, "sections_en/03_method.tex"):
                    sync.export_payload(snapshot, b"figure")

    def test_english_method_without_entry_is_also_adapted(self):
        snapshot = {PurePosixPath("sections/03_method.tex"): sync.LOCAL_FIGURE_PATH,
                    PurePosixPath("sections_en/03_method.tex"): sync.LOCAL_FIGURE_PATH}
        result = sync.export_payload(snapshot, b"figure")
        self.assertEqual(result[PurePosixPath("sections_en/03_method.tex")], sync.OVERLEAF_FIGURE_PATH)

    def test_report_lists_all_actual_scientific_source_adaptations(self):
        for bilingual in (False, True):
            methods = [PurePosixPath("sections/03_method.tex")]
            if bilingual:
                methods.append(PurePosixPath("sections_en/03_method.tex"))
            snapshot = {method: sync.LOCAL_FIGURE_PATH for method in methods}
            snapshot[PurePosixPath("paper_zh.tex")] = b"unchanged entry"
            output = io.StringIO()
            with self.subTest(bilingual=bilingual), \
                    patch.object(sync.sys, "argv", ["prepare_overleaf_sync.py", "--check-only",
                                                   "--expected-remote", self.tip]), \
                    patch.object(sync, "local_files", return_value=sorted(snapshot)), \
                    patch.object(sync, "read_sources", return_value=snapshot), \
                    patch.object(sync, "check_build", return_value=b"figure"), \
                    patch.object(sync, "prepare_checkout", return_value=(self.checkout, self.tip, [])), \
                    patch.object(sync, "copy_export") as copy_export, redirect_stdout(output):
                self.assertEqual(sync.main(), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["only_scientific_source_adaptation"], [
                f"{method}: local PDF path to figures/overall_framework.pdf" for method in methods])
            copy_export.assert_not_called()

    def test_dirty_checkout_stops_before_fetch(self):
        with patch.object(sync, "git", return_value=subprocess.CompletedProcess(
                [], 0, " M paper_zh.tex\n", "")) as git:
            with self.assertRaisesRegex(sync.PreparationError, "dirty"):
                sync.prepare_checkout(self.root, self.tip)
        self.assertEqual(git.call_count, 1)
        self.assertFalse(git.call_args.kwargs.get("network", False))

    def test_remote_readme_change_also_stops_before_merge(self):
        new_tip = "a" * 40

        def run(arguments, **kwargs):
            if arguments == ["rev-parse", "refs/remotes/origin/main"]:
                return subprocess.CompletedProcess(arguments, 0, new_tip, "")
            if arguments[0] == "diff":
                return subprocess.CompletedProcess(arguments, 0, "README_zh.md\0", "")
            return self.mock_git(arguments, **kwargs)

        with patch.object(sync, "git", side_effect=run) as git:
            with self.assertRaisesRegex(sync.PreparationError, "README_zh.md"):
                sync.prepare_checkout(self.root, self.tip)
        self.assertFalse(any(call.args[0][0] == "merge" for call in git.call_args_list))

    def test_wrong_origin_stops_before_fetch(self):
        def run(arguments, **kwargs):
            if arguments[:2] == ["remote", "get-url"]:
                return subprocess.CompletedProcess(arguments, 0, "https://example.invalid/wrong", "")
            return self.mock_git(arguments, **kwargs)

        with patch.object(sync, "git", side_effect=run) as git:
            with self.assertRaisesRegex(sync.PreparationError, "origin differs"):
                sync.prepare_checkout(self.root, self.tip)
        self.assertFalse(any(call.kwargs.get("network", False) for call in git.call_args_list))

    def test_matching_tip_fetches_with_network_helper_and_fast_forwards(self):
        with patch.object(sync, "git", side_effect=self.mock_git) as git:
            checkout, tip, changed = sync.prepare_checkout(self.root, self.tip)
        self.assertEqual((checkout, tip, changed), (self.checkout, self.tip, []))
        network_calls = [call.args[0] for call in git.call_args_list
                         if call.kwargs.get("network", False)]
        self.assertEqual(network_calls, [["fetch", "--prune", "origin"]])
        self.assertTrue(any(call.args[0] == ["merge", "--ff-only", "refs/remotes/origin/main"]
                            for call in git.call_args_list))

    def test_network_uses_absolute_keychain_helper_and_suppresses_tracing(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.dict(sync.os.environ, {"GIT_TRACE_CURL": "1", "GIT_CURL_VERBOSE": "1"}), \
                patch.object(sync.subprocess, "run", return_value=result) as run:
            sync.git(["fetch", "origin"], cwd=self.checkout, network=True)
        command = run.call_args.args[0]
        self.assertIn(f"credential.helper={sync.KEYCHAIN_HELPER}", command)
        self.assertTrue(sync.KEYCHAIN_HELPER.startswith("/Applications/Xcode.app/"))
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("GIT_TRACE_CURL", environment)
        self.assertNotIn("GIT_CURL_VERBOSE", environment)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_copy_keeps_remote_extra_files_and_local_source(self):
        method = PurePosixPath("sections/03_method.tex")
        paper = self.root / "IEEE_conference_template"
        (paper / "sections").mkdir(parents=True)
        source = b"include{" + sync.LOCAL_FIGURE_PATH + b"}"
        (paper / method).write_bytes(source)
        extra = self.checkout / "remote-notes.txt"
        extra.write_bytes(b"author's remote notes")
        snapshot = {method: source}
        with patch.object(sync, "local_files", return_value=[method]), \
                patch.object(sync, "git", side_effect=self.mock_git):
            sync.copy_export(self.root, self.checkout, [method], snapshot,
                             sync.export_payload(snapshot, b"figure"))
        self.assertEqual(extra.read_bytes(), b"author's remote notes")
        self.assertEqual((paper / method).read_bytes(), source)
        self.assertEqual((self.checkout / method).read_bytes(), b"include{figures/overall_framework.pdf}")

    def test_symlink_destination_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.checkout / "figures").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(sync.PreparationError, "symbolic-link"):
            sync.safe_destination(self.checkout, PurePosixPath("figures/overall_framework.pdf"))

    def test_credential_in_error_url_is_redacted(self):
        self.assertNotIn("secret", sync.safe_message("failure https://git:secret@git.overleaf.com/id"))

    def build_fixture(self):
        source = self.root / "IEEE_conference_template/paper_zh.tex"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original source")
        build = self.root / "build/paper_zh"
        (build / "figures").mkdir(parents=True)
        (build / "paper_zh.pdf").write_bytes(b"paper PDF")
        (build / "figures/overall_framework.pdf").write_bytes(b"verified figure")
        artifacts = {name: {"sha256": hashlib.sha256((build / name).read_bytes()).hexdigest()}
                     for name in ("paper_zh.pdf", "figures/overall_framework.pdf")}
        snapshot = {PurePosixPath("paper_zh.tex"): source.read_bytes()}
        manifest = {
            "protocol": "paper-source-build-v1",
            "source_sha256": {name.as_posix(): hashlib.sha256(content).hexdigest()
                              for name, content in snapshot.items()},
            "artifacts": artifacts,
        }
        (build / "source_build_manifest.json").write_text(json.dumps(manifest))
        (build / "final_paper_qa.json").write_text(json.dumps({
            "status": "pass", "page_count": 9, "references_on_last_page": True,
            "artifacts": {f"../build/paper_zh/{name}": declaration
                          for name, declaration in artifacts.items()},
        }))
        return source, build, snapshot

    def test_old_mtime_new_source_is_rejected_by_content_hash(self):
        source, _, snapshot = self.build_fixture()
        previous = source.stat()
        source.write_bytes(b"changed source")
        os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        snapshot[PurePosixPath("paper_zh.tex")] = source.read_bytes()
        with self.assertRaisesRegex(sync.PreparationError, "Source content differs"):
            sync.check_build(self.root, snapshot)

    def test_figure_bytes_are_read_once_and_returned_after_hash_check(self):
        _, build, snapshot = self.build_fixture()
        figure = build / "figures/overall_framework.pdf"
        original_read = Path.read_bytes
        figure_reads = []

        def read(path):
            content = original_read(path)
            if path == figure:
                figure_reads.append(path)
                figure.write_bytes(b"replaced after capture")
            return content

        with patch.object(Path, "read_bytes", read):
            result = sync.check_build(self.root, snapshot)
        self.assertEqual(result, b"verified figure")
        self.assertEqual(len(figure_reads), 1)

    def test_dirty_after_fetch_stops_before_merge(self):
        statuses = 0

        def run(arguments, **kwargs):
            nonlocal statuses
            if arguments[0] == "status":
                statuses += 1
                return subprocess.CompletedProcess(arguments, 0, "" if statuses == 1 else " M paper_zh.tex\n", "")
            return self.mock_git(arguments, **kwargs)

        with patch.object(sync, "git", side_effect=run) as git:
            with self.assertRaisesRegex(sync.PreparationError, "dirty"):
                sync.prepare_checkout(self.root, self.tip)
        self.assertTrue(any(call.args[0][0] == "fetch" for call in git.call_args_list))
        self.assertFalse(any(call.args[0][0] == "merge" for call in git.call_args_list))

    def test_untracked_or_ignored_target_collision_is_not_overwritten(self):
        target = self.checkout / "paper_zh.tex"
        target.write_bytes(b"manual untracked or ignored content")
        with patch.object(sync, "git", side_effect=self.mock_git):
            with self.assertRaisesRegex(sync.PreparationError, "Untracked or ignored"):
                sync.copy_export(self.root, self.checkout, [], {},
                                 {PurePosixPath("paper_zh.tex"): b"replacement"})
        self.assertEqual(target.read_bytes(), b"manual untracked or ignored content")

    def test_edit_immediately_before_write_is_preserved(self):
        target = self.checkout / "paper_zh.tex"
        target.write_bytes(b"HEAD content")
        head_reads = 0

        def run(arguments, **kwargs):
            nonlocal head_reads
            if arguments == ["rev-parse", "HEAD"]:
                head_reads += 1
                if head_reads == 2:
                    target.write_bytes(b"concurrent manual edit")
            if arguments[0] == "ls-tree":
                return subprocess.CompletedProcess(arguments, 0, "paper_zh.tex\0", "")
            if arguments[0] == "show":
                return subprocess.CompletedProcess(arguments, 0, b"HEAD content", b"")
            return self.mock_git(arguments, **kwargs)

        with patch.object(sync, "git", side_effect=run):
            with self.assertRaisesRegex(sync.PreparationError, "differs from HEAD"):
                sync.copy_export(self.root, self.checkout, [], {},
                                 {PurePosixPath("paper_zh.tex"): b"replacement"})
        self.assertEqual(target.read_bytes(), b"concurrent manual edit")


if __name__ == "__main__":
    unittest.main()
