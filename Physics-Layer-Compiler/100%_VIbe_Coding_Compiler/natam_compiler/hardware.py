"""Hardware data model for a zoned neutral-atom quantum computer.

The machine has two physical regions ("zones") laid out as 2-D grids of
trap sites:

* a large **storage zone** where idle qubits are parked, and
* a small **entanglement zone** where two-qubit (Rydberg) gates are executed.

Atoms (physical qubits) can be transported between sites/zones by an AOD
(acousto-optic deflector).  The model below captures the geometry plus the
timing and fidelity parameters that the compiler optimizes against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot, sqrt
from typing import Dict, Tuple

# A position is fully described by (zone-name, row, col).
Position = Tuple[str, int, int]

STORAGE = "storage"
ENTANGLEMENT = "entanglement"


@dataclass(frozen=True)
class Zone:
    """A rectangular grid of trap sites."""

    name: str
    rows: int
    cols: int

    @property
    def capacity(self) -> int:
        return self.rows * self.cols

    def contains(self, row: int, col: int) -> bool:
        return 0 <= row < self.rows and 0 <= col < self.cols


@dataclass
class HardwareSpec:
    """Full specification of the neutral-atom machine.

    Defaults follow the problem statement: a 40x20 storage zone and a 2x20
    entanglement zone.  Timing/fidelity numbers are representative values for
    current neutral-atom hardware and can be overridden.
    """

    storage: Zone = field(default_factory=lambda: Zone(STORAGE, 40, 20))
    entanglement: Zone = field(default_factory=lambda: Zone(ENTANGLEMENT, 2, 20))

    # Physical separation between zones, in site units, used to charge the
    # transport cost of crossing from storage into the entanglement region.
    zone_gap: float = 4.0

    # --- timing parameters (microseconds) ---
    t_1q: float = 1.0          # single-qubit gate
    t_2q: float = 0.4          # Rydberg CZ
    t_measure: float = 5.0     # readout
    t_move_per_site: float = 1.5  # transport time per site of travel
    t_move_fixed: float = 15.0    # fixed AOD pick-up/drop overhead per move stage

    # --- fidelity parameters ---
    f_1q: float = 0.9995
    f_2q: float = 0.995
    f_measure: float = 0.99
    # Coherence time (us); movement/idle time costs fidelity via exp(-t/T2).
    t2_coherence: float = 1.5e6
    # Per-move atom-transfer survival probability.
    f_transfer: float = 0.9999

    # --- AOD movement constraints ---------------------------------------
    # Maximum number of atoms that a single AOD movement batch can transport
    # simultaneously.  Moves beyond this are split into additional batches.
    aod_max_atoms: int = 40
    # Minimum allowed centre-to-centre separation between any two atoms
    # (site units).  Parallel trajectories that ever come closer than this
    # are considered colliding and must be scheduled in separate batches.
    min_atom_separation: float = 1.0
    # Maximum travel distance of a single move (site units).
    aod_max_distance: float = 60.0
    # Kinematic limits of the AOD used to build smooth trajectories.
    aod_max_velocity: float = 1.0        # sites / us
    aod_max_acceleration: float = 1.0    # sites / us^2
    # Maximum duration of a single move (us).
    aod_max_move_duration: float = 1000.0
    # If False the AOD cannot cross traps, so the relative ordering of atoms
    # must be preserved (no crossing trajectories inside one batch).
    allow_trap_crossing: bool = False

    def zone(self, name: str) -> Zone:
        if name == self.storage.name:
            return self.storage
        if name == self.entanglement.name:
            return self.entanglement
        raise KeyError(f"unknown zone {name!r}")

    def valid(self, pos: Position) -> bool:
        zone, row, col = pos
        try:
            return self.zone(zone).contains(row, col)
        except KeyError:
            return False

    def coordinate(self, pos: Position) -> Tuple[float, float]:
        """Map a position to a global (x, y) used for distance computations.

        The entanglement zone is placed directly above the storage zone with a
        ``zone_gap`` of empty space between them so that crossing zones is
        appropriately more expensive than moving within a zone.
        """
        zone, row, col = pos
        if zone == self.storage.name:
            y = float(row)
        else:  # entanglement zone sits above storage
            y = -(self.entanglement.rows - row) - self.zone_gap
        return float(col), y

    def distance(self, a: Position, b: Position) -> float:
        ax, ay = self.coordinate(a)
        bx, by = self.coordinate(b)
        return hypot(ax - bx, ay - by)

    def move_duration(self, distance: float) -> float:
        """Duration (us) of a smooth AOD move covering ``distance`` sites.

        A symmetric trapezoidal velocity profile bounded by
        :attr:`aod_max_velocity` and :attr:`aod_max_acceleration` is used, which
        approximates the smooth (low motional-excitation) trajectories real AOD
        hardware follows.  Short moves that never reach top speed use the
        triangular (accelerate-then-decelerate) limit.
        """
        if distance <= 0.0:
            return 0.0
        v = self.aod_max_velocity
        a = self.aod_max_acceleration
        ramp_distance = (v * v) / a  # distance to accelerate to v and back down
        if distance <= ramp_distance:
            return 2.0 * sqrt(distance / a)
        return distance / v + v / a

    def interaction_columns(self) -> int:
        """Number of independent two-qubit interaction slots.

        Two atoms placed in the same column of the 2-row entanglement zone
        (rows 0 and 1) form one interaction site, so the number of parallel
        CZ gates per Rydberg pulse equals the number of columns.
        """
        return self.entanglement.cols

    def as_dict(self) -> Dict:
        return {
            "storage": {"rows": self.storage.rows, "cols": self.storage.cols},
            "entanglement": {
                "rows": self.entanglement.rows,
                "cols": self.entanglement.cols,
            },
            "zone_gap": self.zone_gap,
            "timing": {
                "t_1q": self.t_1q,
                "t_2q": self.t_2q,
                "t_measure": self.t_measure,
                "t_move_per_site": self.t_move_per_site,
                "t_move_fixed": self.t_move_fixed,
            },
            "fidelity": {
                "f_1q": self.f_1q,
                "f_2q": self.f_2q,
                "f_measure": self.f_measure,
                "t2_coherence": self.t2_coherence,
                "f_transfer": self.f_transfer,
            },
            "aod": {
                "aod_max_atoms": self.aod_max_atoms,
                "min_atom_separation": self.min_atom_separation,
                "aod_max_distance": self.aod_max_distance,
                "aod_max_velocity": self.aod_max_velocity,
                "aod_max_acceleration": self.aod_max_acceleration,
                "aod_max_move_duration": self.aod_max_move_duration,
                "allow_trap_crossing": self.allow_trap_crossing,
            },
        }


def default_hardware() -> HardwareSpec:
    """Return the machine described in the problem statement."""
    return HardwareSpec()
