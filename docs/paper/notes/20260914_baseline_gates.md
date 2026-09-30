# Figure 2 gate-set verification — 2026-09-14

## Decision

Use **CZ gates**, drawn as two filled dots connected by a vertical line, in both circuit panels. Use **U3** boxes for the generic one-qubit operations if the input is described as rebased. Do not replace the native two-qubit stage by CNOT gates. The existing controlled-dot plus Z-box notation is mathematically valid CZ; the double-dot form is a legibility improvement and follows both baseline illustrations, not a correction of gate semantics.

## Baseline evidence

### ZAC, HPCA 2025

- Local PDF: `documents/Reuse-Aware_Compilation_for_Zoned_Quantum_Architectures_Based_on_Neutral_Atoms.pdf`.
- Primary source: [UCLA VAST author-hosted publication](https://vast.cs.ucla.edu/sites/default/files/publications/HPCA25_ZAC-2.pdf); [author arXiv record](https://arxiv.org/abs/2411.11784).
- **PDF page 3 / printed page 129, Section IV:** the preprocessing paragraph explicitly rebases into `{CZ, U3}`. Relevant short phrase: “hardware-supported gate set {CZ, U3}”.
- **PDF page 3 / printed page 129, Figure 2 caption:** atoms paired in one Rydberg site perform CZ when the Rydberg laser is on.
- **PDF page 4 / printed page 130, Figure 4:** the unreduced input includes CNOT, CZ, H, X and Z. After resynthesis and single-qubit optimization, the diagram uses symmetric double-dot CZ gates and U3 boxes. The final step groups gates into alternating single-qubit and Rydberg stages.
- Thus the source distinguishes arbitrary logical input from the gate set passed to placement. An input icon containing native CZ and generic U3 is appropriate once the caption identifies it as rebased.

### Routing-aware placement, ICCAD 2025

- Local PDF: `documents/2025_iccad_routing-aware_placement_zoned_neutral_atom.pdf`.
- Primary source: [author arXiv record](https://arxiv.org/abs/2505.22715); [TUM author publication listing](https://www.cda.cit.tum.de/team/wille/publications/).
- **PDF page 2, Section II-A:** local lasers implement one-qubit rotations, and global Rydberg beams implement two-qubit operations such as CZ. Architecture Figure 1 labels its global Rydberg beam for CZ gates.
- **PDF page 3, Figure 3:** its input text explicitly contains `cz`, `rz` and `ry`; its drawn CZ is a pair of filled dots joined by a line, while RY/RZ are rectangular boxes. The figure's single RY box spans four wires because the accompanying program applies that one-qubit rotation across the register; it is not a four-qubit entangling gate.
- **PDF page 3, Section III-A:** scheduling groups non-overlapping two-qubit operations into a layer, preserving dependencies and the associated one-qubit layers. The convention permits some empty one-qubit layers.
- The ICCAD paper therefore does not require replacing RY/RZ by U3 in every logical-input illustration. U3 is the cleaner choice here because Figure 2 illustrates our rebased compiler input and the ZAC-derived interface.

## Active implementation

- `ZAC_zzx/zac/zac.py:33` enables resynthesis by default; lines 131–134 call Qiskit with `basis_gates=["cz", "id", "u2", "u1", "u3"]`, optimization level 3 and fixed transpiler seed 0.
- `ZAC_zzx/zac/router/router.py:498` emits the ZAIR `1qGate` instruction's generic unitary as `u3`.
- `u1` and `u2` are restricted one-qubit families within a generic U3 representation. This does not imply that every undecomposed input must use U3, or that the hardware consists of a single elementary U3 laser pulse.
- No code or experiment was changed for this audit.

## Existing five-wire example

The four CZ pairs in the input are `(0,1), (2,3), (0,2), (1,4)`. Their scheduled layers are `{(0,1),(2,3)}` followed by `{(0,2),(1,4)}`; each layer is qubit-disjoint.

The six one-qubit operations retain their ordering on each wire: the operation on q4 can move into the first one-qubit layer because q4 has no earlier two-qubit gate; the final operation on q3 can move immediately after the first two-qubit layer because q3 participates in no subsequent two-qubit operation. Replacing their displayed names uniformly by U3 does not require a commutation assumption in this example. The two non-adjacent CZ pairs in the second layer should remain slightly horizontally offset so overlapping vertical connectors do not visually become one multi-qubit gate.

## Inspection artifacts

Text extraction and rendered source pages are in `IEEE_conference_template/build/paper_zh/baseline-gates-20260914/`. This note records literature and live-code evidence only; main manuscript/figure changes are handled separately.
