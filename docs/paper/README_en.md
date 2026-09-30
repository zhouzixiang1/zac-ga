# English IEEE manuscript

The active source is [paper_en.tex](../../IEEE_conference_template/paper_en.tex),
with matching Chinese and English files in `sections/` and `sections_en/`.
The September 14 compact revision was synchronized to Overleaf at `23fd2ee`,
after preserving the author's remote keyword update from `d8bdcea`.
The subsequent [introduction and chapter-transition revision](notes/20260914_intro_background.md)
is local: contributions now form one paragraph, SLM/AOD definitions are together
in Section II, and Section III opens with the layer-by-layer compilation process.

The title remains **GA-LK: A Multi-Layer Look-Ahead Compiler for Joint Placement
on Zoned Neutral-Atom Architectures**. Abstracts and each language's keywords
are otherwise unchanged, except that the English abstract uses “AODs” in
place of “acousto-optic deflectors (AODs)”. The author's updated English keywords
use “quantum compiler” and omit “multi-layer look-ahead”. The repository link is retained.

## Argument and figures

The introduction states the coupled placement problem and two contributions.
Background supplies the physical model and directly related compilers.
Section III proceeds from complete candidates to physical prediction, then
search and initialization, and finally executable output. Section IV connects
aggregate gains to their physical sources, tests look-ahead and search, and
examines computational budgets. Section V is one conclusion paragraph.

Four shared vector figures show architecture constraints, the compiler
framework, a complete candidate, and horizon effects. The framework follows
the original circuit, layer and atom-state examples through all six stages.
The horizon plot places two axes side by side within one column, retaining
all ten circuits and original means.
One main-results table and one guiding algorithm accompany the four figures.
Each method section inputs its language's `03_algorithm.tex`, which summarizes
candidate evaluation, search, and final selection. Retired figures, the case
table, and the earlier `algorithm_precompact.tex` remain in
[supplementary/](supplementary/), with original sources in the pre-edit snapshot.

## Evidence and builds

The main results still use the publication package in
[physical_ga_main_v1/paper_exports](../../ZAC_zzx/results/physical_ga_main_v1/paper_exports/):
26.42% maximum geometric-mean fidelity improvement and 25.86% mean batch reduction.
Dynamic controls, initialization studies and timing scopes remain distinct.
No new experiments or changes to accepted data are part of this revision.

Run `make paper-en PYTHON=ZAC/.venv/bin/python` from the repository root.
Each language rebuilds its own standalone framework PDF before its main PDF.
The English target is six body pages and one references page; Chinese pagination
is natural. Both use clean text, IEEE fonts and normal figure colors.

- [English PDF](../../IEEE_conference_template/build/paper_en/paper_en.pdf)
- [English QA](../../IEEE_conference_template/build/paper_en/final_paper_qa.json)
- [Chinese PDF](../../IEEE_conference_template/build/paper_zh/paper_zh.pdf)
- [Change record](notes/20260914_compact_revision.md)
- [Figure correction](notes/20260914_figure_repair.md)
- [Argument and example revision](notes/20260914_story_revision.md)

Checks cover source/build identity, frozen evidence, bilingual citations,
labels, numerical macros and equations, protected abstracts/keywords, physical
figure constraints and rendered layout. The exported sources compile independently
to six Chinese pages and seven English pages; remote files were verified after push.
Online compilation was not triggered because the browser session is not logged in.
See the [sync record](notes/20260914_overleaf_sync.md).
