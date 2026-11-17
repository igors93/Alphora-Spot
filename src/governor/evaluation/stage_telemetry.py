"""Stage telemetry (G9): one event per evaluation stage, summarized in the cycle metrics.

``StageRunner`` emits ``evaluation_stage`` on the ``TELEMETRY`` channel of the event API for
every stage it runs. No file sink writes that channel: a stage event per symbol and cycle would
grow the run files without bound. ``StageTelemetry`` is the in-process sink that keeps counts and
durations per stage until ``CycleTelemetry`` adds them to the next ``governor_cycle_metrics``.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from utils.events import EventChannel, EventRecord

STAGE_EVENT = "evaluation_stage"
STAGE_COMPONENT = "governor.evaluation"
_DURATION_SAMPLES = 256


@dataclass
class _StageStats:
    count: int = 0
    errors: int = 0
    terminal: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    durations: deque[float] = field(default_factory=lambda: deque(maxlen=_DURATION_SAMPLES))
    classifications: dict[str, int] = field(default_factory=dict)

    def add(self, payload: Any) -> None:
        duration = _float(payload.get("duration_ms"))
        self.count += 1
        self.total_ms += duration
        self.max_ms = max(self.max_ms, duration)
        self.durations.append(duration)
        if payload.get("status") == "ERROR":
            self.errors += 1
        if payload.get("terminal"):
            self.terminal += 1
            classification = str(payload.get("classification") or "")
            if classification:
                self.classifications[classification] = (
                    self.classifications.get(classification, 0) + 1
                )

    def summary(self) -> dict[str, Any]:
        ordered = sorted(self.durations)
        return {
            "count": self.count,
            "errors": self.errors,
            "terminal": self.terminal,
            "total_ms": round(self.total_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "p50_ms": round(_percentile(ordered, 0.50), 3),
            "p95_ms": round(_percentile(ordered, 0.95), 3),
            "terminal_classifications": dict(sorted(self.classifications.items())),
        }


class StageTelemetry:
    """Sink of ``evaluation_stage`` events: per-stage statistics since the last summary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, _StageStats] = {}

    def accepts(self, record: EventRecord) -> bool:
        return record.channel is EventChannel.TELEMETRY and record.event_type == STAGE_EVENT

    def write(self, record: EventRecord) -> None:
        stage = str(record.payload.get("stage") or "")
        if not stage:
            return
        with self._lock:
            self._stages.setdefault(stage, _StageStats()).add(record.payload)

    def summary_and_reset(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            stages, self._stages = self._stages, {}
        return {name: stats.summary() for name, stats in stages.items()}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


__all__ = ["STAGE_COMPONENT", "STAGE_EVENT", "StageTelemetry"]
