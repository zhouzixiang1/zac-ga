"""Hardware geometry and timing model for the Fable compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot, sqrt
from typing import Dict, Tuple

STORAGE = "storage"
ENTANGLEMENT = "entanglement"
Position = Tuple[str, int, int]


@dataclass(frozen=True)
class Zone:
    name: str
    cols: int
    rows: int

    @property
    def capacity(self) -> int:
        return self.cols * self.rows

    def contains(self, col: int, row: int) -> bool:
        return 0 <= col < self.cols and 0 <= row < self.rows


@dataclass
class HardwareSpec:
    """Neutral-atom machine matching the prompt geometry.

    Positions are stored as ``(zone, col, row)``. Storage column ``0`` is the
    column closest to the entanglement zone; larger storage columns move left,
    farther away from the entanglement zone.
    """

    storage: Zone = field(default_factory=lambda: Zone(STORAGE, 40, 20))
    entanglement: Zone = field(default_factory=lambda: Zone(ENTANGLEMENT, 2, 20))
    zone_gap: float = 2.0

    t_1q: float = 1.0
    t_2q: float = 0.4
    t_measure: float = 5.0
    t_move_fixed: float = 15.0

    f_1q: float = 0.9995
    f_2q: float = 0.995
    f_measure: float = 0.99
    f_transfer: float = 0.9999
    t2_coherence: float = 1.5e6

    aod_max_atoms: int = 40
    min_atom_separation: float = 1.0
    aod_max_distance: float = 60.0
    aod_max_velocity: float = 1.0
    aod_max_acceleration: float = 1.0
    aod_max_move_duration: float = 1000.0
    allow_tone_crossing: bool = False

    def zone(self, name: str) -> Zone:
        if name == STORAGE:
            return self.storage
        if name == ENTANGLEMENT:
            return self.entanglement
        raise KeyError(f"unknown zone {name!r}")

    def valid(self, pos: Position) -> bool:
        zone, col, row = pos
        try:
            return self.zone(zone).contains(col, row)
        except KeyError:
            return False

    def storage_index_to_site(self, index: int) -> Position:
        if not 0 <= index < self.storage.capacity:
            raise ValueError(f"storage index {index} outside 0..{self.storage.capacity - 1}")
        col = index // self.storage.rows
        row = index % self.storage.rows
        return (STORAGE, col, row)

    def coordinate(self, pos: Position) -> Tuple[float, float]:
        """Map ``(zone, col, row)`` to global coordinates.

        Storage lives to the left of the entanglement zone. The storage column
        nearest the entanglement zone has x = -1, then x decreases leftward.
        Entanglement columns have x = 0 and x = 1.
        """
        zone, col, row = pos
        if zone == STORAGE:
            x = -self.zone_gap - float(col)
        elif zone == ENTANGLEMENT:
            x = float(col)
        else:
            raise KeyError(f"unknown zone {zone!r}")
        return (x, float(row))

    def distance(self, a: Position, b: Position) -> float:
        ax, ay = self.coordinate(a)
        bx, by = self.coordinate(b)
        return hypot(ax - bx, ay - by)

    def move_duration(self, distance: float) -> float:
        if distance <= 0.0:
            return 0.0
        v = self.aod_max_velocity
        a = self.aod_max_acceleration
        ramp_distance = (v * v) / a
        if distance <= ramp_distance:
            return 2.0 * sqrt(distance / a)
        return distance / v + v / a

    def interaction_rows(self) -> int:
        return self.entanglement.rows

    def as_dict(self) -> Dict:
        return {
            "storage": {"cols": self.storage.cols, "rows": self.storage.rows},
            "entanglement": {
                "cols": self.entanglement.cols,
                "rows": self.entanglement.rows,
                "placement": "right_of_storage",
            },
            "indexing": "storage column-major from nearest entanglement column",
            "timing": {
                "t_1q": self.t_1q,
                "t_2q": self.t_2q,
                "t_measure": self.t_measure,
                "t_move_fixed": self.t_move_fixed,
            },
            "fidelity": {
                "f_1q": self.f_1q,
                "f_2q": self.f_2q,
                "f_measure": self.f_measure,
                "f_transfer": self.f_transfer,
                "t2_coherence": self.t2_coherence,
            },
            "aod": {
                "aod_max_atoms": self.aod_max_atoms,
                "min_atom_separation": self.min_atom_separation,
                "aod_max_distance": self.aod_max_distance,
                "aod_max_velocity": self.aod_max_velocity,
                "aod_max_acceleration": self.aod_max_acceleration,
                "aod_max_move_duration": self.aod_max_move_duration,
                "allow_tone_crossing": self.allow_tone_crossing,
            },
        }


def default_hardware() -> HardwareSpec:
    return HardwareSpec()