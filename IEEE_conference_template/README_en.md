# English IEEE manuscript

The maintained English counterpart is [paper_en.tex](paper_en.tex), with section
sources in [sections_en/](sections_en/). It translates the Chinese manuscript
after the Table IV layout revision. It does not replace the Chinese source.

## Correspondence

Each file in `sections_en/` corresponds to the same filename in `sections/`.
The title, author block, abstract, and keywords are in the respective main files.
Both versions use the same six TikZ/PGFPlots figures, numerical macros in
`results_values_zh.tex`, figure data, and `references.bib`. The English tables
reuse the original result macros; no results are re-entered manually.

The [terminology ledger](writing/08_translation_ledger.md) records the chosen
English terms. Translation preserves the sequence of sections, all equations
and labels, citations, benchmark cohorts, circuit-selection conditions, and
compiler-runtime results. Sentence boundaries may differ for readable English.

## Build and review

Run from the ZAC repository root:

```bash
make paper-en
make paper-en-check
make paper-en-preview PYTHON=/path/to/environment/bin/python
make paper-test PYTHON=/path/to/environment/bin/python
```

`paper-en` first verifies the Chinese manuscript and rebuilds the shared
standalone Fig. 3, then compiles and verifies the English version. All generated
files remain under the repository-root `build/`:

| Output | Path |
|---|---|
| English PDF | `build/paper_en/paper_en.pdf` |
| English QA and correspondence report | `build/paper_en/final_paper_qa.json` |
| English contact sheet | `build/paper_en/page_overview.png` |
| English page previews | `build/paper_en/preview/` |
| Chinese PDF | `build/paper_zh/paper_zh.pdf` |

The English manuscript uses the IEEE A4 two-column format and XeLaTeX. Its page
count is checked independently; translated content is not removed to match the
Chinese page count. References begin on the final page. The shared figures
remain editable LaTeX sources; no raster replacements are introduced.

Automatic checks cover source/PDF identity, labels, citations, numerical-macro
usage, displayed equations, shared figure inclusion, and compilation defects.
They supplement, rather than replace, line-by-line review of the translation.
Read each English section beside its Chinese counterpart when revising content.

Overleaf synchronization remains a separate reviewed Git operation. Adding the
English files locally does not change the online main-document selection.
