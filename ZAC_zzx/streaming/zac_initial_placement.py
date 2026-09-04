"""Exact bounded-memory construction of stock ZAC's initial SA objective."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from zac.placer.saplacer import SAPlacer

from .qasm_sqlite import LayerStore, ZacStage


def _stage_gates(stage) -> Iterable[Sequence[int]]:
    gates = stage.gates if isinstance(stage, ZacStage) else stage
    for gate in gates:
        yield gate.gate_pair if hasattr(gate, "gate_pair") else gate


def build_stock_sa_interactions(
    n_qubits: int,
    stages: Iterable[ZacStage | Sequence[Sequence[int]]],
) -> list[dict[int, float]]:
    """Stream the exact dictionaries produced by ``SAPlacer.preprocessing``.

    Stock ZAC weights physical stages as ``1,.9,.8,.7,.6`` and all later
    stages as ``.6``.  Updates intentionally use the same gate order and
    repeated floating-point ``+=`` operations as the batch implementation;
    replacing them with ``count * .6`` can change the final IEEE-754 value and
    perturb an SA tie on very large repeated circuits.
    """
    if n_qubits <= 0:
        raise ValueError("SA interaction width must be positive")
    interactions: list[dict[int, float]] = [dict() for _ in range(n_qubits)]
    for layer, stage in enumerate(stages):
        weight = 1.0 - 0.1 * layer if layer < 5 else 0.6
        for raw_gate in _stage_gates(stage):
            if len(raw_gate) != 2:
                raise ValueError(f"SA stage {layer} contains a non-2Q gate")
            q0, q1 = int(raw_gate[0]), int(raw_gate[1])
            if not (0 <= q0 < n_qubits and 0 <= q1 < n_qubits) or q0 == q1:
                raise ValueError(f"SA stage {layer} contains invalid gate {(q0, q1)}")
            if q1 in interactions[q0]:
                interactions[q0][q1] += weight
            else:
                interactions[q0][q1] = weight
            interactions[q1][q0] = interactions[q0][q1]
    return interactions


def place_stock_sa_from_layer_store(
    architecture,
    store: LayerStore,
    *,
    max_gates_per_stage: int,
    l2: bool = False,
) -> tuple[tuple[int, int, int], ...]:
    """Run stock SA from the exact capacity-split SQLite ZAC stage stream."""
    n_qubits = int(store.metadata["qubits"])
    interactions = build_stock_sa_interactions(
        n_qubits, store.iter_zac_stages(max_gates_per_stage))
    placer = SAPlacer(l2=l2)
    placer.run_preprocessed(architecture, n_qubits, interactions)
    if placer.best_mapping is None:
        raise RuntimeError("stock SA did not produce an initial mapping")
    return tuple(tuple(int(value) for value in site)
                 for site in placer.best_mapping)


__all__ = ["build_stock_sa_interactions", "place_stock_sa_from_layer_store"]
