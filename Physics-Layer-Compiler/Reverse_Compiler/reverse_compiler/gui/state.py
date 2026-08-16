"""State management for the GUI: atoms, operation timeline, undo/redo.

This module owns the *authoring* data model.  It preserves the ZAIR instruction
format exactly (``init`` / ``rearrangeJob`` / ``1qGate`` / ``rydberg`` / ``swap``
/ ``measure``) so the downstream reverse compiler keeps working unchanged.

Key invariants (matching the reverse compiler semantics):
* Atom movement (``rearrangeJob``) updates position/zone only; it is **not** a
  quantum gate and is stored as a hardware schedule instruction.
* The physical-qubit <-> atom association never changes on a move.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------- hardware dims
STORAGE_ARRAY = 0
ENTANGLE_ARRAY = 1
STORAGE_ROWS = 20
STORAGE_COLS = 40
# The entanglement-zone tweezer array is user-configurable (default 2 x 20).
ENTANGLE_ROWS = 20
ENTANGLE_COLS = 2

# Sensible bounds for the configurable entanglement zone.
ENTANGLE_ROWS_MAX = 60
ENTANGLE_COLS_MAX = 40


def configure_entanglement(rows: int, cols: int) -> None:
    """Resize the entanglement-zone tweezer array (global hardware spec).

    ``cols`` must be even so atoms pair up as adjacent columns ``{2k, 2k+1}``
    for the Rydberg CZ/CX interaction.
    """
    global ENTANGLE_ROWS, ENTANGLE_COLS
    rows = int(rows)
    cols = int(cols)
    if not 1 <= rows <= ENTANGLE_ROWS_MAX:
        raise StateError(f"Entanglement rows must be between 1 and {ENTANGLE_ROWS_MAX}.")
    if cols < 2 or cols > ENTANGLE_COLS_MAX:
        raise StateError(f"Entanglement columns must be between 2 and {ENTANGLE_COLS_MAX}.")
    if cols % 2 != 0:
        raise StateError("Entanglement columns must be even (atoms pair as adjacent columns).")
    ENTANGLE_ROWS = rows
    ENTANGLE_COLS = cols


def entangle_dims() -> tuple[int, int]:
    """Return the current entanglement zone (rows, cols)."""
    return ENTANGLE_ROWS, ENTANGLE_COLS


def entangle_pair_ok(p0: "Site", p1: "Site") -> bool:
    """Whether two positions form a valid Rydberg interaction pair.

    Rule: both atoms in the entanglement zone, in the same row, occupying an
    adjacent even/odd column pair ``{2k, 2k+1}``.  For a 2-column zone this is
    simply columns ``{0, 1}``.
    """
    if p0.array != ENTANGLE_ARRAY or p1.array != ENTANGLE_ARRAY:
        return False
    if p0.row != p1.row:
        return False
    lo, hi = sorted((p0.col, p1.col))
    return hi - lo == 1 and lo % 2 == 0


@dataclass(frozen=True)
class Site:
    """A hardware trap position ``(array, row, col)``."""

    array: int
    row: int
    col: int

    def as_loc(self, atom: int) -> list[int]:
        return [atom, self.array, self.row, self.col]

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.array, self.row, self.col)


class StateError(ValueError):
    """User-facing validation error with a readable message."""


def valid_site(site: Site) -> bool:
    if site.array == STORAGE_ARRAY:
        return 0 <= site.row < STORAGE_ROWS and 0 <= site.col < STORAGE_COLS
    if site.array == ENTANGLE_ARRAY:
        return 0 <= site.row < ENTANGLE_ROWS and 0 <= site.col < ENTANGLE_COLS
    return False


def op_kind(inst: dict[str, Any]) -> str:
    """Return a short timeline label for a ZAIR instruction."""
    itype = str(inst.get("type", ""))
    if itype == "init":
        return "INIT"
    if itype in {"rearrangeJob", "move", "MOVE"}:
        return "MOVE"
    if itype in {"1qGate", "global_1qGate"}:
        return "1Q"
    if itype in {"rydberg", "2qGate"}:
        # cx/cz are 2Q, swap handled separately
        return "2Q"
    if itype in {"swap", "SWAP"}:
        return "SWAP"
    if itype in {"measure", "MEASURE"}:
        return "MEASURE"
    return "OTHER"


def op_summary(inst: dict[str, Any]) -> str:
    """A human-readable one-line summary of an instruction."""
    kind = op_kind(inst)
    if kind == "INIT":
        return f"INIT  ·  {len(inst.get('init_locs', []))} atoms"
    if kind == "MOVE":
        locs = inst.get("end_locs", inst.get("locs", []))
        if len(locs) > 1:
            return f"MOVE  ·  {len(locs)} atoms (parallel)"
        if locs:
            a, ar, r, c = locs[0][0], locs[0][1], locs[0][2], locs[0][3]
            return f"MOVE  ·  atom {a} → (a{ar}, r{r}, c{c})"
        return "MOVE"
    if kind == "1Q":
        gates = inst.get("gates", [])
        if len(gates) > 1:
            names = "/".join(sorted({str(g.get('name', '?')).upper() for g in gates}))
            return f"1Q  ·  {len(gates)} gates (parallel) [{names}]"
        if gates:
            g = gates[0]
            name = str(g.get("name", inst.get("unitary", "?"))).upper()
            params = g.get("params")
            ptxt = f"({params[0]:.3g})" if params else ""
            return f"1Q  ·  {name}{ptxt} on atom {g.get('q', '?')}"
        return "1Q gate"
    if kind == "2Q":
        gates = inst.get("gates", [])
        if len(gates) > 1:
            name = str(gates[0].get("name", "cz")).upper()
            pairs = ", ".join(f"({g.get('q0')},{g.get('q1')})" for g in gates)
            return f"2Q  ·  {len(gates)}× {name} (parallel) {pairs}"
        if gates:
            g = gates[0]
            name = str(g.get("name", "cz")).upper()
            return f"2Q  ·  {name} on atoms {g.get('q0')}, {g.get('q1')}"
        return "2Q gate"
    if kind == "SWAP":
        qs = inst.get("qubits", [])
        return f"SWAP  ·  atoms {qs[0]}, {qs[1]}" if len(qs) >= 2 else "SWAP"
    if kind == "MEASURE":
        qs = inst.get("qubits", inst.get("targets", []))
        return f"MEASURE  ·  atoms {', '.join(str(q) for q in qs)}"
    return str(inst.get("type", "op"))


def instruction_atoms(inst: dict[str, Any]) -> set[int]:
    """Atoms referenced by an instruction (for highlighting)."""
    kind = op_kind(inst)
    atoms: set[int] = set()
    if kind == "MOVE":
        for loc in inst.get("end_locs", inst.get("locs", [])):
            atoms.add(int(loc[0]))
    elif kind == "1Q":
        for g in inst.get("gates", []):
            if "q" in g:
                atoms.add(int(g["q"]))
    elif kind == "2Q":
        for g in inst.get("gates", []):
            atoms.add(int(g["q0"]))
            atoms.add(int(g["q1"]))
    elif kind == "SWAP":
        for q in inst.get("qubits", [])[:2]:
            atoms.add(int(q))
    elif kind == "MEASURE":
        for q in inst.get("qubits", inst.get("targets", [])):
            if isinstance(q, int):
                atoms.add(int(q))
    return atoms


@dataclass
class GuiState:
    """Authoring state: initial layout + ordered operations, with undo/redo."""

    atom_count: int = 6
    init_positions: dict[int, Site] = field(default_factory=dict)
    operations: list[dict[str, Any]] = field(default_factory=list)
    _undo: list[tuple] = field(default_factory=list)
    _redo: list[tuple] = field(default_factory=list)

    # ------------------------------------------------------------- snapshots
    def _snapshot(self) -> tuple:
        return (dict(self.init_positions), copy.deepcopy(self.operations))

    def _restore(self, snap: tuple) -> None:
        positions, operations = snap
        self.init_positions = dict(positions)
        self.operations = copy.deepcopy(operations)

    def _push_undo(self) -> None:
        self._undo.append(self._snapshot())
        if len(self._undo) > 200:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        return True

    # ------------------------------------------------------------------- init
    def reset_atoms(self, count: Optional[int] = None) -> None:
        """Reset to a fresh default layout of ``count`` atoms in storage."""
        self._push_undo()
        if count is not None:
            self.atom_count = int(count)
        n = int(self.atom_count)
        self.init_positions = {}
        # Fill starting from the column closest to the entanglement zone
        # (rightmost), top-to-bottom vertically, then move one column left.
        for atom in range(n):
            col = STORAGE_COLS - 1 - (atom // STORAGE_ROWS)
            row = atom % STORAGE_ROWS
            if col < 0:
                break
            self.init_positions[atom] = Site(STORAGE_ARRAY, row, col)
        self.operations = []

    def new_project(self) -> None:
        self._push_undo()
        self.init_positions = {}
        self.operations = []
        self.atom_count = 0

    def place_atom(self, atom: int, site: Site) -> None:
        if atom < 0:
            raise StateError("Atom ID must be non-negative.")
        if not valid_site(site):
            raise StateError(f"Site (a{site.array}, r{site.row}, c{site.col}) is out of range.")
        occ = self.atom_at_init(site)
        if occ is not None and occ != atom:
            raise StateError(f"Site already occupied by atom {occ}.")
        if self.operations:
            raise StateError("Clear operations before editing the initial layout.")
        self._push_undo()
        self.init_positions[atom] = site

    def remove_atom(self, atom: int) -> None:
        if atom not in self.init_positions:
            raise StateError(f"Atom {atom} is not present.")
        if self.operations:
            raise StateError("Clear operations before editing the initial layout.")
        self._push_undo()
        del self.init_positions[atom]

    def atom_at_init(self, site: Site) -> Optional[int]:
        for atom, p in self.init_positions.items():
            if p.key == site.key:
                return atom
        return None

    # ------------------------------------------------------------- operations
    def _last(self) -> Optional[dict[str, Any]]:
        return self.operations[-1] if self.operations else None

    def add_move(self, atom: int, dst: Site, *, parallel: bool = False) -> None:
        positions = self.positions_at(len(self.operations))
        if atom not in positions:
            raise StateError(f"Atom {atom} does not exist.")
        if not valid_site(dst):
            raise StateError(f"Destination (a{dst.array}, r{dst.row}, c{dst.col}) is out of range.")
        src = positions[atom]
        if src.key == dst.key:
            raise StateError("Atom is already at that site.")

        last = self._last()
        if parallel and last is not None and op_kind(last) == "MOVE":
            moved = {int(loc[0]) for loc in last.get("end_locs", [])}
            if atom in moved:
                raise StateError(f"Atom {atom} is already moved in this parallel layer.")
            dest_keys = {(int(l[1]), int(l[2]), int(l[3])) for l in last.get("end_locs", [])}
            if dst.key in dest_keys:
                raise StateError("Destination collides with another move in this parallel layer.")
            # Co-moving atoms move simultaneously, so the destination only needs to
            # avoid atoms that stay put (not other members of this AOD group).
            group: list[tuple[int, Site, Site]] = []
            for b, e in zip(last.get("begin_locs", []), last.get("end_locs", [])):
                group.append(
                    (int(b[0]), Site(int(b[1]), int(b[2]), int(b[3])),
                     Site(int(e[1]), int(e[2]), int(e[3])))
                )
            group.append((atom, src, dst))
            self._validate_parallel_move(group)
            moving = moved | {atom}
            static = {p.key: a for a, p in positions.items() if a not in moving}
            if dst.key in static:
                raise StateError(f"Destination occupied by (non-moving) atom {static[dst.key]}.")
            self._push_undo()
            last.setdefault("aod_qubits", []).append(atom)
            last.setdefault("begin_locs", []).append(src.as_loc(atom))
            last.setdefault("end_locs", []).append(dst.as_loc(atom))
            return

        occ = self._atom_at(positions, dst)
        if occ is not None and occ != atom:
            raise StateError(f"Destination occupied by atom {occ}.")
        self._push_undo()
        self.operations.append(
            {
                "type": "rearrangeJob",
                "aod_qubits": [atom],
                "begin_locs": [src.as_loc(atom)],
                "end_locs": [dst.as_loc(atom)],
            }
        )

    def add_parallel_move(self, atoms: list[int], drow: int, dcol: int,
                          *, target_array: Optional[int] = None) -> None:
        """Move several atoms **simultaneously** by a shared shift ``(drow, dcol)``.

        This is the physical AOD transport: any set of ``m`` rows × ``n`` columns of
        atoms (rows/columns need **not** be contiguous) can be carried together as
        long as the motion preserves row/column ordering (a uniform shift always
        does).  Because the move is simultaneous, an atom may shift into a site that
        is currently held by another atom of the same group (which vacates it at the
        same instant); only collisions with *stationary* atoms are rejected.

        ``target_array`` optionally transports the whole grid into a different zone
        (e.g. storage → entanglement); the row/column offsets still apply, so a
        block keeps its shape in the destination zone.
        """
        if not atoms:
            raise StateError("Select at least one atom for a parallel move.")
        positions = self.positions_at(len(self.operations))
        group: list[tuple[int, Site, Site]] = []
        for a in atoms:
            if a not in positions:
                raise StateError(f"Atom {a} does not exist.")
            src = positions[a]
            arr = src.array if target_array is None else int(target_array)
            dst = Site(arr, src.row + drow, src.col + dcol)
            if not valid_site(dst):
                raise StateError(
                    f"Atom {a} would move out of range to "
                    f"(a{dst.array}, r{dst.row}, c{dst.col})."
                )
            group.append((a, src, dst))

        if all(s.key == d.key for _a, s, d in group):
            raise StateError("Parallel move does not change any position.")

        self._validate_parallel_move(group)

        dst_keys = [g[2].key for g in group]
        if len(set(dst_keys)) != len(dst_keys):
            raise StateError("Parallel move: two atoms would land on the same site.")
        moving = {g[0] for g in group}
        static = {p.key: a for a, p in positions.items() if a not in moving}
        for a, _src, dst in group:
            if dst.key in static:
                raise StateError(
                    f"Atom {a} would collide with stationary atom {static[dst.key]}."
                )

        self._push_undo()
        self.operations.append(
            {
                "type": "rearrangeJob",
                "aod_qubits": [g[0] for g in group],
                "begin_locs": [g[1].as_loc(g[0]) for g in group],
                "end_locs": [g[2].as_loc(g[0]) for g in group],
            }
        )

    @staticmethod
    def _validate_parallel_move(group: list[tuple[int, "Site", "Site"]]) -> None:
        """Enforce the AOD grid rule for a simultaneous multi-atom move.

        AOD transports a selection of ``m`` rows × ``n`` columns of atoms.  The rows
        and the columns need **not** be contiguous, but the motion must preserve the
        grid structure and the ordering of rows/columns:

        * every atom starts in one array and ends in one array;
        * atoms sharing a source row move to the same destination row, and atoms
          sharing a source column move to the same destination column (the grid
          stays a grid — no shearing);
        * the ordering of rows and of columns is preserved (rows/columns may
          stretch or shift but never cross or merge).
        """
        if len(group) <= 1:
            return
        src_arrays = {s.array for _, s, _ in group}
        dst_arrays = {d.array for _, _, d in group}
        if len(src_arrays) != 1 or len(dst_arrays) != 1:
            raise StateError(
                "Parallel move: all atoms must start in one array and end in one array."
            )

        row_map: dict[int, int] = {}
        col_map: dict[int, int] = {}
        for _atom, s, d in group:
            if s.row in row_map and row_map[s.row] != d.row:
                raise StateError(
                    "Parallel move breaks the AOD grid: atoms in source "
                    f"row {s.row} must all move to the same destination row."
                )
            row_map[s.row] = d.row
            if s.col in col_map and col_map[s.col] != d.col:
                raise StateError(
                    "Parallel move breaks the AOD grid: atoms in source "
                    f"column {s.col} must all move to the same destination column."
                )
            col_map[s.col] = d.col

        def _order_preserving(mapping: dict[int, int], what: str) -> None:
            keys = sorted(mapping)
            vals = [mapping[k] for k in keys]
            for i in range(len(vals) - 1):
                if vals[i] >= vals[i + 1]:
                    raise StateError(
                        f"Parallel move: {what} order must be preserved "
                        "(rows/columns cannot cross or merge)."
                    )

        _order_preserving(row_map, "row")
        _order_preserving(col_map, "column")

    def add_1q(self, atom: int, gate: str, param: Optional[float] = None, *, parallel: bool = False) -> None:
        positions = self.positions_at(len(self.operations))
        if atom not in positions:
            raise StateError(f"Atom {atom} does not exist.")
        gate = gate.strip().lower()
        if not gate:
            raise StateError("Gate name cannot be empty.")
        g: dict[str, Any] = {"name": gate, "q": atom}
        if gate in {"rx", "ry", "rz"}:
            if param is None:
                raise StateError(f"Gate {gate.upper()} requires an angle parameter.")
            g["params"] = [float(param)]

        last = self._last()
        if parallel and last is not None and last.get("type") == "1qGate":
            used = {int(x["q"]) for x in last.get("gates", []) if "q" in x}
            if atom in used:
                raise StateError(f"Atom {atom} already has a gate in this parallel layer.")
            self._push_undo()
            last.setdefault("gates", []).append(g)
            return

        self._push_undo()
        self.operations.append({"type": "1qGate", "unitary": gate, "gates": [g]})

    def add_2q(self, gate: str, a0: int, a1: int, *, parallel: bool = False) -> None:
        positions = self.positions_at(len(self.operations))
        gate = gate.strip().lower()
        if a0 == a1:
            raise StateError("A two-qubit gate needs two distinct atoms.")
        if a0 not in positions or a1 not in positions:
            raise StateError("Both atoms must exist.")
        if gate not in {"cz", "cx", "swap"}:
            raise StateError("2Q gate must be cz, cx or swap.")
        if gate in {"cz", "cx"}:
            p0, p1 = positions[a0], positions[a1]
            if not entangle_pair_ok(p0, p1):
                raise StateError(
                    "CZ/CX requires both atoms in the entanglement zone (a1), "
                    "same row, in an adjacent column pair {2k, 2k+1}."
                )
            last = self._last()
            if parallel and last is not None and last.get("type") == "rydberg":
                used: set[int] = set()
                for gg in last.get("gates", []):
                    used.add(int(gg["q0"]))
                    used.add(int(gg["q1"]))
                if a0 in used or a1 in used:
                    raise StateError("An atom is already entangled in this parallel layer.")
                self._push_undo()
                last.setdefault("gates", []).append({"q0": a0, "q1": a1, "name": gate})
                return
            self._push_undo()
            self.operations.append(
                {
                    "type": "rydberg",
                    "zone_id": 0,
                    "gates": [{"q0": a0, "q1": a1, "name": gate}],
                }
            )
        else:
            self._push_undo()
            self.operations.append({"type": "swap", "qubits": [a0, a1]})

    def add_all_entangle_pairs(self, gate: str = "cz",
                               atoms: Optional[set[int]] = None) -> int:
        """Apply a 2Q gate to **every** valid pair in the entanglement zone at once.

        A pair is two atoms in the same entanglement-zone row occupying an adjacent
        column pair ``{2k, 2k+1}``.  All such pairs become one parallel ``rydberg``
        layer.  Optionally restrict to atoms in ``atoms``.  Returns the pair count.
        """
        gate = gate.strip().lower()
        if gate not in {"cz", "cx"}:
            raise StateError("Entangle-all supports cz or cx only.")
        positions = self.positions_at(len(self.operations))
        by_row: dict[int, dict[int, int]] = {}
        for a, p in positions.items():
            if p.array != ENTANGLE_ARRAY:
                continue
            if atoms is not None and a not in atoms:
                continue
            by_row.setdefault(p.row, {})[p.col] = a

        gates: list[dict[str, Any]] = []
        for row in sorted(by_row):
            cols = by_row[row]
            for c in sorted(cols):
                if c % 2 == 0 and (c + 1) in cols:
                    gates.append({"q0": cols[c], "q1": cols[c + 1], "name": gate})
        if not gates:
            raise StateError(
                "No entanglement-zone row pairs found. Move atoms into adjacent "
                "columns {2k, 2k+1} of the entanglement zone first."
            )
        self._push_undo()
        self.operations.append({"type": "rydberg", "zone_id": 0, "gates": gates})
        return len(gates)

    def add_measure(self, atoms: list[int]) -> None:
        positions = self.positions_at(len(self.operations))
        clean: list[int] = []
        for a in atoms:
            if a not in positions:
                raise StateError(f"Atom {a} does not exist.")
            clean.append(int(a))
        if not clean:
            raise StateError("Select at least one atom to measure.")
        self._push_undo()
        self.operations.append({"type": "measure", "qubits": clean})

    def delete_op(self, index: int) -> None:
        if not 0 <= index < len(self.operations):
            return
        self._push_undo()
        self.operations.pop(index)

    def duplicate_op(self, index: int) -> None:
        if not 0 <= index < len(self.operations):
            return
        self._push_undo()
        self.operations.insert(index + 1, copy.deepcopy(self.operations[index]))

    def move_op(self, index: int, delta: int) -> int:
        new_index = index + delta
        if not (0 <= index < len(self.operations)) or not (0 <= new_index < len(self.operations)):
            return index
        self._push_undo()
        op = self.operations.pop(index)
        self.operations.insert(new_index, op)
        return new_index

    def replace_op(self, index: int, inst: dict[str, Any]) -> None:
        if not 0 <= index < len(self.operations):
            return
        self._push_undo()
        self.operations[index] = inst

    def clear_operations(self) -> None:
        if not self.operations:
            return
        self._push_undo()
        self.operations = []

    # -------------------------------------------------------------- positions
    @staticmethod
    def _atom_at(positions: dict[int, Site], site: Site) -> Optional[int]:
        for atom, p in positions.items():
            if p.key == site.key:
                return atom
        return None

    def positions_at(self, step: int) -> dict[int, Site]:
        """Atom positions after applying the first ``step`` operations."""
        positions = dict(self.init_positions)
        for inst in self.operations[:step]:
            if op_kind(inst) == "MOVE":
                for loc in inst.get("end_locs", inst.get("locs", [])):
                    positions[int(loc[0])] = Site(int(loc[1]), int(loc[2]), int(loc[3]))
        return positions

    def move_endpoints(self, index: int) -> list[tuple[int, Site, Site]]:
        """(atom, src, dst) tuples for the move operation at ``index``."""
        if not 0 <= index < len(self.operations):
            return []
        inst = self.operations[index]
        if op_kind(inst) != "MOVE":
            return []
        before = self.positions_at(index)
        out: list[tuple[int, Site, Site]] = []
        for loc in inst.get("end_locs", inst.get("locs", [])):
            atom = int(loc[0])
            dst = Site(int(loc[1]), int(loc[2]), int(loc[3]))
            src = before.get(atom, dst)
            out.append((atom, src, dst))
        return out

    # -------------------------------------------------------------------- zair
    def build_zair(self, name: str = "gui_authored_program") -> dict[str, Any]:
        init = {
            "type": "init",
            "init_locs": [site.as_loc(atom) for atom, site in sorted(self.init_positions.items())],
        }
        return {
            "name": name,
            "architecture_spec_path": None,
            "gui_hardware": {
                "storage_rows": STORAGE_ROWS,
                "storage_cols": STORAGE_COLS,
                "entangle_rows": ENTANGLE_ROWS,
                "entangle_cols": ENTANGLE_COLS,
            },
            "instructions": [init] + copy.deepcopy(self.operations),
        }

    def load_zair(self, zair: dict[str, Any]) -> None:
        instructions = zair.get("instructions", [])
        if not instructions or instructions[0].get("type") != "init":
            raise StateError("ZAIR must start with an 'init' instruction.")
        hw = zair.get("gui_hardware")
        if isinstance(hw, dict) and "entangle_rows" in hw and "entangle_cols" in hw:
            try:
                configure_entanglement(int(hw["entangle_rows"]), int(hw["entangle_cols"]))
            except StateError:
                pass
        self._push_undo()
        init = instructions[0]
        self.init_positions = {
            int(loc[0]): Site(int(loc[1]), int(loc[2]), int(loc[3]))
            for loc in init.get("init_locs", [])
        }
        self.atom_count = len(self.init_positions)
        self.operations = copy.deepcopy(list(instructions[1:]))
