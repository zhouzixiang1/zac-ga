"""Physical-scene checks for the accepted Overleaf background figures.

The storage-route replay is retained from verify_paper_zh.py; only the drawing
bindings change with the new layout. Tests exercise unsafe-route mutations.
"""

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path
import re


def _visible(text):
    text = re.sub(r"(?<!\\)%[^\n]*", "", text)
    marker = r"\PaperRevision{"
    while marker in text:
        start = text.index(marker)
        pos = start + len(marker)
        end, depth = pos, 1
        while end < len(text) and depth:
            depth += (text[end] == "{") - (text[end] == "}")
            end += 1
        if depth:
            raise ValueError("Unbalanced PaperRevision argument")
        text = text[:start] + text[pos:end - 1] + text[end:]
    return text


def _storage_dependency_checks(active: str) -> dict[str, bool]:
    """Replay the two illustrated routes and check that their states are drawn."""
    compact = re.sub(r"\s+", "", active)
    checks = dict.fromkeys(("storage_common_sources_and_targets", "storage_path_obstruction",
                            "storage_clearance_precedes_passage", "storage_direct_move_is_clear",
                            "storage_states_drive_drawing", "storage_moves_drive_drawing",
                            "storage_stages_not_extra_gate_layers", "storage_return_paths_match_states"), False)

    def rows(name):
        match = re.search(r"\\def\\" + name + r"\{([^{}]+)\}", compact)
        if not match:
            raise ValueError(name)
        return [row.split("/") for row in match.group(1).split(",")]

    def on_path(point, source, target):
        dx, dy = target[0] - source[0], target[1] - source[1]
        cross = dx * (point[1] - source[1]) - dy * (point[0] - source[0])
        between = sum((point[k] - source[k]) * (point[k] - target[k]) for k in (0, 1))
        return abs(cross) < 1e-9 and between <= 1e-9

    def replay(state, moves):
        state = state.copy()
        for order, atom, source, target in moves:
            stationary = [point for key, point in state.items() if key != atom]
            if state.get(atom) != source or any(on_path(p, source, target) for p in stationary):
                return None
            state[atom] = target
        return state

    try:
        states = {name: {q: (float(x), float(y)) for q, x, y in rows("storage" + name)}
                  for name in ("Initial", "Blocked", "Clear", "Final")}
        moves = {name: [(int(i), q, (float(sx), float(sy)), (float(tx), float(ty)))
                       for i, q, sx, sy, tx, ty in rows("storage" + name + "Moves")]
                 for name in ("Detour", "Direct")}
        initial, blocked, clear, final = (states[key] for key in ("Initial", "Blocked", "Clear", "Final"))
        s1, s2, source, target = (.56, .50), (.56, .20), (.18, .50), (.94, .50)
        checks["storage_common_sources_and_targets"] = (
            initial == {"q": (.18, 1.05), "u": source}
            and blocked == {"q": s1, "u": source} and clear == {"q": s2, "u": source}
            and final == {"q": s2, "u": target}
            and all(len(set(state.values())) == 2 for state in states.values()))
        checks["storage_path_obstruction"] = (on_path(blocked["q"], source, target)
            and not on_path(clear["q"], source, target))
        checks["storage_clearance_precedes_passage"] = (
            moves["Detour"] == [(1, "q", s1, s2), (2, "u", source, target)]
            and replay(blocked, moves["Detour"]) == final)
        checks["storage_direct_move_is_clear"] = (
            moves["Direct"] == [(1, "u", source, target)]
            and replay(clear, moves["Direct"]) == final)
    except (ValueError, KeyError, TypeError):
        return checks
    checks["storage_states_drive_drawing"] = all(token in compact for token in (
        r"\storageAtoms{11.48}{\yy}{\storageInitial}",
        r"\storageAtoms{13.84}{3.08}{\storageBlocked}",
        r"\storageAtoms{13.84}{.94}{\storageClear}",
        r"\storageAtoms{16.20}{3.08}{\storageFinal}",
        r"\storageAtoms{16.20}{.94}{\storageFinal}",
        r"\foreach\atomID/\atomX/\atomYin#3{\node[atom]at(#1+\atomX,#2+\atomY)"))
    checks["storage_moves_drive_drawing"] = all(token in compact for token in (
        r"\storageMoves{16.20}{3.08}{\storageDetourMoves}",
        r"\storageMoves{16.20}{.94}{\storageDirectMoves}",
        r"\foreach\order/\qq/\sx/\sy/\tx/\tyin#3",
        r"(#1+\sx,#2+\sy)--(#1+\tx,#2+\ty)",
        r"at(#1+.78,#2+.67){\order}", r"at(#1+.40,#2+.30){\order}"))
    checks["storage_return_paths_match_states"] = all(token in compact for token in (
        r"\foreach\yy/\targetin{3.08/.50,.94/.20}",
        r"(11.66,\yy+1.05)--(12.04,\yy+\target)",
        r"\storageAtoms{11.48}{\yy}{\storageInitial}"))
    panel = active.split("(c) Storage-site dependence", 1)[-1]
    checks["storage_stages_not_extra_gate_layers"] = all(
        label in panel for label in ("Before return", "After return", "After transfer")) \
        and not re.search(r"L_\{\\ell(?:\+\d)?\}", panel)
    return checks


def _architecture_checks(active):
    compact = re.sub(r"\s+", "", active)
    points = {name: (float(x), float(y)) for name, x, y in re.findall(
        r"\\node\[(?:atom|trap)\]\(([vom][01][st])\)at\(([\d.]+),([\d.]+)\)", compact)}

    def sign(x):
        return (x > 0) - (x < 0)

    def relations(prefix):
        return tuple(tuple(sign(points[prefix + "0" + end][axis] -
                                points[prefix + "1" + end][axis])
                           for axis in (0, 1)) for end in "st")

    def orient(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])

    try:
        valid, order, merge = (relations(p) for p in "vom")
        a, b, c, d = (points[k] for k in ("m0s", "m0t", "m1s", "m1t"))
        crosses = orient(a, b, c)*orient(a, b, d) < 0 and orient(c, d, a)*orient(c, d, b) < 0
    except KeyError:
        valid = order = merge = None
        crosses = True
    return {
        "aod_valid_preserves_both_relations": valid == ((-1, 0), (-1, 0)),
        "aod_reversal_changes_column_order": order == ((-1, 0), (1, 0)),
        "aod_merging_invalid_without_path_crossing": merge == ((-1, -1), (-1, 0)) and not crosses,
        "pickup_ghost_and_serialized_beams_safe": all(token in compact for token in (
            r"\foreach\xin{8.65,10.00}{\draw[guide]",
            r"\foreach\yin{1.36,2.36}{\draw[guide]",
            r"\draw[guide](11.91,1.12)--(11.91,2.61)",
            r"\draw[guide](11.58,1.36)--(13.59,1.36)",
            r"\draw[guide](16.52,1.12)--(16.52,2.61)",
            r"\draw[guide](14.84,2.36)--(16.85,2.36)")),
        "pickup_shared_stationary_geometry": all(token in compact for token in (
            r"\foreach\dxin{0,3.26,6.52}", r"\begin{scope}[xshift=\dxcm]",
            r"\node[trap]at(15.17,1.36)", "vacated", "{$s$}"))
            and compact.count(r"\node[fixed]at(8.65,2.36)") == 2,
        "pickup_completes_batch_before_next": "Complete$B_1$before$B_2$;$q_2$staysat$s$" in compact
            and r"\draw[motion](14.00,1.85)--(14.50,1.85)" in compact,
        "transport_preserves_original_curve": all(token in compact for token in (
            "(qthree)at(5.64,2.28)", "(qthreeDest)at(6.32,5.12)",
            "controls(6.02,3.12)and(6.23,4.23)",
            r"\draw[motion](rydberg.south)--(qone.north)",
            r"\draw[motion](raman.southwest)--(qtwo.northeast)")),
    }


def checks(name, active):
    """Return per-property booleans; accept raw or revision-unwrapped TeX."""
    active = _visible(active)
    if name == "baseline_motivation.tex":
        return _storage_dependency_checks(active)
    if name == "architecture_preliminaries.tex":
        return _architecture_checks(active)
    return {}
