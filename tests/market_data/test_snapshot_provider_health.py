"""Tests for Etapa 5: BuilderHealth normalization and operational monitoring."""

from __future__ import annotations

import pytest

from market_data.snapshot_engine import BuilderHealth
from market_data.snapshot_provider import (
    SnapshotProvider,
    SnapshotProviderSettings,
    normalize_builder_health,
)
from market_data.snapshot_provider.submissions import SubmissionFlushSummary


def _fake_health(**overrides) -> BuilderHealth:
    defaults = dict(
        closed=False,
        background_refreshes=0,
        singleflight_in_flight=0,
        source_support_cache_entries=0,
        active_dispatch_workers=0,
        waiting_for_capacity=0,
        queue_timeout_count=0,
        source_timeout_count=0,
        deadline_exceeded_count=0,
        revalidation_attempt_count=0,
        revalidation_failure_count=0,
        pending_submissions=0,
        max_pending_submissions=1024,
    )
    defaults.update(overrides)
    return BuilderHealth(**defaults)


# ──────────────────────────────────────────────────────────────────────────────
# normalize_builder_health tests
# ──────────────────────────────────────────────────────────────────────────────


def test_normalize_builder_health_returns_dict():
    h = _fake_health()
    result = normalize_builder_health(h)
    assert isinstance(result, dict)
    assert "coalestra" in result
    assert "alphora_derived" in result


def test_normalize_builder_health_includes_all_counters():
    h = _fake_health(
        active_dispatch_workers=3,
        waiting_for_capacity=1,
        queue_timeout_count=5,
        source_timeout_count=2,
        deadline_exceeded_count=1,
        revalidation_attempt_count=10,
        revalidation_failure_count=2,
        pending_submissions=100,
        max_pending_submissions=1024,
    )
    result = normalize_builder_health(h)
    coalestra = result["coalestra"]
    assert coalestra["active_dispatch_workers"] == 3
    assert coalestra["waiting_for_capacity"] == 1
    assert coalestra["queue_timeout_count"] == 5
    assert coalestra["source_timeout_count"] == 2
    assert coalestra["deadline_exceeded_count"] == 1
    assert coalestra["revalidation_attempt_count"] == 10
    assert coalestra["revalidation_failure_count"] == 2
    assert coalestra["pending_submissions"] == 100
    assert coalestra["max_pending_submissions"] == 1024


def test_backlog_severity_normal_below_50_pct():
    h = _fake_health(pending_submissions=499, max_pending_submissions=1000)
    result = normalize_builder_health(h)
    assert result["alphora_derived"]["backlog_severity"] == "normal"
    assert result["alphora_derived"]["backlog_pct"] == pytest.approx(0.499)


def test_backlog_severity_attention_at_50_pct():
    h = _fake_health(pending_submissions=500, max_pending_submissions=1000)
    result = normalize_builder_health(h)
    assert result["alphora_derived"]["backlog_severity"] == "attention"


def test_backlog_severity_warning_at_80_pct():
    h = _fake_health(pending_submissions=800, max_pending_submissions=1000)
    result = normalize_builder_health(h)
    assert result["alphora_derived"]["backlog_severity"] == "warning"


def test_backlog_severity_critical_at_100_pct():
    h = _fake_health(pending_submissions=1000, max_pending_submissions=1000)
    result = normalize_builder_health(h)
    assert result["alphora_derived"]["backlog_severity"] == "critical"


def test_backlog_severity_normal_when_max_is_none():
    h = _fake_health(pending_submissions=500, max_pending_submissions=None)
    result = normalize_builder_health(h)
    assert result["alphora_derived"]["backlog_severity"] == "normal"
    assert result["coalestra"]["max_pending_submissions"] is None


def test_normalize_health_is_json_safe():
    import json

    h = _fake_health(pending_submissions=200, max_pending_submissions=1000)
    result = normalize_builder_health(h)
    # Must serialize without error
    serialized = json.dumps(result)
    assert len(serialized) > 0


# ──────────────────────────────────────────────────────────────────────────────
# Official assessment tests
# ──────────────────────────────────────────────────────────────────────────────


def test_official_assessment_reports_counter_increase():
    baseline = _fake_health(queue_timeout_count=2)
    current = normalize_builder_health(_fake_health(queue_timeout_count=5), previous=baseline)
    assessment = current["coalestra"]["assessment"]
    assert assessment["baseline_used"] is True
    assert "queue_timeouts_increased" in assessment["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# collect_health integration test
# ──────────────────────────────────────────────────────────────────────────────


class FakeClient:
    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100.5"}

    def exchange_info(self, *, timeout=None):
        return {"symbols": []}

    def position_risk(self, symbol=None, *, timeout=None):
        return []

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": []}


def test_collect_health_returns_dict():
    provider = SnapshotProvider(client=FakeClient())
    try:
        result = provider.collect_health("test")
        assert isinstance(result, dict)
        assert "coalestra" in result
        assert "alphora_derived" in result
        assert "assessment" in result
    finally:
        provider.close()


def test_collect_health_updates_baseline():
    provider = SnapshotProvider(client=FakeClient())
    try:
        assert provider.health.baseline is None
        provider.collect_health("first")
        assert provider.health.baseline is not None
        assert isinstance(provider.health.baseline, BuilderHealth)
        assert hasattr(provider.health.baseline, "queue_timeout_count")
    finally:
        provider.close()


def test_collect_health_after_close_returns_unavailable_payload():
    provider = SnapshotProvider(client=FakeClient())
    provider.close()

    result = provider.collect_health("after_close")

    assert result["available"] is False
    assert result["label"] == "after_close"
    assert result["error_type"] == "RuntimeError"


def test_provider_close_is_idempotent_and_stops_sinks():
    provider = SnapshotProvider(client=FakeClient())

    provider.close()
    provider.close()

    assert provider.closed is True
    assert provider.metrics_sink._active is False
    assert provider.event_sink._active is False


def test_provider_close_uses_configured_flush_timeout(monkeypatch):
    provider_settings = provider_settings_with_flush_timeout(0.25)
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=provider_settings,
    )
    recorded = []

    def flush(*, timeout_seconds=None):
        recorded.append(timeout_seconds)
        return SubmissionFlushSummary(completed=1)

    monkeypatch.setattr(provider.submissions, "flush", flush)
    try:
        provider.close()

        assert recorded == [provider_settings.submission_flush_timeout_seconds]
    finally:
        provider.close()


def test_provider_close_stops_sinks_when_builder_close_fails(monkeypatch):
    provider = SnapshotProvider(client=FakeClient())
    original_close = provider._sync_builder.close

    def close_then_fail():
        original_close()
        raise RuntimeError("synthetic close failure")

    monkeypatch.setattr(provider._sync_builder, "close", close_then_fail)

    with pytest.raises(RuntimeError):
        provider.close()

    assert provider.metrics_sink._active is False
    assert provider.event_sink._active is False


def provider_settings_with_flush_timeout(
    timeout_seconds: float,
) -> SnapshotProviderSettings:
    return SnapshotProviderSettings(submission_flush_timeout_seconds=timeout_seconds)
