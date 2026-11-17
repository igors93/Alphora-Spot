"""Tests for Etapa 4: coalestra metrics and event sink adapters."""

from __future__ import annotations

from market_data.snapshot_provider.event_sink import SnapshotEventSink
from market_data.snapshot_provider.metrics_sink import SnapshotMetricsSink

# ──────────────────────────────────────────────────────────────────────────────
# MetricsSink tests
# ──────────────────────────────────────────────────────────────────────────────


def test_metrics_sink_start_enables_routing(tmp_path, monkeypatch):
    recorded = []

    def fake_observe(event_type, *, category, **payload):
        recorded.append((category, event_type, payload))

    monkeypatch.setattr("market_data.snapshot_provider.metrics_sink.observe_event", fake_observe)
    sink = SnapshotMetricsSink()
    sink.start()
    sink.increment("cache_hit", 1)

    assert len(recorded) == 1
    assert recorded[0][2]["metric"] == "cache_hit"
    assert recorded[0][2]["metric_type"] == "counter"


def test_metrics_sink_stopped_does_not_route():
    recorded = []
    sink = SnapshotMetricsSink()
    sink.stop()
    sink.increment("cache_hit", 1)
    assert recorded == []


def test_metrics_sink_stop_disables_routing(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        "market_data.snapshot_provider.metrics_sink.observe_event",
        lambda *a, **kw: recorded.append(kw),
    )
    sink = SnapshotMetricsSink()
    sink.start()
    sink.increment("x", 1)
    assert len(recorded) == 1

    sink.stop()
    sink.increment("y", 2)
    assert len(recorded) == 1  # no new events after stop


def test_metrics_sink_high_cardinality_labels_are_filtered(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "market_data.snapshot_provider.metrics_sink.observe_event",
        lambda *a, **kw: captured.update(kw),
    )
    sink = SnapshotMetricsSink()
    sink.start()
    sink.increment(
        "resolve",
        1,
        resource_namespace="market",
        resource_name="state",
        symbol="BTCUSDT",  # high-cardinality — must be filtered
        account="user123",  # high-cardinality — must be filtered
        source="alphora-rest",
        status="ignored_lower_authority",
        reason="snapshot_deadline",
        operation="publish",
    )

    assert "symbol" not in captured
    assert "account" not in captured
    assert captured.get("resource_namespace") == "market"
    assert captured.get("resource_name") == "state"
    assert captured.get("source") == "alphora-rest"
    assert captured.get("status") == "ignored_lower_authority"
    assert captured.get("reason") == "snapshot_deadline"
    assert captured.get("operation") == "publish"


def test_metrics_sink_observe_records_gauge(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "market_data.snapshot_provider.metrics_sink.observe_event",
        lambda *a, **kw: captured.update(kw),
    )
    sink = SnapshotMetricsSink()
    sink.start()
    sink.observe("latency_ms", 42.5, resource_namespace="market")

    assert captured["metric"] == "latency_ms"
    assert captured["metric_type"] == "gauge"
    assert captured["value"] == 42.5


def test_metrics_sink_errors_do_not_propagate(monkeypatch):
    def exploding_audit(*a, **kw):
        raise RuntimeError("audit failed")

    monkeypatch.setattr("market_data.snapshot_provider.metrics_sink.observe_event", exploding_audit)
    sink = SnapshotMetricsSink()
    sink.start()
    # Must not raise
    sink.increment("x", 1)
    sink.observe("y", 2.0)


def test_metrics_sink_idempotent_start_stop():
    sink = SnapshotMetricsSink()
    sink.start()
    sink.start()
    sink.stop()
    sink.stop()
    assert not sink._active


# ──────────────────────────────────────────────────────────────────────────────
# EventSink tests
# ──────────────────────────────────────────────────────────────────────────────


def test_event_sink_routes_known_event_types(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda event_type, **kw: recorded.append(((kw["category"], event_type), kw)),
    )
    sink = SnapshotEventSink()
    sink.start()
    sink.emit(
        "resource_publish_ignored",
        resource="account:position:BTCUSDT",
        source="alphora-rest-private",
        status="ignored_lower_authority",
        candidate_authority_rank=30,
        cached_authority_rank=300,
    )

    assert len(recorded) == 1
    category, event_type = recorded[0][0]
    assert event_type == "coalestra_resource_publish_ignored"
    assert recorded[0][1]["status"] == "ignored_lower_authority"
    assert recorded[0][1]["candidate_authority_rank"] == "30"
    assert recorded[0][1]["cached_authority_rank"] == "300"


def test_event_sink_routes_unknown_event_types_safely(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda event_type, **kw: recorded.append(((kw["category"], event_type), kw)),
    )
    sink = SnapshotEventSink()
    sink.start()
    sink.emit("internal_debug_trace", foo="bar")

    assert len(recorded) == 1
    assert recorded[0][0][1] == "coalestra_unknown_event"
    assert recorded[0][1]["unknown_coalestra_event_type"] == "internal_debug_trace"
    assert sink._unknown_event_count == 1


def test_event_sink_routes_snapshot_session_revalidated(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda event_type, **kw: recorded.append(((kw["category"], event_type), kw)),
    )
    sink = SnapshotEventSink()
    sink.start()
    sink.emit(
        "snapshot_session_revalidated",
        committed=True,
        failed=0,
        retained_previous=False,
        force_refresh=False,
    )

    assert len(recorded) == 1
    assert recorded[0][0][1] == "coalestra_snapshot_session_revalidated"
    assert recorded[0][1]["committed"] == "True"
    assert recorded[0][1]["failed"] == "0"
    assert recorded[0][1]["retained_previous"] == "False"


def test_event_sink_routes_try_and_deadline_pressure_events(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda event_type, **kw: recorded.append(((kw["category"], event_type), kw)),
    )
    sink = SnapshotEventSink()

    sink.emit(
        "snapshot_try_request_resolved",
        snapshot_id="cycle-1",
        state="accepted",
        resources=3,
        errors=0,
    )
    sink.emit(
        "source_dispatch_under_deadline_pressure",
        source="alphora-rest-private",
        resource="account:summary",
        transport_timeout_seconds=6.0,
        available_budget_seconds=5.9,
        gap_seconds=0.1,
    )

    assert [call[0][1] for call in recorded] == [
        "coalestra_snapshot_try_request_resolved",
        "coalestra_source_dispatch_under_deadline_pressure",
    ]
    assert recorded[0][1]["state"] == "accepted"
    assert recorded[1][1]["gap_seconds"] == "0.1"
    assert sink._unknown_event_count == 0


def test_event_sink_sanitizes_high_cardinality_payload(monkeypatch):
    captured_kw = {}

    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda *a, **kw: captured_kw.update(kw),
    )
    sink = SnapshotEventSink()
    sink.start()
    sink.emit(
        "source_circuit_open",
        resource_namespace="account",
        resource_name="position",
        subject="BTCUSDT",  # high-cardinality — must be filtered
        account="acc-123",  # high-cardinality — must be filtered
        error_type="CircuitOpenError",
    )

    assert "subject" not in captured_kw
    assert "account" not in captured_kw
    assert captured_kw.get("resource_namespace") == "account"
    assert captured_kw.get("error_type") == "CircuitOpenError"


def test_event_sink_stopped_does_not_route(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda *a, **kw: recorded.append(kw),
    )
    sink = SnapshotEventSink()
    sink.stop()
    sink.emit("source_timeout")
    assert recorded == []


def test_event_sink_errors_do_not_propagate(monkeypatch):
    def exploding_audit(*a, **kw):
        raise RuntimeError("audit failed")

    monkeypatch.setattr("market_data.snapshot_provider.event_sink.observe_event", exploding_audit)
    sink = SnapshotEventSink()
    sink.start()
    # Must not raise
    sink.emit("snapshot_session_revalidated", committed=True, failed=0)


def test_event_sink_idempotent_start_stop():
    sink = SnapshotEventSink()
    sink.start()
    sink.start()
    sink.stop()
    sink.stop()
    assert not sink._active
