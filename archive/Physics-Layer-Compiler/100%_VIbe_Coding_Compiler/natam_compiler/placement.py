"""Initial placement of logical qubits onto storage-zone sites.

A good initial layout keeps frequently-interacting qubits close to the
entanglement zone so that the transport distance for two-qubit gates is small.
The strategy:

1. weigh every qubit pair by how often it shares a two-qubit gate,
2. greedily match the heaviest pairs and park each matched pair as a vertical
   "domino" (same column, adjacent rows).  A CZ between the two then only
   needs a straight vertical transport into the shared entanglement column --
   both atoms ride one AOD column tone, which is always batch-compatible,
3. place matched pairs (heaviest first) on the dominoes closest to the
   entanglement zone, and remaining qubits (busiest first) on the closest
   free sites.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .hardware import STORAGE, HardwareSpec, Position
from .ir import LogicalCircuit


def interaction_degree(circuit: LogicalCircuit) -> Dict[int, int]:
    deg = {q: 0 for q in range(circuit.num_qubits)}
    for a, b in circuit.two_qubit_pairs():
        deg[a] += 1
        deg[b] += 1
    return deg


def pair_weights(circuit: LogicalCircuit) -> Dict[Tuple[int, int], int]:
    """Count of two-qubit gates per unordered qubit pair."""
    weights: Dict[Tuple[int, int], int] = {}
    for a, b in circuit.two_qubit_pairs():
        key = (min(a, b), max(a, b))
        weights[key] = weights.get(key, 0) + 1
    return weights


def _greedy_matching(
    num_qubits: int, weights: Dict[Tuple[int, int], int]
) -> Tuple[List[Tuple[int, int]], List[int]]:
    """Match qubits into pairs by descending interaction weight (greedy)."""
    edges = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    used: set = set()
    pairs: List[Tuple[int, int]] = []
    for (a, b), _w in edges:
        if a not in used and b not in used:
            used.add(a)
            used.add(b)
            pairs.append((a, b))
    singles = [q for q in range(num_qubits) if q not in used]
    return pairs, singles


def _dominoes_by_proximity(hw: HardwareSpec) -> List[Tuple[Position, Position]]:
    """Vertical two-site slots (same column, adjacent rows), closest first."""
    ent_center_col = (hw.entanglement.cols - 1) / 2.0
    dominoes: List[Tuple[Position, Position]] = []
    for top in range(0, hw.storage.rows - 1, 2):
        for col in range(hw.storage.cols):
            dominoes.append(((STORAGE, top, col), (STORAGE, top + 1, col)))
    # Storage row 0 is adjacent to the entanglement zone.
    dominoes.sort(key=lambda d: (d[0][1], abs(d[0][2] - ent_center_col), d[0][2]))
    return dominoes


def _storage_sites_by_proximity(hw: HardwareSpec) -> List[Position]:
    sites: List[Position] = []
    ent_center_col = (hw.entanglement.cols - 1) / 2.0
    # Closest entanglement row reference point.
    ref = (hw.entanglement.name, hw.entanglement.rows - 1, ent_center_col)
    for row in range(hw.storage.rows):
        for col in range(hw.storage.cols):
            sites.append((STORAGE, row, col))

    def key(site: Position):
        # primary: distance to entanglement zone; tie-break: column closeness
        return (hw.distance(site, ref), abs(site[2] - ent_center_col), site)

    sites.sort(key=key)
    return sites


def place(circuit: LogicalCircuit, hw: HardwareSpec) -> Dict[int, Position]:
    """Return a mapping ``logical qubit -> storage Position``."""
    if circuit.num_qubits > hw.storage.capacity:
        raise ValueError(
            f"circuit needs {circuit.num_qubits} qubits but storage holds "
            f"{hw.storage.capacity}"
        )

    weights = pair_weights(circuit)
    pairs, singles = _greedy_matching(circuit.num_qubits, weights)

    homes: Dict[int, Position] = {}
    dominoes = _dominoes_by_proximity(hw)

    # Heaviest pairs get the dominoes closest to the entanglement zone.
    pairs.sort(key=lambda p: -weights[p])
    for (a, b), (top, bottom) in zip(pairs, dominoes):
        homes[a] = top
        homes[b] = bottom

    # Remaining qubits (busiest first) take the closest free sites.
    deg = interaction_degree(circuit)
    singles.sort(key=lambda q: (-deg[q], q))
    taken = set(homes.values())
    free = [s for s in _storage_sites_by_proximity(hw) if s not in taken]
    for qubit, site in zip(singles, free):
        homes[qubit] = site

    return homes
