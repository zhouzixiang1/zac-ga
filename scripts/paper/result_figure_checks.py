"""Semantic source guards for the ZAIR interface and measured-result figure.

These checks bind visible objects to their fields or frozen result macros.
Numerical provenance and cohort eligibility remain in the existing data audit.
"""

from __future__ import annotations

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path

import re
import json
import math
from pathlib import Path


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_DISTRIBUTIONS = {
    "figures/data/default_initial_qmap_distribution.dat": r"\DefaultQMAPStrictN",
    "figures/data/physical_ga_main_qmap_distribution.dat": r"\GAMainQMAPStrictN",
}


def _compact(text: str) -> str:
    active = re.sub(r"(?<!\\)%[^\n]*", "", text)
    return re.sub(r"\s+", "", active)


def _node(source: str, name: str) -> str:
    for statement in re.findall(r"\\node\b[^;]*;", source):
        if f"({name})" in statement:
            content = re.search(r"\)\{(.*)\};$", statement)
            return content.group(1) if content else ""
    return ""


def _axis_limits(axis: str, dimension: str) -> tuple[float, float] | None:
    limits = []
    for end in ("min", "max"):
        matches = re.findall(rf"(?:\[|,){dimension}{end}=({_NUMBER})(?=,|\])", axis)
        if len(matches) != 1:
            return None
        limits.append(float(matches[0]))
    return tuple(limits) if all(map(math.isfinite, limits)) and limits[0] < limits[1] else None


def _numeric_ticks(axis: str, key: str) -> list[float]:
    matches = re.findall(rf"(?:\[|,){key}=\{{([^{{}}]*)\}}", axis)
    if len(matches) != 1:
        return []
    labels = [value.strip("$") for value in matches[0].split(",")]
    if not all(re.fullmatch(_NUMBER, value) for value in labels):
        return []
    values = [float(value) for value in labels]
    return values if all(map(math.isfinite, values)) else []


def _read_distribution(relative_path: str) -> dict | None:
    """Read the chosen plotting cohort; the generator audits original results."""
    if relative_path not in _DISTRIBUTIONS:
        return None
    path = artifact_path(PAPER_ROOT, relative_path)
    try:
        lines = [line.split() for line in path.read_text(encoding="utf-8").splitlines()]
        fields = ("rank", "sha", "dlog_vs_zac", "dlog_vs_iccad", "member_N")
        if not lines or tuple(lines[0]) != fields or any(len(line) != len(fields) for line in lines[1:]):
            return None
        rows = [dict(zip(fields, line)) for line in lines[1:]]
        summary = json.loads(artifact_path(PAPER_ROOT, Path(relative_path).with_suffix(".provenance.json")).read_text(encoding="utf-8"))
        count = len(rows)
        if (not count or summary["dataset"] != "qmap154"
                or type(summary["unit_count"]) is not int or summary["unit_count"] != count
                or [int(row["rank"]) for row in rows] != list(range(1, count + 1))
                or len({row["sha"] for row in rows}) != count
                or any(not re.fullmatch(r"[0-9a-f]{64}", row["sha"]) for row in rows)
                or any(int(row["member_N"]) < 1 for row in rows)):
            return None
        columns = {field: [float(row[field]) for row in rows] for field in fields[2:4]}
        if not all(math.isfinite(value) for column in columns.values() for value in column):
            return None
        if [(value, row["sha"]) for value, row in zip(columns["dlog_vs_zac"], rows)] != sorted(
                (value, row["sha"]) for value, row in zip(columns["dlog_vs_zac"], rows)):
            return None
        for field, values in columns.items():
            bounds = summary["comparisons"][field]
            if min(values) != bounds["minimum"] or max(values) != bounds["maximum"]:
                return None
        return {"count": count, "minimum": min(map(min, columns.values())),
                "maximum": max(map(max, columns.values()))}
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None



def _read_horizon_trends() -> dict | None:
    """Read the five normalized configurations used by the horizon figure."""
    path = PAPER_ROOT / "figures/data/horizon_trends.dat"
    fields = ("horizon", *(f"f{i}" for i in range(10)), "f_mean",
              *(f"b{i}" for i in range(10)), "b_mean")
    try:
        lines = [line.split() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        if (not lines or tuple(lines[0]) != fields or len(lines) != 6
                or any(len(line) != len(fields) for line in lines[1:])):
            return None
        rows = [dict(zip(fields, map(float, line))) for line in lines[1:]]
        if ([row["horizon"] for row in rows] != [0, 1, 2, 4, 8]
                or any(not math.isfinite(value) for row in rows for value in row.values())
                or any(row[field] <= 0 for row in rows for field in fields[1:])
                or any(not math.isclose(rows[-1][field], 1, rel_tol=0, abs_tol=1e-10)
                       for field in fields[1:])):
            return None
        bounds = {}
        for metric in ("f", "b"):
            for row in rows:
                geometric_mean = math.exp(sum(math.log(row[f"{metric}{i}"])
                                               for i in range(10)) / 10)
                if not math.isclose(row[f"{metric}_mean"], geometric_mean,
                                    rel_tol=1e-8, abs_tol=1e-10):
                    return None
            values = [row[field] for row in rows
                      for field in (*[f"{metric}{i}" for i in range(10)], f"{metric}_mean")]
            bounds[metric] = (min(values), max(values))
        return {"rows": rows, "bounds": bounds}
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None

def _plots(axis: str) -> list[tuple[str, str]]:
    return re.findall(r"\\addplot\[([^;]*?)\]coordinates\{([^;]*?)\};", axis)


def _zair(source: str) -> dict[str, bool]:
    """Read ZAIR record productions, independent of typography and panel count."""
    # Syntax colours do not alter record names or field types. Revision marks
    # may already have been removed by the central verifier; leaving an outer
    # PaperRevision wrapper here does not affect these literal productions.
    plain = re.sub(r"\\textcolor\{[^{}]+\}\{([^{}]*)\}", r"\1", source)
    plain = plain.replace(r"\_", "_")
    records = re.findall(r"<([A-Za-z0-9]+)>::=\\\{(.*?)\\\}", plain)
    expected = {
        "init": {"init_locs": "list[qloc]"},
        "1qGate": {"unitary": "'u3'", "locs": "list[qloc]", "gates": "list[gate]"},
        "rydberg": {"zone_id": "int", "gates": "list[gate]"},
        "rearrangeJob": {"aod_id": "int", "aod_qubits": "list[int]",
                         "begin_locs": "list[qloc]", "end_locs": "list[qloc]",
                         "insts": "list[activate|move|deactivate]"},
        "activate": {"row_id": "list[int]", "row_y": "list[float]",
                     "col_id": "list[int]", "col_x": "list[float]"},
        "deactivate": {"row_id": "list[int]", "col_id": "list[int]"},
        "move": {"row_id": "list[int]", "row_y_begin": "list[float]",
                 "row_y_end": "list[float]", "col_id": "list[int]",
                 "col_x_begin": "list[float]", "col_x_end": "list[float]"},
    }
    parsed = {}
    valid_syntax = True
    field_pattern = r"'([A-Za-z_]+)':(list\[[^\]]+\]|[A-Za-z]+|'[^']+')"
    for kind, body in records:
        fields = re.findall(field_pattern, body)
        parsed[kind] = dict(fields)
        # Detect duplicate keys, malformed fields and missing separators as
        # well as semantic type changes. TeX line breaks are merely layout.
        body = re.sub(r"\\\\(?:\[[^\]]*\])?", "", body).replace(r"\quad", "")
        syntax = ",".join(f"'{key}':{value}" for key, value in fields)
        valid_syntax &= len(fields) == len(parsed[kind]) and body == syntax

    def typed(kind, field):
        return parsed.get(kind, {}).get(field) == expected[kind][field]

    operation_kinds = {"activate", "move", "deactivate"}
    insts = parsed.get("rearrangeJob", {}).get("insts", "")
    alternatives = re.fullmatch(r"list\[([^\]]+)\]", insts)
    alternatives = alternatives.group(1).split("|") if alternatives else []
    return {
        "zair_instruction_types_are_named": len(records) == len(expected)
            and set(parsed) == set(expected),
        "zair_fields_belong_to_named_interfaces": all(
            set(parsed.get(kind, {})) == set(fields) for kind, fields in expected.items()),
        "zair_format_uses_record_productions": bool(records) and valid_syntax,
        "zair_scalar_and_gate_types_match_output": all(typed(kind, field) for kind, field in (
            ("1qGate", "unitary"), ("1qGate", "gates"),
            ("rydberg", "zone_id"), ("rydberg", "gates"),
            ("rearrangeJob", "aod_id"), ("rearrangeJob", "aod_qubits"))),
        "zair_locations_retain_qubit_and_trap_records": all(typed(kind, field) for kind, field in (
            ("init", "init_locs"), ("1qGate", "locs"),
            ("rearrangeJob", "begin_locs"), ("rearrangeJob", "end_locs"))),
        "zair_aod_ids_and_coordinates_have_correct_types": all(
            typed(kind, field) for kind in operation_kinds for field in expected[kind]),
        "zair_rearrangement_contains_expanded_operations": len(alternatives) == 3
            and set(alternatives) == operation_kinds
            and operation_kinds <= set(parsed)
            and all("insts" not in fields for kind, fields in parsed.items()
                    if kind != "rearrangeJob"),
    }


def _results(source: str) -> dict[str, bool]:
    """Guard the selected paired distribution without recomputing experiments."""
    axes = re.findall(r"\\begin\{axis\}(.*?)\\end\{axis\}", source, re.S)
    axis = axes[0] if len(axes) == 1 else ""
    table_plots = re.findall(
        r"\\addplot\[([^;]*?)\]table\[([^;]*?)\]\{([^;]*?)\};", axis)
    selected_path = table_plots[0][2] if table_plots else ""
    expected_macro = _DISTRIBUTIONS.get(selected_path)
    evidence = _read_distribution(selected_path)
    expected_columns = ("dlog_vs_zac", "dlog_vs_iccad")
    paired = expected_macro is not None and len(table_plots) == 2 and all(
        options == f"x=rank,y={column},colsep=space" and path == selected_path
        for (_, options, path), column in zip(table_plots, expected_columns))
    xlimits, ylimits = (_axis_limits(axis, dimension) for dimension in "xy")
    xticks = _numeric_ticks(axis, "xtick")
    yticks, ylabels = (_numeric_ticks(axis, key) for key in ("ytick", "yticklabels"))
    count = evidence["count"] if evidence else None
    full_range = bool(evidence and ylimits and ylimits[0] <= min(0, evidence["minimum"])
                      and ylimits[1] >= max(0, evidence["maximum"]))
    cohort_label = ("MQTQMAPexamples:$N=" + expected_macro + "$circuits"
                    if expected_macro else "")
    forward = (r"ycoordtrafo/.code={\pgfmathparse{sign(#1)*(abs(#1)<=0.01"
               r"?abs(#1)/0.01:1+ln(max(abs(#1),0.01)/0.01)/ln(10))}}")
    inverse = (r"ycoordinvtrafo/.code={\pgfmathparse{sign(#1)*(abs(#1)<=1"
               r"?abs(#1)*0.01:0.01*10^(abs(#1)-1))}}")
    numeric_filter = any(token in source for token in (
        "restrict", "filter", "domain=", "yexpr", "xexpr", "skipcoords", "unboundedcoords",
        "eachnthpoint", "selectcoordsbetweenindex", "rowpredicate", "discard"))
    zero_lines = re.findall(
        rf"\\draw\[[^;]*?\]\(axiscs:({_NUMBER}),({_NUMBER})\)--"
        rf"\(axiscs:({_NUMBER}),({_NUMBER})\);", axis)
    zero_line = bool(xlimits and any(
        tuple(map(float, values)) == (xlimits[0], 0, xlimits[1], 0) for values in zero_lines))
    signed_ticks = bool(yticks and yticks == sorted(set(yticks)) and yticks == ylabels
                        and 0 in yticks and ylimits
                        and all(ylimits[0] <= value <= ylimits[1] for value in yticks)
                        and evidence
                        and (evidence["minimum"] >= 0 or min(yticks) < 0)
                        and (evidence["maximum"] <= 0 or max(yticks) > 0))
    return {
        "distribution_data_matches_selected_frozen_summary": evidence is not None,
        "distribution_has_one_evidence_axis": len(axes) == 1
            and "name=fidelityDistributionAxis," in axis
            and len(re.findall(r"\\addplot\b", source)) == 2
            and not any(macro in source for macro in (
                r"\Ablation", r"\MechanismQFT", r"\FigSix", r"\DefaultZAC", r"\GAMainZAC")),
        "distribution_uses_current_common_circuit_cohort": bool(evidence and paired
            and cohort_label in source and xlimits == (.5, count + .5)
            and xticks == sorted(set(xticks)) and 1 in xticks and count in xticks
            and all(value.is_integer() and 1 <= value <= count for value in xticks)),
        "distribution_pairs_baselines_on_shared_circuit_rank": paired
            and r"ascending$\Delta\lnF$vs.ZAC" in source,
        "distribution_points_have_distinct_markers_without_lines": paired
            and all("onlymarks" in style for style, _, _ in table_plots)
            and "mark=*," in table_plots[0][0] and "mark=triangle," in table_plots[1][0]
            and not any(token in style for style, _, _ in table_plots
                        for token in ("ybar", "stack", "smooth", "sharpplot")),
        "distribution_preserves_all_signed_values_and_extremes": paired
            and full_range and not numeric_filter and "clip=false" in axis,
        "distribution_uses_explicit_continuous_symmetric_log_scale":
            forward in axis and inverse in axis and "ymode=log" not in axis
            and signed_ticks
            and r"Linearscalewithin$|\Delta\lnF|\le0.01$;logarithmicoutside." in source,
        "distribution_has_true_zero_line_and_signed_direction": zero_line
            and "Higherfidelity" in source and "Lowerfidelity" in source
            and r"$\Delta\lnF$(symmetriclogscale)" in axis,
        "distribution_identifies_each_baseline": "vs.ZAC" in source
            and "vs.ICCAD/QMAP" in source,
    }


def _horizon_results(source: str) -> dict[str, bool]:
    """Keep both metrics tied to all normalized circuit and mean series."""
    evidence = _read_horizon_trends()
    axes = re.findall(r"\\begin\{axis\}(.*?)\\end\{axis\}", source, re.S)
    style = re.search(r"\\pgfplotsset\{horizontrendaxis/\.style=\{(.*?)\}\}\\begin\{axis\}", source)
    common_options = style.group(1) if style else ""
    data_binding = (r"\pgfplotstableread[colsep=space]"
                    r"{figures/data/horizon_trends.dat}\HorizonTrendData" in source)
    paired = len(axes) == 2 and data_binding
    spacing = len(axes) == 2
    extents = len(axes) == 2 and evidence is not None
    distinct_lines = len(axes) == 2
    no_transform = not any(token in source for token in (
        "coordtrafo", "mode=log", "symbolic", "restrict", "filter", "discard",
        "skipcoords", "eachnthpoint", "selectcoords", "rowpredicate", "yexpr", "xexpr"))
    for axis, metric, colour in zip(axes, ("f", "b"), ("galkData", "galkData")):
        plots = re.findall(
            r"\\addplot\[([^;]*?)\]table\[([^;]*?)\]\{([^;]*?)\};", axis)
        series = [(options, path) for _, options, path in plots]
        paired &= (series == [(f"x=horizon,y={metric}#1", r"\HorizonTrendData"),
                              (f"x=horizon,y={metric}_mean", r"\HorizonTrendData")]
                   and r"\pgfplotsinvokeforeach{0,...,9}" in axis)
        options = "[" + common_options + "," + axis
        xlimits, ylimits = (_axis_limits(options, dimension) for dimension in "xy")
        spacing &= bool(xlimits and xlimits[0] <= 0 and xlimits[1] >= 8
                        and _numeric_ticks(options, "xtick") == [0, 1, 2, 4, 8])
        values = evidence["bounds"][metric] if evidence else None
        reference = any(coordinates == "(0,1)(8,1)" for _, coordinates in _plots(axis))
        extents &= bool(values and ylimits and ylimits[0] <= min(1, values[0])
                        and ylimits[1] >= max(1, values[1])
                        and reference)
        widths = [re.search(rf"linewidth=({_NUMBER})pt", style) for style, _, _ in plots]
        distinct_lines &= bool(len(plots) == 2 and all(widths)
            and float(widths[0].group(1)) < float(widths[1].group(1))
            and "galkMuted" in plots[0][0] and colour in plots[1][0]
            and not any(token in style for style, _, _ in plots
                        for token in ("onlymarks", "smooth", "ybar", "stack")))
    return {
        "horizon_data_has_five_configs_and_ten_normalized_circuits": evidence is not None,
        "horizon_pairs_all_circuits_and_each_geometric_mean": bool(paired),
        "horizon_uses_actual_numeric_configuration_spacing": bool(spacing and no_transform),
        "horizon_preserves_all_ratios_and_h8_reference": bool(extents and no_transform),
        "horizon_distinguishes_individual_and_mean_trends": bool(distinct_lines)
            and "Fidelity" in source and "Batches" in source
            and "Geometricmean" in source and not re.search(r"\([ab]\)", source),
    }

def _loss_decomposition(source: str) -> dict[str, bool]:
    """Bind additive physical components and net marks to the same four rows.

    The derivation audit checks median-run selection and exact additivity. This
    contract only guards the meaning and signed extent of the plotted objects.
    """
    path = "figures/data/physical_ga_loss_decomposition.dat"
    expected_rows = [("zac18", "M1"), ("zac18", "M2"),
                     ("qmap154", "M1"), ("qmap154", "M2")]
    columns = ("transfer", "excitation", "coherence", "net")
    evidence = None
    try:
        lines = [line.split() for line in artifact_path(PAPER_ROOT, path).read_text().splitlines()]
        header = lines[0]
        rows = [dict(zip(header, line)) for line in lines[1:]]
        values = [{field: float(row[field]) for field in columns} for row in rows]
        if (len(set(header)) == len(header)
                and all(len(line) == len(header) for line in lines[1:])
                and [(row["dataset"], row["baseline"]) for row in rows] == expected_rows
                and all(math.isfinite(value) for row in values for value in row.values())
                and all(row["transfer"] >= 0 and row["coherence"] >= 0
                        and row["excitation"] <= 0 for row in values)):
            evidence = values
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    axes = re.findall(r"\\begin\{axis\}(.*?)\\end\{axis\}", source, re.S)
    axis = axes[0] if len(axes) == 1 else ""
    bindings = all(
        rf"\pgfplotstablegetelem{{#1}}{{{field}}}\of\LossData"
        rf"\let\Loss{field.title()}\pgfplotsretval" in axis for field in columns)
    row_order = (r"\pgfplotsinvokeforeach{0,...,3}" in axis
                 and r"\pgfmathsetmacro{\LossRow}{4-#1}" in axis)
    plots = re.findall(r"\\addplot\[([^;]*?)\]table\[([^;]*?)\]\{([^;]*?)\};", axis)
    net_plot = len(plots) == 1 and plots[0][1:] == (
        r"x=net,yexpr=4-\coordindex,colsep=space", path)
    net_marker = net_plot and "onlymarks" in plots[0][0] and "mark=diamond*," in plots[0][0]
    rectangles = re.findall(
        r"\\draw\[([^;]*?)\]\(axiscs:([^;]*?)\)rectangle\(axiscs:([^;]*?)\);", axis)
    expected_rectangles = (
        ("galkTail", r"\LossExcitation,\LossLow", r"0,\LossHigh"),
        ("galkData", r"0,\LossLow", r"\LossTransfer,\LossHigh"),
        ("galkGood", r"\LossTransfer,\LossLow", r"\LossPositive,\LossHigh"),
    )
    signed_stack = (len(rectangles) == 3
                    and all(any(f"fill={colour}" in style and start == left and end == right
                                for style, start, end in rectangles)
                            for colour, left, right in expected_rectangles)
                    and r"\pgfmathsetmacro{\LossPositive}{\LossTransfer+\LossCoherence}" in axis)
    xlimits, ylimits = (_axis_limits(axis, dimension) for dimension in "xy")
    ticks = _numeric_ticks(axis, "xtick")
    full_range = bool(evidence and xlimits and ylimits
        and xlimits[0] <= min(0, *(row["excitation"] for row in evidence),
                             *(row["net"] for row in evidence))
        and xlimits[1] >= max(0, *(row["transfer"] + row["coherence"] for row in evidence),
                             *(row["net"] for row in evidence))
        and ylimits[0] <= .82 and ylimits[1] >= 4.18
        and ticks and min(ticks) < 0 < max(ticks) and 0 in ticks
        and all(xlimits[0] <= tick <= xlimits[1] for tick in ticks))
    return {
        "decomposition_has_four_named_comparisons": evidence is not None
            and len(axes) == 1 and "name=lossDecompositionAxis," in axis
            and "ZACbenchmarks" in source and "MQTQMAPexamples" in source
            and "yticklabels={ICCAD/QMAP,ZAC,ICCAD/QMAP,ZAC}" in axis,
        "decomposition_components_bind_to_ordered_data_rows": bindings and row_order
            and rf"\pgfplotstableread[colsep=space]{{{path}}}\LossData" in source,
        "decomposition_net_marks_use_net_values": bindings and net_marker
            and len(re.findall(r"\\addplot\b", source)) == 1
            and r"\pgfmathprintnumber[fixed,precision=3,zerofill]{\LossNet}" in axis,
        "decomposition_stacks_signed_physical_components": bindings and signed_stack,
        "decomposition_preserves_negative_and_positive_extents": full_range
            and "clip=false" in axis
            and not any(token in source for token in (
                "coordtrafo", "xmode=log", "restrict", "filter", "discard", "skipcoords")),
        "decomposition_labels_additive_log_fidelity_change":
            r"Meancontributionto$\Delta\lnF$" in axis
            and all(label in source for label in (
                "Traptransfer", "Idleexcitation", "Coherence", "Netchange")),
    }


def checks(name: str, active: str) -> dict[str, bool]:
    """Return figure contracts, reading selected frozen plot data without writes."""
    source = _compact(active)
    if name == "zair_output.tex":
        return _zair(source)
    if name == "experimental_summary.tex":
        return _horizon_results(source)
    if name == "loss_decomposition_supplement.tex":
        return _loss_decomposition(source)
    if name == "fidelity_distribution_supplement.tex":
        return _results(source)
    return {}
