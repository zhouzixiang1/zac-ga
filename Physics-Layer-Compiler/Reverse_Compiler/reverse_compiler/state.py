"""Replay state for the reverse compiler.

Maintains the qubit/atom/position bookkeeping required to replay a ZAC hardware
schedule.  The key invariant is that *atom movement never changes the
qubit-to-atom association*: moving an atom only updates its position/zone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A hardware position is (array/SLM id, row, column).  This matches the ZAIR
# location encoding ``[id, a, r, c]`` where ``(a, r, c)`` is the position.
Position = tuple[int, int, int]


class ResolutionError(KeyError):
    """Raised when a gate target cannot be resolved to a physical qubit."""


@dataclass
class ReplayState:
    """Bookkeeping for replaying a ZAC schedule chronologically."""

    qubit_to_atom: dict[int, int] = field(default_factory=dict)
    atom_to_qubit: dict[int, int] = field(default_factory=dict)
    atom_to_position: dict[int, Position] = field(default_factory=dict)
    position_to_atom: dict[Position, int] = field(default_factory=dict)
    active_atoms: set[int] = field(default_factory=set)
    # Optional trap-id -> position table, supplied by the architecture spec.
    trap_to_position: dict[int, Position] = field(default_factory=dict)

    # ------------------------------------------------------------------ setup
    def bind_qubit(self, qubit: int, atom: int) -> None:
        """Associate a physical qubit with an atom (1:1)."""
        self.qubit_to_atom[qubit] = atom
        self.atom_to_qubit[atom] = qubit

    def place_atom(self, atom: int, position: Position) -> None:
        """Set an atom's position, keeping ``position_to_atom`` consistent."""
        old = self.atom_to_position.get(atom)
        if old is not None and self.position_to_atom.get(old) == atom:
            del self.position_to_atom[old]
        self.atom_to_position[atom] = position
        self.position_to_atom[position] = atom

    # --------------------------------------------------------------- movement
    def move_atom(self, atom: int, position: Position) -> None:
        """Move an atom to ``position``.

        Updates position bookkeeping only.  The qubit<->atom association is left
        untouched, and no quantum gate is produced.
        """
        self.place_atom(atom, position)

    def apply_end_locs(self, end_locs: list) -> None:
        """Apply a batch of ``[id, a, r, c]`` destination locations.

        Positions are cleared first so that two atoms swapping spatial positions
        does not transiently collide in ``position_to_atom``.
        """
        # Clear current positions of the moving atoms first.
        for entry in end_locs:
            atom = entry[0]
            old = self.atom_to_position.get(atom)
            if old is not None and self.position_to_atom.get(old) == atom:
                del self.position_to_atom[old]
        for entry in end_locs:
            atom = entry[0]
            position: Position = (entry[1], entry[2], entry[3])
            self.atom_to_position[atom] = position
            self.position_to_atom[position] = atom

    # -------------------------------------------------------------- liveness
    def deactivate(self, atom: int) -> None:
        self.active_atoms.discard(atom)

    def activate(self, atom: int) -> None:
        self.active_atoms.add(atom)

    def is_active(self, atom: int) -> bool:
        return atom in self.active_atoms

    # ------------------------------------------------------------- resolution
    def atom_at(self, position: Position) -> int:
        atom = self.position_to_atom.get(position)
        if atom is None:
            raise ResolutionError(f"no atom at position {position}")
        return atom

    def qubit_of_atom(self, atom: int) -> int:
        try:
            return self.atom_to_qubit[atom]
        except KeyError as exc:
            raise ResolutionError(f"atom {atom} is not bound to a qubit") from exc

    def resolve_target(self, target) -> int:
        """Resolve a flexible target spec to a physical qubit index.

        Accepted forms::

            42                       # bare int -> atom id
            {"atom": 42}             # atom id
            {"qubit": 7}             # physical qubit id
            {"position": [a, r, c]}  # coordinate -> atom -> qubit
            {"coord": [a, r, c]}     # alias of position
            {"trap": 5}              # trap id -> position -> atom -> qubit
        """
        atom = self._resolve_atom(target)
        return self.qubit_of_atom(atom)

    def _resolve_atom(self, target) -> int:
        if isinstance(target, bool):  # guard: bools are ints in Python
            raise ResolutionError(f"invalid target {target!r}")
        if isinstance(target, int):
            return target
        if isinstance(target, dict):
            if "atom" in target:
                return int(target["atom"])
            if "qubit" in target:
                qubit = int(target["qubit"])
                try:
                    return self.qubit_to_atom[qubit]
                except KeyError as exc:
                    raise ResolutionError(
                        f"qubit {qubit} is not bound to an atom"
                    ) from exc
            for key in ("position", "coord", "coordinate", "loc"):
                if key in target:
                    pos = tuple(target[key])  # type: ignore[arg-type]
                    if len(pos) != 3:
                        raise ResolutionError(
                            f"position must be (a, r, c), got {pos!r}"
                        )
                    return self.atom_at(pos)  # type: ignore[arg-type]
            if "trap" in target:
                trap = int(target["trap"])
                try:
                    pos = self.trap_to_position[trap]
                except KeyError as exc:
                    raise ResolutionError(f"unknown trap id {trap}") from exc
                return self.atom_at(pos)
        raise ResolutionError(f"unrecognised target spec {target!r}")
