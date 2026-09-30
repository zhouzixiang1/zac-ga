"""Locations of manuscript sources, tools and non-typesetting support files.

The support paths are explicit: missing files never fall back to old copies.
Frozen experiment manifests retain their historical logical figure filenames.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPER_ROOT = PROJECT_ROOT / "IEEE_conference_template"
SUPPLEMENT_FILES = frozenset({
    "figures/tikz_style_precompact.tex",
    "sections/algorithm_precompact.tex",
    "sections_en/algorithm_precompact.tex",
    "figures/baseline_motivation.tex",
    "figures/physical_lookahead.tex",
    "figures/zair_output.tex",
    "figures/overall_framework_precompact.tex",
    "sections/05_circuit_table.tex",
    "sections_en/05_circuit_table.tex",
    "figures/data/default_initial_qmap_distribution.dat",
    "figures/data/fig6_ablation.dat",
    "figures/data/fig6_mechanism.dat",
    "figures/data/fig6_meta.tex",
    "figures/data/fig6_qmap_fidelity.dat",
    "figures/data/fig6_selected_cases.dat",
    "figures/data/fig6_selected_cases.tex",
    "figures/data/fig6_stage_time.dat",
    "figures/data/fig6_zac_fidelity.dat",
    "figures/data/physical_ga_loss_decomposition.dat",
    "figures/data/physical_ga_main_qmap_distribution.dat",
    "figures/fidelity_distribution_supplement.tex",
    "figures/fidelity_distribution_supplement_standalone.tex",
    "figures/loss_decomposition_supplement.tex",
    "figures/loss_decomposition_supplement_standalone.tex",
    "initial_lookahead_extension_values.tex",
    "initial_lookahead_values.tex",
    "sections/05_sensitivity_table.tex",
    "sections_en/05_sensitivity_table.tex",
})


def support_root(paper_root=PAPER_ROOT):
    return Path(paper_root).parent / "docs/paper"


def artifact_path(paper_root, relative):
    relative = Path(relative)
    if relative.suffix == ".json":
        return support_root(paper_root) / "metadata" / relative
    if relative.as_posix() in SUPPLEMENT_FILES:
        return support_root(paper_root) / "supplementary" / relative
    return Path(paper_root) / relative


def tool_path(paper_root, name):
    return Path(paper_root).parent / "scripts/paper" / Path(name).name


def notes_path(paper_root, name):
    return support_root(paper_root) / "notes" / Path(name).name
