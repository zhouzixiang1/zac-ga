"""Bounded sequential provider for stock-ZAC capacity-balanced stages."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator

from zzx.algorithm_v2 import ForecastLayerProvider

from .qasm_sqlite import LayerStore, ZacStage


def count_zac_stages(store: LayerStore, max_gates: int) -> int:
    """Count balanced physical stages in SQLite without materialising them."""
    store._require_zac_view()
    if max_gates <= 0:
        raise ValueError("ZAC stage capacity must be positive")
    row = store.connection.execute(
        "SELECT COALESCE(SUM((width + ? - 1) / ?), 0) AS stage_count "
        "FROM (SELECT COUNT(*) AS width FROM events WHERE q1 IS NOT NULL "
        "GROUP BY two_qubit_layer)",
        (max_gates, max_gates),
    ).fetchone()
    return int(row["stage_count"])


class SequentialZacStageProvider(ForecastLayerProvider):
    """Serve monotonic ``L..L+3`` reads from an iterator-backed fixed ring."""

    def __init__(self, stages: Iterable[ZacStage], stage_count: int, *,
                 max_cached_stages: int = 4, start_stage: int = 0):
        if stage_count < 0:
            raise ValueError("ZAC stage count must be non-negative")
        if max_cached_stages <= 0:
            raise ValueError("ZAC stage cache size must be positive")
        if not 0 <= int(start_stage) <= int(stage_count):
            raise ValueError("ZAC provider start stage is outside the stream")
        self._stage_count = int(stage_count)
        self._max_cached_stages = int(max_cached_stages)
        self._iterator = iter(stages)
        self._next_stage = int(start_stage)
        self._cache: OrderedDict[int, ZacStage] = OrderedDict()

    @property
    def layer_count(self) -> int:
        return self._stage_count

    @property
    def stage_count(self) -> int:
        return self._stage_count

    @property
    def cached_stages(self) -> tuple[int, ...]:
        return tuple(self._cache)

    @property
    def max_cached_stages(self) -> int:
        return self._max_cached_stages

    @property
    def next_unread_stage(self) -> int:
        return self._next_stage

    def state_dict(self, *, resume_stage: int) -> dict[str, object]:
        """Describe a deterministic forward-only resume boundary.

        Cached stages are diagnostics only.  A restored provider deliberately
        refetches from ``resume_stage`` so a checkpoint never serialises SQLite
        rows and retains at most the configured forward window.
        """
        resume_stage = int(resume_stage)
        if not 0 <= resume_stage <= self.stage_count:
            raise ValueError("ZAC provider resume stage is outside the stream")
        if resume_stage < self._next_stage and resume_stage not in self._cache:
            raise ValueError("ZAC provider resume stage has already been evicted")
        return {
            "format": "zac-stage-provider-state-v1",
            "resume_stage": resume_stage,
            "stage_count": self.stage_count,
            "max_cached_stages": self.max_cached_stages,
            "cached_stages": list(self.cached_stages),
        }

    def __len__(self) -> int:
        return self._stage_count

    def _fill_to(self, index: int) -> None:
        while self._next_stage <= index:
            try:
                stage = next(self._iterator)
            except StopIteration as error:
                raise ValueError(
                    "ZAC stage iterator ended before its declared count") from error
            if stage.stage_index != self._next_stage:
                raise ValueError(
                    "non-contiguous ZAC stage iterator: expected "
                    f"{self._next_stage}, found {stage.stage_index}")
            self._cache[stage.stage_index] = stage
            self._next_stage += 1
            while len(self._cache) > self._max_cached_stages:
                self._cache.popitem(last=False)

    def stage_record(self, index: int) -> ZacStage:
        index = int(index)
        if not 0 <= index < self.stage_count:
            raise IndexError(f"ZAC stage out of range: {index}")
        stage = self._cache.get(index)
        if stage is None:
            if index < self._next_stage:
                raise IndexError(
                    f"ZAC stage {index} has been evicted from the forward-only ring")
            self._fill_to(index)
            stage = self._cache[index]
        # Keep insertion/physical-stage order rather than LRU order.  Routing
        # reads the just-compiled old stage after H=2 has prefetched L+3; making
        # that old stage MRU would evict L+1 on the next fill and break the
        # monotonic transition stream.
        return stage

    def stage(self, index: int) -> tuple[tuple[int, int], ...]:
        return tuple(gate.gate_pair for gate in self.stage_record(index).gates)

    def __getitem__(self, index: int) -> tuple[tuple[int, int], ...]:
        return self.stage(index)

    def read_layer(self, layer: int) -> tuple[tuple[int, int], ...]:
        return self.stage(layer)

    def assert_exhausted(self) -> None:
        """Verify declared count and iterator agree after the final stage."""
        if self._next_stage < self.stage_count:
            self._fill_to(self.stage_count - 1)
        try:
            extra = next(self._iterator)
        except StopIteration:
            return
        raise ValueError(
            f"ZAC stage iterator exceeds declared count at {extra.stage_index}")


class LayerStoreZacStageProvider(SequentialZacStageProvider):
    """Forward-only physical-stage ring over an open :class:`LayerStore`."""

    def __init__(self, store: LayerStore, max_gates: int, *,
                 max_cached_stages: int = 4, start_stage: int = 0):
        self.store = store
        self.max_gates = int(max_gates)
        stage_count = count_zac_stages(store, self.max_gates)
        start_stage = int(start_stage)
        super().__init__(
            self._iter_from(start_stage, stage_count),
            stage_count,
            max_cached_stages=max_cached_stages,
            start_stage=start_stage,
        )

    def _stage_locator(self, stage_index: int) -> tuple[int, int]:
        """Return ``(ASAP layer, first physical index)`` in one SQL scan."""
        row = self.store.connection.execute(
            "WITH widths AS ("
            " SELECT two_qubit_layer AS layer, COUNT(*) AS width"
            " FROM events WHERE q1 IS NOT NULL GROUP BY two_qubit_layer"
            "), chunks AS ("
            " SELECT layer, ((width + ? - 1) / ?) AS chunk_count FROM widths"
            "), offsets AS ("
            " SELECT layer, chunk_count,"
            " COALESCE(SUM(chunk_count) OVER (ORDER BY layer ROWS BETWEEN "
            " UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS first_stage"
            " FROM chunks"
            ") SELECT layer, first_stage FROM offsets"
            " WHERE first_stage<=? AND first_stage+chunk_count>?"
            " ORDER BY layer LIMIT 1",
            (self.max_gates, self.max_gates, stage_index, stage_index),
        ).fetchone()
        if row is None:
            raise ValueError(f"cannot locate physical ZAC stage {stage_index}")
        return int(row["layer"]), int(row["first_stage"])

    def _iter_from(
        self, start_stage: int, stage_count: int,
    ) -> Iterator[ZacStage]:
        """Stream exact balanced stages beginning at an absolute stage index."""
        if not 0 <= start_stage <= stage_count:
            raise ValueError("ZAC provider start stage is outside the stream")
        if start_stage == stage_count:
            return
        asap_start, physical_index = self._stage_locator(start_stage)
        for layer in self.store.iter_zac_asap_layers(start_layer=asap_start):
            width = len(layer.gates)
            if width < self.max_gates:
                chunk_count = 1
                gates_per_chunk = width
            else:
                chunk_count = (width + self.max_gates - 1) // self.max_gates
                gates_per_chunk = (width + chunk_count - 1) // chunk_count
            chunks = tuple(
                layer.gates[offset:offset + gates_per_chunk]
                for offset in range(0, width, gates_per_chunk)
            )
            if len(chunks) != chunk_count:
                raise ValueError("internal ZAC balanced-stage split mismatch")
            for chunk_index, gates in enumerate(chunks):
                stage = ZacStage(
                    stage_index=physical_index,
                    asap_layer=layer.layer,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    gates=gates,
                )
                physical_index += 1
                if stage.stage_index >= start_stage:
                    yield stage
        if physical_index != stage_count:
            raise ValueError(
                "resumed ZAC provider stage count disagrees with SQLite")


__all__ = [
    "LayerStoreZacStageProvider",
    "SequentialZacStageProvider",
    "count_zac_stages",
]
