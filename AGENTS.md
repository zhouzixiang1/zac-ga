# Repository working conventions

- The repository root is this `zac/` directory. Work on `main` unless the user requests another branch.
- The active Chinese IEEE manuscript is `IEEE_conference_template/paper_zh.tex`. Edit this in-repository copy; the desktop `IEEE_conference_template_upgrade` directory and ZIP are retained snapshots, not active editing targets.
- Preserve existing uncommitted changes; the author may edit the manuscript concurrently.
- Use `build/overleaf-sync/` for the Overleaf export and Git checkout. Inspect remote changes before exporting; never force-push or overwrite unmerged online edits. Adapt only the generated figure path for the cloud project and leave the desktop copy unchanged.
- Put newly generated compilation outputs in root `build/`: paper PDF, figure PDF, LaTeX auxiliaries, QA and previews in `build/paper_zh/`; CMake/CTest and wheel outputs in `build/native/`. Use the root Makefile rather than compiling into source directories.
- Do not relocate or delete historical experiment build artifacts, installed environments, or frozen evidence as part of build cleanup. Experimental data are not disposable build output.
- `ZAC_zzx/results/paper_zh_v2/` is the authoritative accepted evidence. Do not rerun experiments, regenerate its data, change baselines or modify frozen native sources for a manuscript-only task.
- Preserve TikZ/PGFPlots sources and frozen `figures/data/` inputs. Figure 3 is built separately before main-paper compilation; a failed figure build must not reuse a stale PDF.
- After manuscript edits, run `make paper` and relevant paper tests, inspect the rendered PDF and check page count, references, overfull boxes and numerical consistency. The accepted layout is eight body pages plus one references page.
- Keep the repository map and manuscript README aligned with actual paths. Distinguish local commits from a verified push; do not claim remote synchronization without checking the remote branch.
