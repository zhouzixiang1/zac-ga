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
        self.checkout = self.root / "IEEE_conference_template/build/overleaf-sync"
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
                     "latexmkrc", "paper_zh.pdf", "paper_en.pdf", "figures/overall_framework.pdf",
                     "verify_paper_zh.py", "paper_argument_values.json",
                     "template-A4.tex", "template-A4.pdf", "fig1.png", "IEEEtran_HOWTO.pdf"):
            with self.subTest(name=name):
                self.assertTrue(sync.excluded(PurePosixPath(name)))
        for name in ("paper_zh.tex", "paper_en.tex", "figures/data/results.dat", "README.md", ".gitignore"):
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

    def test_legacy_or_prefixed_local_figure_path_is_not_partially_replaced(self):
        for source in (b"{../" + sync.LOCAL_FIGURE_PATH + b"}",
                       b"{IEEE_conference_template/" + sync.LOCAL_FIGURE_PATH + b"}",
                       b"{" + sync.LOCAL_FIGURE_PATH + b".backup}",
                       b"{" + sync.LOCAL_FIGURE_PATH + b"} {../" + sync.LOCAL_FIGURE_PATH + b"}"):
            with self.subTest(source=source), self.assertRaises(sync.PreparationError):
                sync.export_payload({PurePosixPath("sections/03_method.tex"): source}, b"figure")

    def test_nested_build_checkout_and_generated_sources_are_not_exported(self):
        for name in ("paper_zh.tex", "sections/03_method.tex"):
            path = self.root / "IEEE_conference_template" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        listing = "\0".join((
            "IEEE_conference_template/paper_zh.tex",
            "IEEE_conference_template/sections/03_method.tex",
            "IEEE_conference_template/build/overleaf-sync/paper_zh.tex",
            "IEEE_conference_template/build/overleaf-sync/.git/config",
            "IEEE_conference_template/build/paper_zh/figures/overall_framework.pdf",
            "IEEE_conference_template/build/paper_en/paper_en.pdf", ""))
        with patch.object(sync, "git", return_value=subprocess.CompletedProcess([], 0, listing, "")):
            self.assertEqual(sync.local_files(self.root), [PurePosixPath("paper_zh.tex"),
                                                          PurePosixPath("sections/03_method.tex")])

    def test_relocated_tracked_files_are_not_exported_as_missing_sources(self):
        main = self.root / "IEEE_conference_template/paper_zh.tex"
        main.write_text("fixture")
        listing = "IEEE_conference_template/paper_zh.tex\0IEEE_conference_template/initial_lookahead_values.tex\0"
        with patch.object(sync, "git", return_value=subprocess.CompletedProcess([], 0, listing, "")):
            self.assertEqual(sync.local_files(self.root), [PurePosixPath("paper_zh.tex")])

    def test_bilingual_export_only_changes_both_method_include_paths(self):
        methods = [PurePosixPath("sections/03_method.tex"),
                   PurePosixPath("sections_en/03_method.tex")]
        source = b"before {" + sync.LOCAL_FIGURE_PATH + b"} after"
        other = PurePosixPath("sections_en/01_introduction.tex")
        snapshot = {method: source.replace(sync.LOCAL_FIGURE_PATH, sync.LOCAL_EN_FIGURE_PATH)
                    if method.parts[0] == "sections_en" else source for method in methods}
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

    def publication_fixture(self):
        main = PurePosixPath("paper_zh.tex")
        source = b"\n".join(
            b"\\input{" + (sync.DEFAULT_PUBLICATION_DIRECTORY / name).as_posix().encode() + b"}"
            for name in ("default_initial_values.tex", "representative_cases.tex"))
        snapshot = {main: source,
                    PurePosixPath("sections/03_method.tex"): sync.LOCAL_FIGURE_PATH}
        snapshot.update({relative: (relative.name + "\nunchanged bytes\n").encode()
                         for relative in sync.DEFAULT_PUBLICATION_SOURCES})
        for relative, content in snapshot.items():
            path = sync.source_path(self.root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return snapshot

    def ga_publication_fixture(self):
        snapshot = {
            PurePosixPath("paper_zh.tex"): b"\n".join(
                b"\\input{" + (sync.GA_PUBLICATION_DIRECTORY / name).as_posix().encode() + b"}"
                for name in ("ga_main_values.tex", "representative_cases.tex")),
            PurePosixPath("sections/03_method.tex"): sync.LOCAL_FIGURE_PATH,
        }
        snapshot.update({relative: (relative.name + "\naccepted bytes\n").encode()
                         for relative in sync.GA_PUBLICATION_SOURCES})
        for relative, content in snapshot.items():
            path = sync.source_path(self.root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return snapshot

    def test_ga_package_is_discovered_and_exported_without_changing_data(self):
        snapshot = self.ga_publication_fixture()
        listing = "\0".join("IEEE_conference_template/" + str(path)
                             for path in snapshot if path not in sync.GA_PUBLICATION_SOURCES) + "\0"
        with patch.object(sync, "git", return_value=subprocess.CompletedProcess([], 0, listing, "")):
            self.assertEqual(set(sync.local_files(self.root)), set(snapshot))
        result = sync.export_payload(snapshot, b"figure", chinese_only=True)
        for path in sync.GA_PUBLICATION_SOURCES:
            destination = sync.GA_PUBLICATION_DESTINATION / path.name
            if path.suffix == ".tex":
                self.assertEqual(result[destination], snapshot[path])
            else:
                self.assertNotIn(destination, result)
        self.assertNotIn(b"../ZAC_zzx/", result[PurePosixPath("paper_zh.tex")])

    def test_online_readme_uses_overleaf_instructions_without_changing_local_readme(self):
        snapshot = self.ga_publication_fixture()
        readme = PurePosixPath("README.md")
        snapshot[readme] = b"Local make command and ../scripts/paper link"
        result = sync.export_payload(snapshot, b"figure")
        self.assertIn(b"XeLaTeX", result[readme])
        self.assertIn(b"paper_en.tex", result[readme])
        self.assertNotIn(b"../scripts", result[readme])
        self.assertEqual(snapshot[readme], b"Local make command and ../scripts/paper link")
        self.assertFalse(any(path.suffix in {".json", ".csv", ".py"} for path in result))

    def test_ga_standalone_inputs_preserve_suffix_and_unrelated_text(self):
        snapshot = self.ga_publication_fixture()
        for suffix in ("", ".tex"):
            wrapper = PurePosixPath("figures/example" + ("_tex" if suffix else "") + ".tex")
            old = (sync.GA_PUBLICATION_DIRECTORY / ("ga_main_values" + suffix)).as_posix().encode()
            snapshot[wrapper] = b"before\\input{" + old + b"}after"
        original = dict(snapshot)
        result = sync.export_payload(snapshot, b"figure", chinese_only=True)
        for relative in snapshot:
            if relative.parts[0] == "figures":
                self.assertEqual(result[relative], snapshot[relative].replace(
                    sync.GA_PUBLICATION_DIRECTORY.as_posix().encode(),
                    sync.GA_PUBLICATION_DESTINATION.as_posix().encode()))
        self.assertEqual(snapshot, original)

    def test_ga_export_rejects_partial_package_or_missing_main_input(self):
        snapshot = self.ga_publication_fixture()
        for path in sync.GA_PUBLICATION_SOURCES:
            partial = dict(snapshot)
            del partial[path]
            with self.subTest(path=path), self.assertRaisesRegex(sync.PreparationError, "six-file physical-GA"):
                sync.export_payload(partial, b"figure")
        snapshot[PurePosixPath("paper_zh.tex")] = b"\\GAMainZACStrictN"
        with self.assertRaisesRegex(sync.PreparationError, "registered GA input"):
            sync.export_payload(snapshot, b"figure")

    def test_current_package_is_discovered_read_and_remapped_without_byte_changes(self):
        snapshot = self.publication_fixture()
        listing = "\0".join("IEEE_conference_template/" + str(p)
                              for p in snapshot if p not in sync.DEFAULT_PUBLICATION_SOURCES)
        with patch.object(sync, "git", return_value=subprocess.CompletedProcess([], 0, listing, "")):
            files = sync.local_files(self.root)
        self.assertEqual(set(files), set(snapshot))
        self.assertEqual(sync.read_sources(self.root, files), snapshot)
        exported = sync.export_payload(snapshot, b"figure")
        self.assertFalse(any(".." in path.parts for path in exported))
        for relative in sync.DEFAULT_PUBLICATION_SOURCES:
            if relative.suffix == ".tex":
                self.assertEqual(exported[sync.export_path(relative)], snapshot[relative])
            else:
                self.assertNotIn(sync.export_path(relative), exported)
        main = PurePosixPath("paper_zh.tex")
        self.assertNotIn(b"../ZAC_zzx", exported[main])
        self.assertIn(b"\\input{paper_exports/default_initial_v1/default_initial_values.tex}", exported[main])
        self.assertEqual(sync.source_path(self.root, main).read_bytes(), snapshot[main])

    def test_partial_package_or_wrong_external_input_stops_export(self):
        snapshot = self.publication_fixture()
        missing = dict(snapshot)
        missing.pop(sync.DEFAULT_PUBLICATION_DIRECTORY / "mechanism.csv")
        with self.assertRaisesRegex(sync.PreparationError, "complete six-file"):
            sync.export_payload(missing, b"figure")
        for change in (lambda text: text.replace(b"../ZAC_zzx", b"../../ZAC_zzx"),
                       lambda text: text + b"\n" + text):
            changed = dict(snapshot)
            changed[PurePosixPath("paper_zh.tex")] = change(changed[PurePosixPath("paper_zh.tex")])
            with self.assertRaisesRegex(sync.PreparationError, "registered external input"):
                sync.export_payload(changed, b"figure")

    def test_external_sources_must_be_registered_and_not_symlinked(self):
        snapshot = self.publication_fixture()
        with self.assertRaisesRegex(sync.PreparationError, "unregistered external"):
            sync.read_sources(self.root, [PurePosixPath("../ZAC_zzx/unregistered.csv")])
        relative = sync.DEFAULT_PUBLICATION_DIRECTORY / "mechanism.csv"
        source = sync.source_path(self.root, relative)
        source.unlink()
        source.symlink_to(sync.source_path(self.root, PurePosixPath("paper_zh.tex")))
        with self.assertRaisesRegex(sync.PreparationError, "symbolic-link"):
            sync.read_sources(self.root, sorted(snapshot))

    def test_chinese_only_preserves_english_even_when_english_path_is_not_exportable(self):
        snapshot = self.publication_fixture()
        english = {PurePosixPath("paper_en.tex"), PurePosixPath("sections_en/03_method.tex"),
                   PurePosixPath("README_en.md"), PurePosixPath("verify_paper_en.py"),
                   PurePosixPath("writing/test_verify_paper_en.py")}
        for relative in english:
            snapshot[relative] = b"English not synchronized; no valid local figure path"
            source = sync.source_path(self.root, relative)
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(snapshot[relative])
            remote = self.checkout / relative
            remote.parent.mkdir(parents=True, exist_ok=True)
            remote.write_bytes(b"preserved remote English")
        exported = sync.export_payload(snapshot, b"figure", chinese_only=True)
        self.assertFalse(english & set(exported))
        with patch.object(sync, "local_files", return_value=sorted(snapshot)), \
                patch.object(sync, "git", side_effect=self.mock_git):
            sync.copy_export(self.root, self.checkout, sorted(snapshot), snapshot, exported)
        for relative in english:
            self.assertEqual((self.checkout / relative).read_bytes(), b"preserved remote English")
        for relative in sync.DEFAULT_PUBLICATION_SOURCES & sync.PUBLICATION_TEX_SOURCES:
            self.assertEqual((self.checkout / sync.export_path(relative)).read_bytes(), snapshot[relative])

    def test_package_destination_collision_is_rejected(self):
        snapshot = self.publication_fixture()
        snapshot[sync.DEFAULT_PUBLICATION_DESTINATION / "representative_cases.tex"] = b"unrelated local file"
        with self.assertRaisesRegex(sync.PreparationError, "same export destination"):
            sync.export_payload(snapshot, b"figure")

    def test_chinese_only_report_retains_package_provenance(self):
        snapshot = self.publication_fixture()
        snapshot[PurePosixPath("paper_en.tex")] = b"frozen English source"
        output = io.StringIO()
        with patch.object(sync.sys, "argv", ["prepare_overleaf_sync.py", "--check-only",
                                               "--chinese-only", "--expected-remote", self.tip]), \
                patch.object(sync, "local_files", return_value=sorted(snapshot)), \
                patch.object(sync, "read_sources", return_value=snapshot), \
                patch.object(sync, "check_build", return_value=b"figure") as check_build, \
                patch.object(sync, "prepare_checkout", return_value=(self.checkout, self.tip, [])), \
                patch.object(sync, "copy_export") as copy_export, redirect_stdout(output):
            self.assertEqual(sync.main(), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["chinese_only"])
        self.assertEqual(report["english_only_files_preserved"], ["paper_en.tex"])
        self.assertEqual(len(report["external_source_files"]), 6)
        for item in report["external_source_files"]:
            relative = PurePosixPath(item["source"])
            self.assertEqual(item["destination"], sync.export_path(relative).as_posix())
            self.assertEqual(item["sha256"], hashlib.sha256(snapshot[relative]).hexdigest())
        # Chinese-only controls copying, never the full build identity check.
        self.assertEqual(check_build.call_args.args[1], snapshot)
        copy_export.assert_not_called()

    def test_concurrent_package_change_is_detected_after_copy(self):
        snapshot = self.publication_fixture()
        relative = sync.DEFAULT_PUBLICATION_DIRECTORY / "analysis_units.csv"
        original_copy_mode = sync.shutil.copymode

        def change_source(source, destination):
            original_copy_mode(source, destination)
            if Path(destination).name == "representative_cases.tex":
                sync.source_path(self.root, relative).write_bytes(b"author updated the source")

        with patch.object(sync, "local_files", return_value=sorted(snapshot)), \
                patch.object(sync, "git", side_effect=self.mock_git), \
                patch.object(sync.shutil, "copymode", side_effect=change_source):
            with self.assertRaisesRegex(sync.PreparationError, "Local paper changed during export"):
                sync.copy_export(self.root, self.checkout, sorted(snapshot), snapshot,
                                 sync.export_payload(snapshot, b"figure"))
        self.assertEqual(sync.source_path(self.root, relative).read_bytes(), b"author updated the source")

    def test_english_method_without_entry_is_also_adapted(self):
        snapshot = {PurePosixPath("sections/03_method.tex"): sync.LOCAL_FIGURE_PATH,
                    PurePosixPath("sections_en/03_method.tex"): sync.LOCAL_EN_FIGURE_PATH}
        result = sync.export_payload(snapshot, b"figure")
        self.assertEqual(result[PurePosixPath("sections_en/03_method.tex")], sync.OVERLEAF_FIGURE_PATH)

    def test_report_lists_all_actual_scientific_source_adaptations(self):
        for bilingual in (False, True):
            methods = [PurePosixPath("sections/03_method.tex")]
            if bilingual:
                methods.append(PurePosixPath("sections_en/03_method.tex"))
            snapshot = {method: sync.LOCAL_EN_FIGURE_PATH if method.parts[0] == "sections_en"
                        else sync.LOCAL_FIGURE_PATH for method in methods}
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
        self.assertFalse((self.root / "build").exists())

    def test_cloud_latexmkrc_rejects_old_and_new_local_build_paths(self):
        for name in (".latexmkrc", "latexmkrc"):
            for local_path in ("../build/paper_zh", "build/paper_zh", "./build/paper_en",
                               "IEEE_conference_template/build/paper_zh",
                               "build/overleaf-sync"):
                rc = self.checkout / name
                rc.write_text(f"$out_dir = '{local_path}';\n")
                with self.subTest(name=name, local_path=local_path), \
                        patch.object(sync, "git", side_effect=self.mock_git), \
                        self.assertRaisesRegex(sync.PreparationError, "local build path"):
                    sync.prepare_checkout(self.root, self.tip)
                rc.unlink()

    def test_cloud_latexmkrc_without_local_paths_is_preserved(self):
        rc = self.checkout / ".latexmkrc"
        original = "$pdf_mode = 5;\n$max_repeat = 5;\n"
        rc.write_text(original)
        with patch.object(sync, "git", side_effect=self.mock_git):
            sync.prepare_checkout(self.root, self.tip)
        self.assertEqual(rc.read_text(), original)

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
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"original source")
        build = self.root / "IEEE_conference_template/build/paper_zh"
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
            "artifacts": {f"build/paper_zh/{name}": declaration
                          for name, declaration in artifacts.items()},
        }))
        return source, build, snapshot

    def test_verified_build_is_read_from_manuscript_build_only(self):
        _, _, snapshot = self.build_fixture()
        self.assertEqual(sync.check_build(self.root, snapshot), b"verified figure")
        self.assertFalse((self.root / "build").exists())

    def test_build_binds_all_six_external_files_including_csv_and_json(self):
        _, build, _ = self.build_fixture()
        snapshot = self.publication_fixture()
        snapshot[PurePosixPath("paper_en.tex")] = b"English scientific source"
        manifest_path = build / "source_build_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source_sha256"] = {str(path): hashlib.sha256(content).hexdigest()
                                     for path, content in snapshot.items()}
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(sync.check_build(self.root, snapshot), b"verified figure")
        for relative in (*sync.DEFAULT_PUBLICATION_SOURCES, PurePosixPath("paper_en.tex")):
            changed = {**snapshot, relative: snapshot[relative] + b"changed after build"}
            with self.subTest(source=relative), self.assertRaisesRegex(
                    sync.PreparationError, "Source content differs"):
                sync.check_build(self.root, changed)

    def test_legacy_qa_artifact_keys_are_rejected(self):
        _, build, snapshot = self.build_fixture()
        qa_file = build / "final_paper_qa.json"
        qa = json.loads(qa_file.read_text())
        qa["artifacts"] = {f"../{name}": value for name, value in qa["artifacts"].items()}
        qa_file.write_text(json.dumps(qa))
        with self.assertRaisesRegex(sync.PreparationError, "incomplete verified build"):
            sync.check_build(self.root, snapshot)

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
