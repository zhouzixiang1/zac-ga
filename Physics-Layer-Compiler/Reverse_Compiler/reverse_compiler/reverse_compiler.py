"""ZAC reverse compiler.

Replays a ZAC hardware schedule (ZAIR ``*_code.json`` dict) in chronological
order and reconstructs the corresponding physical-qubit circuit as a
:class:`~reverse_compiler.circuit_ir.CircuitIR`.

The replay consumes the *native* ZAC instruction types (``init``, ``1qGate``,
``rydberg``, ``rearrangeJob``) and a small, backward-compatible superset that
covers operations the stock pipeline does not currently emit but which the
hardware model supports (``measure``, ``reset``, explicit quantum ``swap``,
global single-qubit pulses, coordinate-targeted gates, atom loss, ...).

Core semantics
--------------
* Each physical qubit is associated with one atom.
* ``MOVE``/``rearrangeJob`` updates atom position/zone only and emits **no** gate.
* Two atoms exchanging spatial positions does **not** emit ``SWAP``.
* Only an explicit quantum ``SWAP`` operation emits a ``SWAP`` gate.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from .circuit_ir import CircuitIR, CircuitOperation
from .state import Position, ReplayState

logger = logging.getLogger(__name__)


class ReverseCompileError(RuntimeError):
    """Raised when a schedule cannot be reconstructed unambiguously."""


# Parameter-free single-qubit gate name normalisation (lowercase -> canonical).
_FIXED_1Q = {
    "x": "X",
    "y": "Y",
    "z": "Z",
    "h": "H",
    "s": "S",
    "sdg": "SDG",
    "t": "T",
    "tdg": "TDG",
}
# Single-qubit gates that map onto a fixed rotation in stim/qiskit terms.
_SX = {"sx": ("RX", math.pi / 2), "sxdg": ("RX", -math.pi / 2)}
# Rotation gates carrying a single angle parameter.
_ROT_1Q = {"rx": "RX", "ry": "RY", "rz": "RZ", "p": "RZ", "u1": "RZ"}
# Identity-like names that contribute no operation.
_IDENTITY = {"id", "i", "delay"}

# Two-qubit entangling gate normalisation for ``rydberg`` / generic 2q ops.
_TWO_QUBIT = {
    "cz": "CZ",
    "cx": "CX",
    "cnot": "CX",
    "swap": "SWAP",
}


class ReverseCompiler:
    """Reconstruct a physical-qubit circuit from a ZAC schedule."""

    def __init__(self, *, strict: bool = True) -> None:
        """
        Args:
            strict: when ``True`` (default) a single-qubit gate whose angle the
                stock ZAIR does not record (e.g. a bare ``u3``) raises an error
                instead of being silently dropped.
        """
        self.strict = strict
        self.state = ReplayState()
        self._layer = 0

    # ------------------------------------------------------------------ public
    def compile(self, zair: dict[str, Any]) -> CircuitIR:
        """Reconstruct the circuit described by ``zair``."""
        if "instructions" not in zair:
            raise ReverseCompileError("ZAIR dict has no 'instructions'")

        self.state = ReplayState()
        self._layer = 0
        ir = CircuitIR(name=str(zair.get("name", "reconstructed")))
        ir.metadata["architecture_spec_path"] = zair.get("architecture_spec_path")

        instructions = zair["instructions"]
        if not instructions or instructions[0].get("type") != "init":
            raise ReverseCompileError("first instruction must be 'init'")

        # Optional architecture-provided trap table and explicit qubit binding.
        self._load_trap_table(zair)

        for inst in instructions:
            self._dispatch(inst, ir, zair)

        ir.metadata["num_atoms"] = len(self.state.atom_to_qubit)
        return ir

    # --------------------------------------------------------------- dispatch
    def _dispatch(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        itype = inst.get("type")
        handler = {
            "init": self._handle_init,
            "1qGate": self._handle_1q,
            "global_1qGate": self._handle_global_1q,
            "rydberg": self._handle_rydberg,
            "2qGate": self._handle_two_qubit,
            "rearrangeJob": self._handle_move,
            "move": self._handle_move,
            "MOVE": self._handle_move,
            "swap": self._handle_swap,
            "SWAP": self._handle_swap,
            "measure": self._handle_measure,
            "MEASURE": self._handle_measure,
            "reset": self._handle_reset,
            "RESET": self._handle_reset,
            "barrier": self._handle_timing,
            "BARRIER": self._handle_timing,
            "delay": self._handle_timing,
            "DELAY": self._handle_timing,
            "loss": self._handle_loss,
            "deactivate": self._handle_loss,
            "activate": self._handle_activate,
        }.get(itype)
        if handler is None:
            raise ReverseCompileError(f"unsupported instruction type {itype!r}")
        handler(inst, ir, zair)

    # --------------------------------------------------------------- handlers
    def _handle_init(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        init_locs = inst.get("init_locs", [])
        explicit = zair.get("qubit_to_atom")
        for entry in init_locs:
            atom = int(entry[0])
            position: Position = (int(entry[1]), int(entry[2]), int(entry[3]))
            self.state.place_atom(atom, position)
            self.state.activate(atom)
        # Default identity qubit<->atom binding, overridable by explicit map.
        if explicit:
            for q, a in explicit.items():
                self.state.bind_qubit(int(q), int(a))
        else:
            for atom in self.state.atom_to_position:
                self.state.bind_qubit(atom, atom)
        # Atoms can be marked lost/inactive at init time.
        for atom in inst.get("inactive", []) + inst.get("lost", []):
            self.state.deactivate(int(atom))
        self._advance_layer()

    def _handle_1q(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        timestamp = inst.get("begin_time")
        seen: set[int] = set()
        for gate in inst.get("gates", []):
            target = self._gate_target_1q(gate)
            qubit = self.state.resolve_target(target)
            if qubit in seen:
                raise ReverseCompileError(
                    f"conflict: qubit {qubit} used twice in 1qGate layer"
                )
            seen.add(qubit)
            op = self._make_1q_op(
                gate.get("name", inst.get("unitary", "")),
                gate.get("params", gate.get("param", ())),
                qubit,
                layer,
                timestamp,
            )
            if op is not None:
                op.metadata["atom"] = self.state.qubit_to_atom.get(qubit)
                op.metadata["position"] = self.state.atom_to_position.get(
                    op.metadata["atom"]
                )
                op.metadata["source"] = "1qGate"
                ir.add(op)

    def _handle_global_1q(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        timestamp = inst.get("begin_time")
        name = inst.get("name", inst.get("unitary", inst.get("gate", "")))
        params = inst.get("params", inst.get("param", ()))
        atoms = self._global_atom_mask(inst)
        for atom in sorted(atoms):
            qubit = self.state.qubit_of_atom(atom)
            op = self._make_1q_op(name, params, qubit, layer, timestamp)
            if op is not None:
                op.metadata.update(
                    {
                        "atom": atom,
                        "position": self.state.atom_to_position.get(atom),
                        "source": "global_1qGate",
                        "global": True,
                    }
                )
                ir.add(op)

    def _handle_rydberg(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        timestamp = inst.get("begin_time")
        zone = inst.get("zone_id")
        seen: set[int] = set()
        for gate in inst.get("gates", []):
            name = _TWO_QUBIT.get(str(gate.get("name", "cz")).lower(), "CZ")
            q0 = self.state.resolve_target(self._two_qubit_target(gate, "q0"))
            q1 = self.state.resolve_target(self._two_qubit_target(gate, "q1"))
            if q0 in seen or q1 in seen:
                raise ReverseCompileError(
                    f"conflict: qubit reused in rydberg layer ({q0}, {q1})"
                )
            seen.update((q0, q1))
            op = CircuitOperation(
                name=name,
                qubits=(q0, q1),
                layer=layer,
                timestamp=timestamp,
                metadata={
                    "source": "rydberg",
                    "zone": zone,
                    "atoms": (
                        self.state.qubit_to_atom.get(q0),
                        self.state.qubit_to_atom.get(q1),
                    ),
                },
            )
            ir.add(op)

    def _handle_two_qubit(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        # Generic 2-qubit gate instruction (extension).
        self._handle_rydberg(inst, ir, zair)

    def _handle_move(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        # Movement updates positions/zones only; it never emits a gate and never
        # changes the qubit<->atom association.
        if "end_locs" in inst:
            self.state.apply_end_locs(inst["end_locs"])
        elif "locs" in inst:  # generic move: list of [id, a, r, c]
            self.state.apply_end_locs(inst["locs"])
        elif "atom" in inst and "position" in inst:
            pos = tuple(inst["position"])
            self.state.move_atom(int(inst["atom"]), pos)  # type: ignore[arg-type]
        # No advance_layer: movement is hardware-only and produces no IR op.

    def _handle_swap(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        targets = inst.get("qubits", inst.get("targets"))
        if not targets or len(targets) != 2:
            raise ReverseCompileError("swap requires exactly two targets")
        q0 = self.state.resolve_target(targets[0])
        q1 = self.state.resolve_target(targets[1])
        ir.add(
            CircuitOperation(
                name="SWAP",
                qubits=(q0, q1),
                layer=layer,
                timestamp=inst.get("begin_time"),
                metadata={"source": "swap", "explicit_quantum_swap": True},
            )
        )

    def _handle_measure(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        targets = inst.get("qubits", inst.get("targets", []))
        cbits = inst.get("classical_bits", inst.get("clbits", []))
        for i, target in enumerate(targets):
            qubit = self.state.resolve_target(target)
            cbit = int(cbits[i]) if i < len(cbits) else qubit
            ir.add(
                CircuitOperation(
                    name="MEASURE",
                    qubits=(qubit,),
                    classical_bits=(cbit,),
                    layer=layer,
                    timestamp=inst.get("begin_time"),
                    metadata={"source": "measure"},
                )
            )

    def _handle_reset(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        targets = inst.get("qubits", inst.get("targets", []))
        for target in targets:
            qubit = self.state.resolve_target(target)
            ir.add(
                CircuitOperation(
                    name="RESET",
                    qubits=(qubit,),
                    layer=layer,
                    timestamp=inst.get("begin_time"),
                    metadata={"source": "reset"},
                )
            )

    def _handle_timing(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        layer = self._advance_layer()
        name = "DELAY" if str(inst.get("type", "")).lower() == "delay" else "BARRIER"
        targets = inst.get("qubits", inst.get("targets"))
        if targets:
            qubits = tuple(self.state.resolve_target(t) for t in targets)
        else:
            qubits = tuple(sorted(self.state.qubit_to_atom))
        params = ()
        if name == "DELAY" and "duration" in inst:
            params = (float(inst["duration"]),)
        ir.add(
            CircuitOperation(
                name=name,
                qubits=qubits,
                params=params,
                layer=layer,
                timestamp=inst.get("begin_time"),
                metadata={"source": str(inst.get("type"))},
            )
        )

    def _handle_loss(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        for target in inst.get("atoms", inst.get("qubits", [])):
            atom = self.state._resolve_atom(target)
            self.state.deactivate(atom)

    def _handle_activate(self, inst: dict, ir: CircuitIR, zair: dict) -> None:
        for target in inst.get("atoms", inst.get("qubits", [])):
            atom = self.state._resolve_atom(target)
            self.state.activate(atom)

    # ----------------------------------------------------------------- helpers
    def _advance_layer(self) -> int:
        layer = self._layer
        self._layer += 1
        return layer

    def _load_trap_table(self, zair: dict) -> None:
        traps = zair.get("trap_to_position")
        if isinstance(traps, dict):
            for tid, pos in traps.items():
                self.state.trap_to_position[int(tid)] = tuple(pos)  # type: ignore[arg-type]

    @staticmethod
    def _gate_target_1q(gate: dict):
        if "q" in gate:
            return int(gate["q"])
        for key in ("atom", "qubit", "position", "coord", "trap", "target"):
            if key in gate:
                value = gate[key]
                return value if key == "target" else {key: value}
        raise ReverseCompileError(f"1q gate has no target: {gate!r}")

    @staticmethod
    def _two_qubit_target(gate: dict, key: str):
        if key in gate:
            return int(gate[key]) if isinstance(gate[key], (int, float)) else gate[key]
        raise ReverseCompileError(f"2q gate missing {key}: {gate!r}")

    def _global_atom_mask(self, inst: dict) -> set[int]:
        """Resolve which atoms a global pulse actually affects."""
        active_only = inst.get("active_only", True)
        if "atoms" in inst or "mask" in inst:
            base = {
                self.state._resolve_atom(a)
                for a in inst.get("atoms", inst.get("mask", []))
            }
        else:
            base = set(self.state.atom_to_position)
        # Zone restriction: keep atoms whose array/SLM id is in the zone set.
        zone = inst.get("zone")
        if zone is not None:
            zones = {zone} if isinstance(zone, int) else set(zone)
            base = {
                a
                for a in base
                if self.state.atom_to_position.get(a, (None,))[0] in zones
            }
        # Exclusion / shelving / loss masks.
        for key in ("exclude", "shelved", "lost"):
            for a in inst.get(key, []):
                base.discard(self.state._resolve_atom(a))
        if active_only:
            base &= self.state.active_atoms
        return base

    def _make_1q_op(
        self,
        raw_name: str,
        params: Any,
        qubit: int,
        layer: int,
        timestamp: float | None,
    ) -> CircuitOperation | None:
        name = str(raw_name).lower()
        param_tuple = self._as_params(params)

        if name in _IDENTITY and not param_tuple:
            return None
        if name in _FIXED_1Q:
            return CircuitOperation(
                _FIXED_1Q[name], (qubit,), layer=layer, timestamp=timestamp
            )
        if name in _SX:
            canon, angle = _SX[name]
            return CircuitOperation(
                canon, (qubit,), params=(angle,), layer=layer, timestamp=timestamp
            )
        if name in _ROT_1Q:
            if not param_tuple:
                return self._missing_angle(name, qubit)
            return CircuitOperation(
                _ROT_1Q[name],
                (qubit,),
                params=param_tuple[:1],
                layer=layer,
                timestamp=timestamp,
            )
        if name in ("u", "u3", "u2"):
            if not param_tuple:
                return self._missing_angle(name, qubit)
            return CircuitOperation(
                "U", (qubit,), params=param_tuple, layer=layer, timestamp=timestamp
            )
        # Unknown but parametrised gate: keep its name and params verbatim.
        return CircuitOperation(
            raw_name.upper(),
            (qubit,),
            params=param_tuple,
            layer=layer,
            timestamp=timestamp,
        )

    def _missing_angle(self, name: str, qubit: int) -> CircuitOperation | None:
        msg = (
            f"single-qubit gate {name!r} on qubit {qubit} has no recorded angle; "
            "the stock ZAC schedule does not store rotation parameters"
        )
        if self.strict:
            raise ReverseCompileError(msg)
        logger.warning("%s; dropping", msg)
        return None

    @staticmethod
    def _as_params(params: Any) -> tuple[float, ...]:
        if params is None:
            return ()
        if isinstance(params, (int, float)):
            return (float(params),)
        return tuple(float(p) for p in params)


def reverse_compile(zair: dict[str, Any], *, strict: bool = True) -> CircuitIR:
    """Convenience wrapper around :class:`ReverseCompiler`."""
    return ReverseCompiler(strict=strict).compile(zair)
