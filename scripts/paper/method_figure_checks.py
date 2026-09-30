"""Semantic guards for the reconstructed method figures (3, 4 and 5).

The gate and atom checks read the data that draw the example. Routing safety is
provided by the existing coordinate replay, not replaced by wording checks.
"""

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path
from bisect import bisect_right
from collections import Counter
import re


def _unmark(text):
    """Unwrap review colour without deleting nested TeX groups."""
    token = r"\PaperRevision{"
    while token in text:
        start = text.index(token)
        body = start + len(token)
        depth, end = 1, body
        while end < len(text) and depth:
            depth += (text[end] == "{") - (text[end] == "}")
            end += 1
        if depth:
            return text
        text = text[:start] + text[body:end - 1] + text[end:]
    return text


def _rows(text, name):
    match = re.search(r"\\def\\" + name + r"\{([^{}]*)\}", text)
    if not match:
        return []
    try:
        return [tuple(float(item) for item in row.split("/"))
                for row in match[1].split(",")]
    except ValueError:
        return []


def _state(text, name):
    rows = _rows(text, name)
    if not rows or any(len(row) != 3 for row in rows):
        return {}
    # Reject duplicate identities before constructing a dictionary.
    return {int(q): (x, y) for q, x, y in rows} if len({r[0] for r in rows}) == len(rows) else {}


def _five_atom_states(text):
    states = [_state(text, name) for name in ("routeInitial", "routeFinal")]
    return all(set(s) == set(range(5)) and len(set(s.values())) == 5 for s in states)


def _poststate_pairing(text):
    initial, final = (_state(text, name) for name in ("routeInitial", "routeFinal"))
    if not _five_atom_states(text):
        return False
    return (final[0] == initial[0] and final[2] == initial[1]
            and final[1] == initial[2] and final[4] == initial[3]
            and final[3][1] < initial[3][1])


def _circuit_events(text):
    """Extract the drawn input/scheduled gates, retaining each atom identity."""
    match = re.search(r"\\foreach\s+\\qq/\\yy\s+in\s*\{([0-4]/[\d.,/]+)\}", text)
    if not match:
        return [], []
    try:
        wires = {float(y): int(q) for q, y in (row.split("/") for row in match[1].split(","))}
        events = []
        for x, y1, y2 in re.findall(r"\\czGate\{([\d.]+)\}\{([\d.]+)\}\{([\d.]+)\}", text):
            events.append((float(x), "CZ", (wires[float(y1)], wires[float(y2)])))
        for x, y, gate in re.findall(
                r"\\node\[gate\]\s+at\s*\(([\d.]+),([\d.]+)\)\s*\{(RY|RZ|U|V|\$U_3\$)\}", text):
            events.append((float(x), gate, (wires[float(y)],)))
    except (ValueError, KeyError):
        return [], []
    # The two wire spans are the scene boundary, independent of gate columns.
    spans = re.findall(r"\\draw\[wire\]\s*\(([\d.]+),\\yy\)--\(([\d.]+),\\yy\)", text)
    if len(spans) != 2:
        return [], []
    return tuple(sorted((event for event in events if float(lo) <= event[0] <= float(hi)))
                 for lo, hi in spans)


def _nodes(text):
    """Read named boxes without depending on any particular page coordinates."""
    return {name: (float(x), float(y), options) for options, name, x, y in re.findall(
        r"\\node\[([^\]]*)\]\s*\((\w+)\)\s*at\s*\(([\d.]+),([\d.]+)\)", text)}


def _dimension(options, key):
    value = re.search(re.escape(key) + r"\s*=\s*([\d.]+)cm", options)
    return float(value[1]) if value else 0


def _inside(nodes, child, owner, default_height=0):
    if child not in nodes or owner not in nodes:
        return False
    x, y, _ = nodes[child]
    ox, oy, options = nodes[owner]
    width = _dimension(options, "minimum width")
    height = _dimension(options, "minimum height") or default_height
    return width > 0 and height > 0 and abs(x - ox) < width / 2 and abs(y - oy) < height / 2


def _path(compact, source, target, style):
    """Require an actual styled edge, allowing harmless intermediate bends."""
    return bool(re.search(r"\\draw\[" + re.escape(style) + r"\]"
                         + re.escape(source) + r"[^;]*" + re.escape(target) + ";", compact))


def _poststate_to_future(text, compact, nodes):
    """The forecast edge must leave the final snapshot and enter its future box."""
    state = re.search(r"\\coordinate\s*\(stateAfter\)\s*at\s*\(([\d.]+),([\d.]+)\)", text)
    edge = re.search(r"\\draw\[feedback\]\(stateAfter\)--\(([\d.]+),([\d.]+)\);", compact)
    if not state or not edge or not {"currentEval", "stateBefore", "future"} <= nodes.keys():
        return False
    sx, sy = map(float, state.groups())
    ex, ey = map(float, edge.groups())
    cx, cy, current = nodes["currentEval"]
    fx, fy, future = nodes["future"]
    current_width = _dimension(current, "minimum width")
    current_height = _dimension(current, "minimum height")
    future_width = _dimension(future, "text width")
    future_height = _dimension(future, "minimum height")
    return (nodes["stateBefore"][0] < cx < sx < cx + current_width / 2
            and abs(sy - cy) < current_height / 2
            and abs(ex - fx) < future_width / 2
            and abs(ey - (fy + future_height / 2)) < .05)


def checks(name, active, route_checks):
    """Return relationship checks; route_checks is the unchanged replay result."""
    text = _unmark(active)
    compact = re.sub(r"\s+", "", text)
    if name == "overall_framework.tex":
        inputs, scheduled = _circuit_events(text)
        expected = Counter({("CZ", (0, 1)): 1, ("CZ", (2, 3)): 1,
                            ("CZ", (0, 2)): 1, ("CZ", (1, 4)): 1,
                            ("$U_3$", (0,)): 1, ("$U_3$", (3,)): 2,
                            ("$U_3$", (1,)): 1, ("$U_3$", (4,)): 1,
                            ("$U_3$", (2,)): 1})
        multiset = lambda events: Counter((gate, atoms) for _, gate, atoms in events)
        per_atom = lambda events, q: [(gate, atoms) for _, gate, atoms in events if q in atoms]
        dividers = re.search(r"\\foreach\s+\\xx\s+in\s*\{([\d.,]+)\}\s*"
                             r"\\draw\[divider,densely dashed\]", text)
        layers = [Counter() for _ in range(5)]
        if dividers:
            boundaries = [float(x) for x in dividers[1].split(",")]
            if len(boundaries) == 4 and boundaries == sorted(boundaries):
                for x, gate, atoms in scheduled:
                    layers[bisect_right(boundaries, x)][gate, atoms] += 1
        expected_layers = [Counter({("$U_3$", (0,)): 1, ("$U_3$", (3,)): 1, ("$U_3$", (4,)): 1}),
                           Counter({("CZ", (0, 1)): 1, ("CZ", (2, 3)): 1}),
                           Counter({("$U_3$", (1,)): 1, ("$U_3$", (3,)): 1}),
                           Counter({("CZ", (0, 2)): 1, ("CZ", (1, 4)): 1}),
                           Counter({("$U_3$", (2,)): 1})]
        nodes = _nodes(text)
        stages = ("input", "schedule", "initial", "joint", "routing", "output")
        stage_height = re.search(r"stage/\.style=\{[^}]*minimum height=([\d.]+)cm", text)
        height = float(stage_height[1]) if stage_height else 0
        before, after = (_state(text, key) for key in ("candidateBefore", "candidateAfter"))
        permutations = [_rows(text, key) for key in ("initPermOne", "initPermTwo", "initPermThree")]
        instruction_rows = re.search(r"\\foreach\s+\\yy/\\txt\s+in\s*\{([^{}]+)\}", text)
        instructions = ([row.strip().split("/")[1] for row in instruction_rows[1].split(",")]
                        if instruction_rows else [])
        return {
            "six_stage_compilation_order": all(token in compact.replace(r"\\", "") for token in (
                "input/schedule,schedule/initial,initial/joint,joint/routing,routing/output",
                "Input", "Architecture", "Gatescheduling", "Initialplacement",
                "Jointoptimization", "Routing", "ZAIR"))
                and all(stage in nodes for stage in stages)
                and all(nodes[a][0] < nodes[b][0] for a, b in zip(stages, stages[1:]))
                and _path(compact, r"(\from.east|-flowRow)", r"(\to.west|-flowRow)", "flow"),
            "framework_joint_owns_candidate_evaluation": all(
                _inside(nodes, child, "joint", height) for child in ("jointSearch", "currentEval", "future", "candidateScore")),
            "initialization_selects_starting_state": all(
                _inside(nodes, child, "initial", height)
                for child in ("initialCandidates", "initialEval", "initialMap", "initialState"))
                and all(_path(compact, "(" + a + ".south)", "(" + b + ".north)", "flow")
                        for a, b in (("initialCandidates", "initialEval"),
                                     ("initialEval", "initialMap"), ("initialMap", "initialState")))
                and r"\min_\piJ_{\rminit}(\pi)" in compact and r"\mathbf{s}_0" in text,
            "initial_layouts_are_permutations_on_shared_sites": len(permutations) == 3 and all(
                len(rows) == 5 and {row[0] for row in rows} == set(range(5))
                and {row[1] for row in rows} == set(range(5)) for rows in permutations),
            "initial_permutations_drive_drawing": all(
                re.search(r"\\layout\{[\d.]+\}\{\\" + macro + r"\}", text)
                for macro in ("initPermOne", "initPermTwo", "initPermThree"))
                and r"\foreach\qq/\iin#2" in compact,
            "symmetric_cz_macro": all(token in compact for token in (
                r"\draw[wire](#1,#2)--(#1,#3)", r"\fill[galkInk](#1,#2)circle(1.15pt)",
                r"\fill[galkInk](#1,#3)circle(1.15pt)")),
            "rebased_cz_u3_gate_set": {gate for _, gate, _ in inputs + scheduled} == {"CZ", "$U_3$"},
            "same_gate_multiset": multiset(inputs) == multiset(scheduled) == expected,
            "per_atom_dependency_order": bool(inputs) and all(
                per_atom(inputs, q) == per_atom(scheduled, q) for q in range(5)),
            "scheduled_gate_layers": layers == expected_layers and all(
                label in text for label in ("$1Q_0$", "$2Q_0$", "$1Q_1$", "$2Q_1$", "$1Q_2$")),
            "generic_candidate_conserves_five_atoms": all(
                set(state) == set(range(5)) and len(set(state.values())) == 5 for state in (before, after)),
            "generic_candidate_states_drive_drawing": all(
                re.search(r"\\stateCard\{[\d.]+\}\{\\" + macro + r"\}", text)
                for macro in ("candidateBefore", "candidateAfter"))
                and r"\foreach\qq/\xx/\yyin#2" in compact,
            "general_layer_does_not_claim_terminal_future": all(token in text for token in (
                r"\mathbf{s}_\ell", r"\mathbf{s}_{\ell+1}", r"\widehat{\mathbf{s}}_{\ell+2}",
                r"\widehat{\mathbf{s}}_{\ell+3}", r"Commit $L_\ell$"))
                and "Example: evaluate" not in text and "If layers remain" not in text,
            "lookahead_starts_at_candidate_poststate": _poststate_to_future(text, compact, nodes)
                and all(_path(compact, "(" + a + ".east)", "(" + b + ".west)", "feedback")
                        for a, b in (("futureOne", "futureTwo"), ("futureTwo", "futureMore"))),
            "candidate_evaluation_and_score_feedback": all(token in text for token in (
                r"Candidate $(\mathbf{x}_\ell,a_\ell)$", "Joint encoding", "Matching", r"Score $J_\ell$"))
                and _path(compact, "(jointSearch.south)", "(currentEval.north)", "flow")
                and _path(compact, "(currentLoss.south)", "(candidateScore.west)", "flow")
                and _path(compact, "(future.south)", "(candidateScore.north)", "feedback")
                and _path(compact, "(candidateScore.south)", "(jointSearch.west)", "feedback"),
            "execution_updates_next_layer_state": all(
                _inside(nodes, child, "routing", height)
                for child in ("selectedPlan", "routeBatches", "executeCurrent", "stateUpdate"))
                and all(_path(compact, "(" + a + ".south)", "(" + b + ".north)", "flow")
                        for a, b in (("selectedPlan", "routeBatches"), ("routeBatches", "executeCurrent"),
                                     ("executeCurrent", "stateUpdate")))
                and _path(compact, "(stateUpdate.south)", "(joint.west|-flowRow)", "flow")
                and "2Q+1Q" in compact and "Nextlayer:actualstate" in compact,
            "zair_stream_preserves_execution_order": instructions == [
                "init", "1qGate", "rearrangeJob", "rydberg", "1qGate"] and r"\vdots" in text,
        }
    if name == "overall_framework_precompact.tex":
        inputs, scheduled = _circuit_events(text)
        expected = Counter({("CZ", (0, 1)): 1, ("CZ", (2, 3)): 1,
                            ("CZ", (0, 2)): 1, ("CZ", (1, 4)): 1,
                            ("RY", (0,)): 1, ("RZ", (3,)): 1,
                            ("U", (1,)): 1, ("RY", (4,)): 1,
                            ("RZ", (2,)): 1, ("U", (3,)): 1})
        multiset = lambda events: Counter((gate, atoms) for _, gate, atoms in events)
        per_atom = lambda events, q: [(gate, atoms) for _, gate, atoms in events if q in atoms]
        dividers = re.search(r"\\foreach\s+\\xx\s+in\s*\{([\d.,]+)\}\s*"
                             r"\\draw\[divider,densely dashed\]", text)
        layers = [Counter() for _ in range(5)]
        if dividers:
            boundaries = [float(x) for x in dividers[1].split(",")]
            if len(boundaries) == 4 and boundaries == sorted(boundaries):
                for x, gate, atoms in scheduled:
                    layers[bisect_right(boundaries, x)][gate, atoms] += 1
        expected_layers = [Counter({("RY", (0,)): 1, ("RZ", (3,)): 1, ("RY", (4,)): 1}),
                           Counter({("CZ", (0, 1)): 1, ("CZ", (2, 3)): 1}),
                           Counter({("U", (1,)): 1, ("U", (3,)): 1}),
                           Counter({("CZ", (0, 2)): 1, ("CZ", (1, 4)): 1}),
                           Counter({("RZ", (2,)): 1})]
        nodes = _nodes(text)
        stages = ("input", "schedule", "initial", "joint", "routing", "output")
        stage_height = re.search(r"stage/\.style=\{[^}]*minimum height=([\d.]+)cm", text)
        height = float(stage_height[1]) if stage_height else 0
        instruction_rows = re.search(
            r"\\foreach\s+\\yy/\\txt\s+in\s*\{([^{}]+)\}", text)
        instructions = ([row.strip().split("/")[1] for row in instruction_rows[1].split(",")]
                        if instruction_rows else [])
        return {
            "six_stage_compilation_order": all(token in compact.replace(r"\\", "") for token in (
                "input/schedule,schedule/initial,initial/joint,joint/routing,routing/output",
                "Input", "Architecture", "Gatescheduling", "Initialplacement",
                "Jointoptimization", "Routing", "ZAIR"))
                and all(stage in nodes for stage in stages)
                and all(nodes[a][0] < nodes[b][0] for a, b in zip(stages, stages[1:]))
                and _path(compact, r"(\from.east|-flowRow)", r"(\to.west|-flowRow)", "flow"),
            "framework_joint_owns_candidate_evaluation": all(
                _inside(nodes, child, "joint", height) for child in ("jointSearch", "currentEval", "future")),
            "initialization_selects_starting_state": all(
                _inside(nodes, child, "initial", height)
                for child in ("initialCandidates", "initialEval", "initialMap", "initialState"))
                and all(_path(compact, "(" + a + ".south)", "(" + b + ".north)", "flow")
                        for a, b in (("initialCandidates", "initialEval"),
                                     ("initialEval", "initialMap"), ("initialMap", "initialState")))
                and r"Startingstate\\\mathbf{s}_0" in compact.replace("$", ""),
            "boxed_cz_macro": all(token in compact for token in (
                r"\draw[wire](#1,#2)--(#1,#3)", r"\fill[galkInk](#1,#2)circle(.8pt)",
                r"\node[gate]at(#1,#3){Z}")),
            "boxed_u_no_v": sum(gate == "U" for _, gate, _ in inputs + scheduled) == 4
                and not re.search(r"\{V\}", text),
            "same_gate_multiset": multiset(inputs) == multiset(scheduled) == expected,
            "per_atom_dependency_order": bool(inputs) and all(
                per_atom(inputs, q) == per_atom(scheduled, q) for q in range(5)),
            "scheduled_gate_layers": layers == expected_layers and all(
                label in text for label in ("$1Q_0$", "$2Q_0$", "$1Q_1$", "$2Q_1$", "$1Q_2$")),
            "framework_five_atom_configurations": _five_atom_states(text),
            "framework_poststate_matches_second_layer": _poststate_pairing(text),
            "framework_states_drive_drawing": all(
                r"\foreach\qq/\xx/\yyin" + "\\" + macro in compact
                for macro in ("routeInitial", "routeFinal")),
            "lookahead_starts_at_candidate_poststate": _poststate_to_future(text, compact, nodes)
                and all(_path(compact, "(" + a + ".east)", "(" + b + ".west)", "feedback")
                        for a, b in (("futureOne", "futureTwo"), ("futureTwo", "futureMore"))),
            "last_layer_example_keeps_future_conditional": all(token in compact for token in (
                r"Example:evaluate$2Q_1+1Q_2$", "Iflayersremain", r"$L_{\ell+1}$", r"$L_{\ell+2}$")),
            "candidate_evaluation_and_score_feedback": all(token in compact for token in (
                "Jointsearch+matching", r"$J_\ell$:current+futureloss"))
                and any(token in compact for token in (
                    "Gatesites/residence/storage", "Gatesites/residency/storage"))
                and _path(compact, "(jointSearch.south)", "(currentEval.north)", "flow")
                and _path(compact, "(future.west)", "(jointSearch.west)", "feedback"),
            "execution_updates_next_layer_state": all(
                _inside(nodes, child, "routing", height)
                for child in ("selectedPlan", "routeBatches", "executeCurrent", "stateUpdate"))
                and all(_path(compact, "(" + a + ".south)", "(" + b + ".north)", "flow")
                        for a, b in (("selectedPlan", "routeBatches"), ("routeBatches", "executeCurrent"),
                                     ("executeCurrent", "stateUpdate")))
                and _path(compact, "(stateUpdate.south)", "(joint.west|-flowRow)", "flow")
                and any(token in compact.replace(r"\\", "") for token in (
                    "Executecurrent2Q+1Q", "Schedule2Q+1Q"))
                and any(token in compact.replace(r"\\", "") for token in (
                    "Updatepositions,occupancyandclock", "Positions,occupancyandclock"))
                and "Nextlayer:updatedstate" in compact.replace(r"\\", ""),
            "zair_sequence_includes_final_single_qubit_layer": instructions == [
                "init", "1qGate", "rearrangeJob", "rydberg", "1qGate", "rearrangeJob", "rydberg", "1qGate"],
        }
    if name == "joint_ga.tex":
        result = dict(route_checks)
        result.update({
            "encoding_uses_depicted_gate_sites": _poststate_pairing(text) and all(token in compact for token in (
                r"0/{$z_{\ell,1}$}/{$T_1^{+}$}", r"1/{$z_{\ell,2}$}/{$T_2^{+}$}",
                r"$(q_0,q_2)$,$(q_1,q_4)$")),
            "candidate_genes_drive_visible_cells": all(token in compact for token in (
                r"\foreach\i/\head/\value/\cellstylein", r"\node[\cellstyle](gene-\i)",
                r"{\value}", r"{\head}")),
            "return_gene_matches_storage_assignment": all(token in compact for token in (
                r"2/{$b_{\ell,1}$}/{1}", r"a_\ell:q_3\mapstos_1", r"q_3@s_1"))
                and _state(text, "routeFinal").get(3) == (1.33, 1.08),
            "candidate_includes_separate_storage_assignment": all(token in compact for token in (
                "Storageassignment", r"Upto$K$assignments", r"Candidate$(\mathbf{x}_\ell,a_\ell)$")),
            "ordered_execution_includes_current_gates": all(token in compact for token in (
                r"\mathbf{s}_{\ell+1}", "Place", "2Q+1Q", r"$B_1$--$B_3$", r"$q_2,q_1,q_4$")),
        })
        return result
    if name == "physical_lookahead.tex":
        intuition = text.split("Transfer and excitation loss", 1)[0]
        storage_states = re.findall(r"\\device\{([\d.]+)\}\{([\d.]+)\}\{([\d.]+)\}", intuition)
        bars = re.findall(r"([\d.]+)/\\(Argument\w+Loss)/(galk\w+)", text)
        bar_values = [(macro, style) for _, macro, style in sorted(bars, key=lambda row: float(row[0]))]
        return {
            "single_atom_component_scope": "Transfer and excitation loss" in text
                and intuition.count(r"\node[occupied]") == 1
                and r"{$q$}" in intuition and "Full candidate fidelity" not in text,
            "roundtrip_four_transfers": r"f_{\rm tran}^{\,4}" in text
                and len(re.findall(r"\\node\[note\]\s*at\s*\([\d.]+,[\d.]+\)\s*\{2\}", intuition)) == 2
                and intuition.count("{transfers}") == 2,
            "reuse_after_r_inactive_layers": all(token in compact for token in (
                r"L_{\ell+r+1}", r"$r$inactivelayers", r"$r$exposures"))
                and len(storage_states) == 3
                and [float(row[2]) for row in storage_states] == [.535, .15, .535]
                and len({row[1] for row in storage_states}) == 1
                and [float(row[0]) for row in storage_states] == sorted(float(row[0]) for row in storage_states),
            "model_losses_drive_bars": bar_values == [
                ("ArgumentStayOneLoss", "galkEntangle"), ("ArgumentRoundtripLoss", "galkStorage"),
                ("ArgumentStayTwoLoss", "galkEntangle"), ("ArgumentRoundtripLoss", "galkStorage")]
                and all(token in compact for token in (r".174*\loss", r"precision=2]{\loss}")),
            "negative_log_axis_starts_at_zero": all(token in compact for token in (
                r"Loss($10^{-3}$)", r"\foreach\valuein{0,2,4,6}", "(1.28,0)--(1.28,1.14)")),
            "lookahead_starts_at_candidate_poststate": all(token in compact for token in (
                r"\mathbf{s}_{\ell+1}", "(postCandidate.east)--(firstFuture.west)",
                "(firstFuture.east)--(secondFuture.west)", "(secondFuture.east)--(laterFuture.west)")),
            "prediction_updates_configuration_and_clock": compact.count(r"updatestate\\andclock") == 2,
            "future_increments_are_decayed": all(token in compact for token in (
                r"\alpha\widehatC_{\ell,1}", r"\alpha\rho\widehatC_{\ell,2}")),
            "current_and_future_losses_share_candidate_objective": all(token in compact for token in (
                r"C_\ell^{\rmcur}", r"Candidate score".replace(" ", ""), r"$J_\ell$",
                r"\Phi_\ell", r"\alpha\rho^{h_\ell-1}")) and all(
                    "(" + node + ".south)--(" + node + ".south|-candidateScore.north)" in compact
                    for node in ("postCandidate", "firstFuture", "secondFuture", "endpoint")),
            "endpoint_follows_entire_prediction_window": "(laterFuture.south)--(endpoint.north)" in compact,
        }
    return {}
