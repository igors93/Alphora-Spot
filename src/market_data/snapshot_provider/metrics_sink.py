"""Snapshot engine MetricsSink adapter that routes metrics into Alphora's audit log."""

from __future__ import annotations

import logging

from market_data.snapshot_provider import wire_names
from utils.events import EventChannel, observe_event

# Only these label keys are safe to forward — they have bounded cardinality.
# Symbol, account, and full resource-key labels are explicitly excluded to prevent
# metric series explosion in the audit log.
_SAFE_LABEL_KEYS = frozenset(
    {
        "resource_namespace",
        "resource_name",
        "source",
        "status",
        "reason",
        "operation",
    }
)

_LOGGER = logging.getLogger(__name__)


def _filter_labels(labels: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in labels.items() if k in _SAFE_LABEL_KEYS}


class SnapshotMetricsSink:
    """MetricsSink that routes snapshot engine counters and gauges to the Alphora audit log.

    Implements the engine MetricsSink protocol (increment / observe).
    Lifecycle is idempotent: calling start/stop multiple times is safe.
    Errors inside the sink are swallowed so they never propagate back into the engine.
    """

    def __init__(self, *, component: str = "market_data.snapshot_metrics") -> None:
        self._component = component
        self._active = True
        self._failure_count = 0

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def close(self) -> None:
        self.stop()

    def increment(self, metric: str, value: int = 1, **labels: str) -> None:
        if not self._active:
            return
        try:
            observe_event(
                wire_names.ENGINE_METRIC,
                category="market_data",
                channel=EventChannel.AUDIT,
                metric=metric,
                metric_type="counter",
                value=value,
                component=self._component,
                **_filter_labels(labels),
            )
        except Exception as error:
            self._failure_count += 1
            _LOGGER.debug("Snapshot metric sink failed: %s", error)

    def observe(self, metric: str, value: float, **labels: str) -> None:
        if not self._active:
            return
        try:
            observe_event(
                wire_names.ENGINE_METRIC,
                category="market_data",
                channel=EventChannel.AUDIT,
                metric=metric,
                metric_type="gauge",
                value=value,
                component=self._component,
                **_filter_labels(labels),
            )
        except Exception as error:
            self._failure_count += 1
            _LOGGER.debug("Snapshot metric sink failed: %s", error)


__all__ = ["SnapshotMetricsSink"]
