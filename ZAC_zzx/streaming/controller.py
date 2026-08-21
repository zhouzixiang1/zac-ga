"""Checkpoint scheduling for resumable Large compilation.

The controller deliberately owns only *when* a checkpoint is committed.  The
compiler remains responsible for constructing the complete :class:`Checkpoint`
state.  A flush hook is run before the atomic checkpoint write so a checkpoint
can never refer to event bytes that are still buffered in userspace.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .checkpoint import Checkpoint, save_checkpoint


@dataclass(frozen=True)
class CheckpointPolicy:
    """Frozen Large checkpoint cadence from the experiment plan."""

    layer_interval: int = 1_000
    time_interval_seconds: float = 300.0

    def validate(self) -> None:
        if self.layer_interval <= 0:
            raise ValueError("checkpoint layer_interval must be positive")
        if self.time_interval_seconds <= 0:
            raise ValueError("checkpoint time_interval_seconds must be positive")


@dataclass(frozen=True)
class CheckpointDecision:
    saved: bool
    reasons: tuple[str, ...] = ()
    layer: Optional[int] = None
    path: Optional[str] = None


CheckpointFactory = Callable[[], Checkpoint]


class CheckpointController:
    """Atomically save every 1,000 layers or five minutes, whichever is first.

    ``maybe_checkpoint`` is cheap when no checkpoint is due and does not invoke
    the state factory.  Long-running work within one layer can call the same
    method as a heartbeat with the unchanged layer number; the time trigger will
    still fire.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        input_sha256: str,
        config_sha256: str,
        policy: CheckpointPolicy | None = None,
        start_layer: int = 0,
        before_save: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.path = Path(path)
        self.input_sha256 = str(input_sha256)
        self.config_sha256 = str(config_sha256)
        self.policy = policy or CheckpointPolicy()
        self.policy.validate()
        if start_layer < 0:
            raise ValueError("start_layer must be non-negative")
        self._last_layer = int(start_layer)
        self._clock = clock
        self._last_time = float(clock())
        self._before_save = before_save

    @property
    def last_checkpoint_layer(self) -> int:
        return self._last_layer

    def resume_from(self, checkpoint: Checkpoint) -> None:
        """Reset cadence after loading and validating a prior checkpoint."""

        checkpoint.validate()
        self._validate_identity(checkpoint)
        self._last_layer = checkpoint.current_layer
        self._last_time = float(self._clock())

    def due_reasons(self, current_layer: int, *, now: float | None = None) -> tuple[str, ...]:
        if current_layer < self._last_layer:
            raise ValueError(
                f"checkpoint layer moved backwards: {current_layer} < {self._last_layer}"
            )
        current_time = float(self._clock() if now is None else now)
        reasons: list[str] = []
        if current_layer - self._last_layer >= self.policy.layer_interval:
            reasons.append("layer_interval")
        if current_time - self._last_time >= self.policy.time_interval_seconds:
            reasons.append("time_interval")
        return tuple(reasons)

    def maybe_checkpoint(
        self,
        current_layer: int,
        factory: CheckpointFactory,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> CheckpointDecision:
        """Save a fresh state if either cadence fires, or unconditionally on force."""

        current_time = float(self._clock() if now is None else now)
        reasons = self.due_reasons(current_layer, now=current_time)
        if force and "forced" not in reasons:
            reasons = (*reasons, "forced")
        if not reasons:
            return CheckpointDecision(saved=False)

        checkpoint = factory()
        if not isinstance(checkpoint, Checkpoint):
            raise TypeError("checkpoint factory must return Checkpoint")
        checkpoint.validate()
        self._validate_identity(checkpoint)
        if checkpoint.current_layer != current_layer:
            raise ValueError(
                "checkpoint state layer does not match scheduling layer: "
                f"{checkpoint.current_layer} != {current_layer}"
            )
        if self._before_save is not None:
            self._before_save()
        save_checkpoint(self.path, checkpoint)
        # Advance cadence only after flush + atomic save both succeed.
        self._last_layer = current_layer
        self._last_time = current_time
        return CheckpointDecision(
            saved=True,
            reasons=reasons,
            layer=current_layer,
            path=str(self.path),
        )

    def _validate_identity(self, checkpoint: Checkpoint) -> None:
        if checkpoint.input_sha256 != self.input_sha256:
            raise ValueError("checkpoint input hash mismatch")
        if checkpoint.config_sha256 != self.config_sha256:
            raise ValueError("checkpoint config hash mismatch")


__all__ = [
    "CheckpointController",
    "CheckpointDecision",
    "CheckpointFactory",
    "CheckpointPolicy",
]
