"""Bounded forecast providers for the formal resident Large compiler."""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Sequence

from zzx.algorithm_v2 import ForecastLayerProvider

from .qasm_sqlite import LayerStore


class CachedForecastLayerProvider(ForecastLayerProvider):
    """Read deterministic CZ layers through a fixed-size LRU ring."""

    def __init__(
        self,
        layer_count: int,
        loader: Callable[[int], Sequence[Sequence[int]]],
        *,
        max_cached_layers: int = 4,
    ):
        if layer_count < 0:
            raise ValueError("forecast layer count must be non-negative")
        if max_cached_layers <= 0:
            raise ValueError("forecast cache size must be positive")
        self._layer_count = int(layer_count)
        self._loader = loader
        self._max_cached_layers = int(max_cached_layers)
        self._cache: OrderedDict[int, tuple[tuple[int, int], ...]] = OrderedDict()

    @property
    def layer_count(self) -> int:
        return self._layer_count

    @property
    def cached_layers(self) -> tuple[int, ...]:
        """Expose only cache keys for bounded-memory tests and diagnostics."""

        return tuple(self._cache)

    def read_layer(self, layer: int) -> tuple[tuple[int, int], ...]:
        if not 0 <= layer < self.layer_count:
            raise IndexError(f"forecast layer out of range: {layer}")
        cached = self._cache.get(layer)
        if cached is not None:
            self._cache.move_to_end(layer)
            return cached
        value = tuple(
            (int(gate[0]), int(gate[1])) for gate in self._loader(layer))
        self._cache[layer] = value
        self._cache.move_to_end(layer)
        while len(self._cache) > self._max_cached_layers:
            self._cache.popitem(last=False)
        return value


class LayerStoreForecastProvider(CachedForecastLayerProvider):
    """CZ-layer provider backed by an open :class:`LayerStore`."""

    def __init__(self, store: LayerStore, *, max_cached_layers: int = 4):
        count = int(store.metadata.get("two_qubit_layers", 0))

        def load(layer: int) -> tuple[tuple[int, int], ...]:
            return tuple(
                (event.qubits[0], event.qubits[1])
                for event in store.two_qubit_layer(layer))

        super().__init__(
            count, load, max_cached_layers=max_cached_layers)


__all__ = ["CachedForecastLayerProvider", "LayerStoreForecastProvider"]
