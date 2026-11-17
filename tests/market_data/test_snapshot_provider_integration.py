"""Integration tests for Coalestra 0.6.1 migration.

Verifies that:
- require_capabilities() guard is called at provider init (0.6.1 capabilities present).
- REST sources declare blocking_io=True and transport_timeout_seconds in BuilderHealth.
- BuilderHealth baseline is stored as a BuilderHealth object (not a dict).
- collect_health() includes the official 0.6.1 assessment payload.
- buffer_observability is active (observability fields in health).
- SnapshotAcceptancePolicy and SnapshotConsistencyPolicy are wired to cycle session methods.
- New 0.6.1 event types are routed by the event sink.
- _call_client raises for methods without timeout support.
- Sinks are NOT manually started before builder construction (single lifecycle owner).
- CapabilityRequirementError is NOT swallowed by build_snapshot_provider.
- Requirements.txt and pyproject.toml use the embedded snapshot engine.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesNetworkError
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from domain.symbol_rules import SymbolRules
from governor.cycle_context import SnapshotReadiness
from market_data.snapshot_engine import (
    BuilderHealth,
    CapabilityRequirementError,
    Snapshot,
    SnapshotAcceptanceError,
    SnapshotConsistencyError,
    SnapshotRequest,
    SnapshotValue,
)
from market_data.snapshot_provider import (
    ACCOUNT,
    ALL_POSITIONS,
    SnapshotProvider,
    SnapshotProviderSettings,
    SnapshotTransportBudgetExceeded,
    build_snapshot_provider,
    exchange_rules,
    mark_price,
    market_state,
    open_algo_orders,
    open_orders,
    position,
    resolve_startup_policy,
)
from market_data.snapshot_provider.event_sink import _ROUTED_EVENT_TYPES, SnapshotEventSink
from market_data.snapshot_provider.sources import (
    MIN_ENTRY_AUTHORITY_RANK,
    MIN_EXIT_AUTHORITY_RANK,
    SOURCE_REST_EXCHANGE,
    SOURCE_REST_MARKET,
    SOURCE_REST_PRIVATE,
    SOURCE_RULES_DERIVED,
)

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


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


class ReadyClient(FakeClient):
    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def account_info(self, *, timeout=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "100"}]}


def _symbol_rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.10"),
    )


def _empty_position(symbol: str = "BTCUSDT") -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _entry_resource_values(symbol: str = "BTCUSDT"):
    return (
        (market_state(symbol), MarketSnapshot(symbol, Decimal("100"), [Decimal("100")])),
        (mark_price(symbol), Decimal("100")),
        (exchange_rules(symbol), _symbol_rules()),
        (ACCOUNT, {"assets": [{"asset": "USDT", "availableBalance": "100"}]}),
        (position(symbol), _empty_position(symbol)),
        (open_orders(symbol), []),
        (open_algo_orders(symbol), []),
    )


def _publish_entry_resources(
    provider: SnapshotProvider,
    *,
    observed_at: float,
    source: str | None = None,
    symbol: str = "BTCUSDT",
) -> None:
    source_by_key = {
        market_state(symbol): SOURCE_REST_MARKET,
        mark_price(symbol): SOURCE_REST_MARKET,
        exchange_rules(symbol): SOURCE_RULES_DERIVED,
        ACCOUNT: SOURCE_REST_PRIVATE,
        position(symbol): SOURCE_REST_PRIVATE,
        open_orders(symbol): SOURCE_REST_PRIVATE,
        open_algo_orders(symbol): SOURCE_REST_PRIVATE,
    }
    for key, value in _entry_resource_values(symbol):
        provider.publisher.publish(
            key,
            value,
            source=source or source_by_key[key],
            observed_at=observed_at,
            force=True,
            replace_equal=True,
        )


def _entry_snapshot_request(
    provider: SnapshotProvider,
    symbol: str = "BTCUSDT",
) -> SnapshotRequest:
    return SnapshotRequest(
        optional=[key for key, _value in _entry_resource_values(symbol)],
        acceptance_policy=provider.policies.entry_acceptance(symbol),
        consistency_policy=provider.policies.entry_consistency(),
    )


# ---------------------------------------------------------------------------
# require_capabilities guard
# ---------------------------------------------------------------------------


def test_provider_initializes_successfully_with_061_capabilities():
    """Provider init succeeds when the internal snapshot engine exposes the required capabilities."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        assert provider is not None
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# Blocking IO declarations in health snapshot
# ---------------------------------------------------------------------------


def test_rest_sources_declared_as_blocking_io():
    """REST sources must report blocking_io=True in the timeout guarantees."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        health: BuilderHealth = provider.health_snapshot()
        guarantees = dict(health.source_timeout_guarantees)
        for source_name in (SOURCE_REST_MARKET, SOURCE_REST_PRIVATE, SOURCE_REST_EXCHANGE):
            assert source_name in guarantees, f"Missing timeout guarantee for {source_name}"
            guarantee = guarantees[source_name]
            assert guarantee.blocking_io is True, f"{source_name} must declare blocking_io=True"
            assert guarantee.transport_timeout_seconds is not None, (
                f"{source_name} must declare transport_timeout_seconds"
            )
    finally:
        provider.close()


def test_transport_timeout_within_source_timeout():
    """transport_timeout_seconds must not exceed the Coalestra source timeout_seconds."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        health: BuilderHealth = provider.health_snapshot()
        guarantees = dict(health.source_timeout_guarantees)
        for source_name in (SOURCE_REST_MARKET, SOURCE_REST_PRIVATE):
            g = guarantees[source_name]
            assert g.transport_timeout_seconds <= provider.settings.rest_timeout_seconds, (
                f"{source_name}: transport_timeout ({g.transport_timeout_seconds}s) "
                f"must not exceed source timeout ({provider.settings.rest_timeout_seconds}s)"
            )
        g_exchange = guarantees[SOURCE_REST_EXCHANGE]
        assert (
            g_exchange.transport_timeout_seconds <= provider.settings.exchange_info_timeout_seconds
        ), (
            f"{SOURCE_REST_EXCHANGE}: transport_timeout ({g_exchange.transport_timeout_seconds}s) "
            f"must not exceed source timeout ({provider.settings.exchange_info_timeout_seconds}s)"
        )
    finally:
        provider.close()


def test_default_private_rest_budget_has_testnet_headroom():
    settings = SnapshotProviderSettings()

    assert settings.rest_timeout_seconds == 2.8
    assert settings.rest_transport_timeout_seconds == 2.5
    assert settings.rest_connect_timeout_seconds == 0.6
    assert settings.rest_read_timeout_seconds == 1.8
    assert settings.execution_max_age_seconds == 3.0
    assert settings.execution_max_skew_seconds == 3.0


def test_no_unsafe_blocking_sources():
    """All blocking sources must have transport_timeout_seconds — none unsafe."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        health: BuilderHealth = provider.health_snapshot()
        assert health.unsafe_blocking_source_count == 0, (
            f"Expected 0 unsafe blocking sources, got {health.unsafe_blocking_source_count}"
        )
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# BuilderHealth baseline type and to_dict(previous=...)
# ---------------------------------------------------------------------------


def test_collect_health_stores_builder_health_object():
    """_health_baseline must be a BuilderHealth instance after first collect_health call."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        assert provider.health.baseline is None
        provider.collect_health("init")
        assert isinstance(provider.health.baseline, BuilderHealth)
    finally:
        provider.close()


def test_collect_health_includes_assessment_from_to_dict():
    """collect_health return dict must include 'assessment' key from BuilderHealth.to_dict()."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        # First call: no baseline yet — assessment has baseline_used=False
        provider.collect_health("first")
        # Second call: baseline present — assessment has baseline_used=True
        result = provider.collect_health("second")
        assert "assessment" in result, "collect_health must include 'assessment' from to_dict()"
        assessment = result["assessment"]
        assert isinstance(assessment, dict)
        assert assessment.get("baseline_used") is True
        assert "severity" in assessment
    finally:
        provider.close()


def test_collect_health_returns_observability_fields():
    """Health dict must include 0.6.1 observability tracking fields."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        result = provider.collect_health("test")
        coalestra = result["coalestra"]
        assert "observability_pending" in coalestra
        assert "observability_dropped_count" in coalestra
        assert "observability_shutdown_incomplete" in coalestra
        assert "blocking_source_count" in coalestra
        assert "unsafe_blocking_source_count" in coalestra
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# Acceptance/Consistency policy factories
# ---------------------------------------------------------------------------


def test_baseline_acceptance_policy_allows_stale():
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.baseline_acceptance()
        rule = policy.rule_for(ACCOUNT)
        assert rule.allow_stale is True
        assert rule.max_age_seconds == provider.settings.max_data_age_seconds
    finally:
        provider.close()


def test_evaluation_acceptance_policy_has_max_age():
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.evaluation_acceptance(["BTCUSDT"])
        # mark_price uses max_data_age_seconds (30s) because Binance server timestamps
        # on Testnet can lag 15-30s behind wall clock, so the tighter
        # evaluation_max_age_seconds would cause spurious rejections.
        rule = policy.rule_for(mark_price("BTCUSDT"))
        assert rule.max_age_seconds == provider.settings.max_data_age_seconds
        assert rule.allow_stale is True
        # Other resources use the default evaluation_max_age_seconds rule
        default_rule = policy.default_rule
        assert default_rule.max_age_seconds == provider.settings.evaluation_max_age_seconds
        assert policy.requirements
    finally:
        provider.close()


def test_execution_acceptance_policy_has_max_age_constraint():
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.execution_acceptance()
        rule = policy.rule_for(market_state(""))
        assert rule.max_age_seconds == provider.settings.execution_max_age_seconds
        assert rule.allow_stale is False
        rules_rule = policy.rule_for(exchange_rules(""))
        assert rules_rule.max_age_seconds == provider.settings.exchange_info_ttl_seconds
        assert rules_rule.allow_stale is False
        assert policy.include_optional_resources is True
        assert policy.requirements
    finally:
        provider.close()


def test_execution_consistency_policy_uses_settings():
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.execution_consistency()
        expected_skew = provider.settings.execution_max_skew_seconds
        assert policy.max_observation_skew_seconds == expected_skew
    finally:
        provider.close()


def test_entry_readiness_is_entry_safe_when_requirements_are_satisfied():
    provider = SnapshotProvider(client=ReadyClient())
    now = time.time()
    session = provider.open_cycle_session(cycle_id="entry-safe", now=now, symbols=["BTCUSDT"])
    try:
        session.resolve_baseline()
        snapshot = session.revalidate_for_execution("BTCUSDT")
        diagnostic = provider.readiness_diagnostic(
            snapshot,
            profile="entry",
            symbol="BTCUSDT",
        )

        assert diagnostic.readiness is SnapshotReadiness.ENTRY_SAFE
        assert diagnostic.readiness.allows_entry is True
        assert diagnostic.missing_resources == ()
    finally:
        session.close()
        provider.close()


def test_entry_readiness_is_partial_when_required_resources_are_missing():
    provider = SnapshotProvider(client=ReadyClient())
    try:
        snapshot = provider.get_light_snapshot("BTCUSDT")
        diagnostic = provider.readiness_diagnostic(
            provider.last_snapshot,
            profile="entry",
            symbol="BTCUSDT",
        )

        assert snapshot.symbol == "BTCUSDT"
        assert diagnostic.readiness is SnapshotReadiness.PARTIAL
        assert "account:summary" in diagnostic.missing_resources
        assert "orders:open:BTCUSDT" in diagnostic.missing_resources
    finally:
        provider.close()


def test_entry_policy_rejects_old_resources_and_reports_readiness_violation():
    provider = SnapshotProvider(
        client=ReadyClient(),
        settings=SnapshotProviderSettings(
            light_ttl_seconds=30.0,
            heavy_ttl_seconds=30.0,
            account_ttl_seconds=30.0,
        ),
    )
    now = time.time()
    _publish_entry_resources(provider, observed_at=now - 5.0)
    try:
        with pytest.raises(SnapshotAcceptanceError) as error_info:
            provider._build_request(
                _entry_snapshot_request(provider),
                now=now,
                snapshot_id="entry-old",
            )
        diagnostic = provider.readiness_diagnostic(
            error_info.value.snapshot,
            profile="entry",
            symbol="BTCUSDT",
            error=error_info.value,
        )

        assert diagnostic.readiness is SnapshotReadiness.PARTIAL
        assert diagnostic.reason == "POLICY_VIOLATION"
        assert diagnostic.max_age_seconds is not None
        assert diagnostic.max_age_seconds > provider.settings.execution_max_age_seconds
        assert any(item["reason"] == "too_old" for item in diagnostic.acceptance_violations)
    finally:
        provider.close()


def test_entry_policy_rejects_observation_skew_and_reports_diagnostic():
    provider = SnapshotProvider(
        client=ReadyClient(),
        settings=SnapshotProviderSettings(
            light_ttl_seconds=30.0,
            heavy_ttl_seconds=30.0,
            account_ttl_seconds=30.0,
        ),
    )
    now = time.time()
    _publish_entry_resources(provider, observed_at=now)
    provider.publisher.publish(
        mark_price("BTCUSDT"),
        Decimal("100"),
        source=SOURCE_REST_MARKET,
        observed_at=now - provider.settings.execution_max_skew_seconds - 0.5,
        force=True,
        replace_equal=True,
    )
    try:
        with pytest.raises(SnapshotConsistencyError) as error_info:
            provider._build_request(
                _entry_snapshot_request(provider),
                now=now,
                snapshot_id="entry-skew",
            )
        diagnostic = provider.readiness_diagnostic(
            error_info.value.snapshot,
            profile="entry",
            symbol="BTCUSDT",
            error=error_info.value,
        )

        assert diagnostic.readiness is SnapshotReadiness.PARTIAL
        assert diagnostic.observation_skew_seconds is not None
        assert diagnostic.observation_skew_seconds > provider.settings.execution_max_skew_seconds
        assert diagnostic.consistency_violations
    finally:
        provider.close()


def test_entry_policy_rejects_insufficient_authority():
    provider = SnapshotProvider(client=ReadyClient())
    now = time.time()
    _publish_entry_resources(
        provider,
        observed_at=now,
        source=SOURCE_REST_EXCHANGE,
    )
    try:
        with pytest.raises(SnapshotAcceptanceError) as error_info:
            provider._build_request(
                _entry_snapshot_request(provider),
                now=now,
                snapshot_id="entry-low-authority",
            )
        diagnostic = provider.readiness_diagnostic(
            error_info.value.snapshot,
            profile="entry",
            symbol="BTCUSDT",
            error=error_info.value,
        )

        assert diagnostic.readiness is SnapshotReadiness.PARTIAL
        assert any(
            item["reason"] == "insufficient_authority" for item in diagnostic.acceptance_violations
        )
        assert diagnostic.authority_summary["min_authority_rank"] < MIN_ENTRY_AUTHORITY_RANK
    finally:
        provider.close()


def test_exit_readiness_accepts_minimal_exit_safe_state():
    provider = SnapshotProvider(client=ReadyClient())
    now = time.time()
    try:
        snapshot = Snapshot(
            snapshot_id="exit-safe",
            created_at=now,
            resources={
                position("BTCUSDT"): SnapshotValue(
                    key=position("BTCUSDT"),
                    value=_empty_position(),
                    source=SOURCE_REST_PRIVATE,
                    observed_at=now,
                    fetched_at=now,
                    age_seconds=0.0,
                    stale=False,
                    from_cache=False,
                    latency_ms=0.0,
                    authority_rank=MIN_EXIT_AUTHORITY_RANK,
                ),
                open_orders("BTCUSDT"): SnapshotValue(
                    key=open_orders("BTCUSDT"),
                    value=[],
                    source=SOURCE_REST_PRIVATE,
                    observed_at=now,
                    fetched_at=now,
                    age_seconds=0.0,
                    stale=False,
                    from_cache=False,
                    latency_ms=0.0,
                    authority_rank=MIN_EXIT_AUTHORITY_RANK,
                ),
            },
        )
        diagnostic = provider.readiness_diagnostic(
            snapshot,
            profile="exit",
            symbol="BTCUSDT",
        )

        assert diagnostic.readiness is SnapshotReadiness.EXIT_SAFE
        assert diagnostic.readiness.allows_exit is True
        assert "account:positions" not in diagnostic.missing_resources
        assert "orders:open-algo:BTCUSDT" not in diagnostic.missing_resources
    finally:
        provider.close()


def test_exit_policy_request_allows_exit_safe_snapshot():
    provider = SnapshotProvider(client=ReadyClient())
    now = time.time()
    provider.publisher.publish(
        position("BTCUSDT"),
        _empty_position(),
        source=SOURCE_REST_PRIVATE,
        observed_at=now,
        force=True,
        replace_equal=True,
    )
    provider.publisher.publish(
        open_orders("BTCUSDT"),
        [],
        source=SOURCE_REST_PRIVATE,
        observed_at=now,
        force=True,
        replace_equal=True,
    )
    provider.publisher.publish(
        open_algo_orders("BTCUSDT"),
        [],
        source=SOURCE_REST_PRIVATE,
        observed_at=now,
        force=True,
        replace_equal=True,
    )
    try:
        policy = provider.policies.exit_acceptance("BTCUSDT")
        snapshot = provider._build_request(
            SnapshotRequest(
                optional=[
                    ALL_POSITIONS,
                    position("BTCUSDT"),
                    open_orders("BTCUSDT"),
                    open_algo_orders("BTCUSDT"),
                ],
                acceptance_policy=policy,
                consistency_policy=provider.policies.exit_consistency(),
            ),
            now=now,
            snapshot_id="exit-safe",
        )
        diagnostic = provider.readiness_diagnostic(
            snapshot,
            profile="exit",
            symbol="BTCUSDT",
        )

        assert diagnostic.readiness is SnapshotReadiness.EXIT_SAFE
        assert diagnostic.readiness.allows_exit is True
        assert "account:positions" not in diagnostic.missing_resources
        assert "orders:open-algo:BTCUSDT" not in diagnostic.missing_resources
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# Buffer observability is active
# ---------------------------------------------------------------------------


def test_buffer_observability_reported_in_health():
    """With buffer_observability=True, the health observability_buffers dict is non-empty."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        health: BuilderHealth = provider.health_snapshot()
        assert len(health.observability_buffers) > 0, (
            "buffer_observability=True must produce non-empty observability_buffers in health"
        )
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# Event sink: 0.6.1 new event types are routed
# ---------------------------------------------------------------------------


def test_event_sink_routes_060_acceptance_failed():
    assert "snapshot_acceptance_failed" in _ROUTED_EVENT_TYPES


def test_event_sink_routes_060_consistency_failed():
    assert "snapshot_consistency_failed" in _ROUTED_EVENT_TYPES


def test_event_sink_routes_060_transport_timeout_violated():
    assert "source_transport_timeout_violated" in _ROUTED_EVENT_TYPES


def test_event_sink_routes_060_transport_budget_rejected():
    assert "source_transport_budget_rejected" in _ROUTED_EVENT_TYPES


def test_event_sink_routes_060_payload_copy_deadline_exceeded():
    assert "payload_copy_deadline_exceeded" in _ROUTED_EVENT_TYPES


def test_event_sink_routes_unknown_event_types_and_counts_them(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        "market_data.snapshot_provider.event_sink.observe_event",
        lambda event_type, **kw: recorded.append(((kw["category"], event_type), kw)),
    )
    sink = SnapshotEventSink()
    sink.start()
    sink.emit("completely_unknown_event_xyz", resource="test")
    assert sink._unknown_event_count == 1
    assert recorded[0][0][1] == "coalestra_unknown_event"
    sink.stop()


def test_event_sink_emits_acceptance_failed_without_error():
    """Emitting snapshot_acceptance_failed must not raise (sink swallows errors)."""
    sink = SnapshotEventSink()
    sink.start()
    sink.emit(
        "snapshot_acceptance_failed",
        snapshot_id="test-snap",
        violations=3,
    )
    sink.stop()


# ---------------------------------------------------------------------------
# _call_client enforces timeout parameter presence
# ---------------------------------------------------------------------------


def test_call_client_raises_for_method_without_timeout():
    """_call_client must raise RuntimeError for any method that lacks timeout support."""
    provider = SnapshotProvider(client=FakeClient())
    try:

        def no_timeout_method(symbol):
            return {}

        with pytest.raises(RuntimeError, match="timeout"):
            provider.transport.call_client(no_timeout_method, "BTCUSDT")
    finally:
        provider.close()


def test_call_client_passes_timeout_to_method():
    """_call_client must forward the effective timeout to the method."""
    received: list[object] = []

    def timeout_method(symbol, *, timeout=None):
        received.append(timeout)
        return {}

    provider = SnapshotProvider(client=FakeClient())
    try:
        provider.transport.call_client(timeout_method, "BTCUSDT")
        assert received, "method was never called"
        assert received[0] is not None, "timeout must be non-None when passed"
        assert isinstance(received[0], tuple), "transport timeout must be a (connect, read) tuple"
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# Lifecycle: sinks must NOT be active before builder construction
# ---------------------------------------------------------------------------


def test_sinks_are_ready_by_default_for_builder_buffering():
    """A freshly-created downstream sink is ready; builder close owns shutdown."""
    sink = SnapshotEventSink()
    assert sink._active is True


def test_sinks_are_ready_after_provider_init():
    """Provider does not start sinks manually; downstream sinks are ready until builder close."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        assert provider.event_sink._active is True
        assert provider.metrics_sink._active is True
    finally:
        provider.close()


def test_sinks_are_inactive_after_provider_close():
    """Builder close owns downstream sink shutdown through close()."""
    provider = SnapshotProvider(client=FakeClient())
    provider.close()
    assert provider.event_sink._active is False
    assert provider.metrics_sink._active is False


# ---------------------------------------------------------------------------
# CapabilityRequirementError must NOT be swallowed
# ---------------------------------------------------------------------------


def test_capability_requirement_error_propagates_from_builder():
    """build_snapshot_provider must re-raise CapabilityRequirementError."""
    from unittest.mock import patch

    mock_error = CapabilityRequirementError(
        missing_features=("nonexistent_feature",),
        schema_mismatches={},
    )
    with patch(
        "market_data.snapshot_provider.provider.require_capabilities",
        side_effect=mock_error,
    ):
        with pytest.raises(CapabilityRequirementError):
            build_snapshot_provider(client=FakeClient())


# ---------------------------------------------------------------------------
# Policies: baseline and evaluation must evaluate all optional resources
# ---------------------------------------------------------------------------


def test_baseline_policy_includes_optional_resources():
    """Baseline acceptance policy must have include_optional_resources=True."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.baseline_acceptance()
        assert policy.include_optional_resources is True, (
            "Baseline policy must set include_optional_resources=True so age is "
            "checked on optional resources, not silently skipped."
        )
    finally:
        provider.close()


def test_evaluation_policy_includes_optional_resources():
    """Evaluation acceptance policy must have include_optional_resources=True."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.evaluation_acceptance()
        assert policy.include_optional_resources is True
    finally:
        provider.close()


def test_execution_policy_has_minimum_authority_rank():
    """Execution policy must require minimum authority rank 30 (REST_PRIVATE level)."""
    provider = SnapshotProvider(client=FakeClient())
    try:
        policy = provider.policies.execution_acceptance()
        rule = policy.rule_for(market_state(""))
        assert rule.minimum_authority_rank is not None, (
            "Execution policy must declare minimum_authority_rank to reject low-authority sources."
        )
        assert rule.minimum_authority_rank >= 30, (
            f"Minimum authority rank must be >= 30 (SOURCE_REST_PRIVATE level), "
            f"got {rule.minimum_authority_rank}"
        )
    finally:
        provider.close()


# ---------------------------------------------------------------------------
# Version consistency: requirements.txt and pyproject.toml must agree
# ---------------------------------------------------------------------------


def test_requirements_txt_and_pyproject_use_embedded_snapshot_engine():
    """The source tree supplies Coalestra without an external dependency."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent.parent
    req_text = (root / "requirements.txt").read_text()
    pyp_text = (root / "pyproject.toml").read_text()
    assert not any(line.strip().lower().startswith("coalestra") for line in req_text.splitlines())
    assert '"coalestra==0.6.4"' not in pyp_text
    assert '"market_data*"' in pyp_text
    assert (root / "src" / "market_data" / "snapshot_engine" / "__init__.py").is_file()


def _tight_settings() -> SnapshotProviderSettings:
    return SnapshotProviderSettings(
        deadline_seconds=1.0,
        cycle_deadline_seconds=2.0,
        rest_timeout_seconds=0.5,
        rest_connect_timeout_seconds=0.05,
        rest_read_timeout_seconds=0.05,
        rest_transport_timeout_seconds=0.2,
        rest_min_attempt_timeout_seconds=0.05,
        rest_retry_delay_seconds=0.0,
        exchange_info_timeout_seconds=0.5,
        exchange_info_connect_timeout_seconds=0.05,
        exchange_info_read_timeout_seconds=0.05,
        exchange_info_transport_timeout_seconds=0.2,
    )


class PrivateTimestampRetryClient(FakeClient):
    def __init__(self):
        self.signed_calls = 0
        self.sync_calls = 0
        self.timeouts = []
        self.generation = 0

    def _time_sync_generation_snapshot(self):
        return self.generation

    def _sync_time_if_generation_current(self, expected_generation, *, timeout=None):
        self.sync_calls += 1
        self.timeouts.append(("sync", timeout))
        self.generation = expected_generation + 1
        return True, 0, self.generation

    def _signed_request_once(self, method, path, params=None, *, timeout=None):
        self.signed_calls += 1
        self.timeouts.append(("signed", timeout))
        if self.signed_calls == 1:
            raise BinanceFuturesAPIError(400, -1021, "timestamp outside recvWindow")
        return {"assets": []}


def test_private_call_retries_timestamp_sync_inside_budget():
    client = PrivateTimestampRetryClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(rest_retry_delay_seconds=0.0),
    )
    try:
        assert provider.transport.call_private_client("account_info") == {"assets": []}
        assert client.signed_calls == 2
        assert client.sync_calls == 1
        assert all(timeout is not None for _kind, timeout in client.timeouts)
    finally:
        provider.close()


def test_call_rejected_before_start_when_budget_is_insufficient():
    calls = []

    def method(*, timeout=None):
        calls.append(timeout)
        return {}

    provider = SnapshotProvider(client=FakeClient())
    try:
        with pytest.raises(SnapshotTransportBudgetExceeded):
            provider.transport.call_client(method, budget_seconds=0.01, max_attempts=1)
        assert calls == []
    finally:
        provider.close()


def test_retry_not_started_when_remaining_budget_is_too_small():
    now = [0.0]
    calls = []

    def monotonic():
        return now[0]

    def method(*, timeout=None):
        calls.append(timeout)
        now[0] += 1.49
        raise BinanceFuturesNetworkError("temporary")

    provider = SnapshotProvider(
        client=FakeClient(),
        monotonic_clock=monotonic,
        sleeper=lambda seconds: None,
        settings=SnapshotProviderSettings(
            rest_timeout_seconds=2.0,
            rest_transport_timeout_seconds=1.5,
            rest_connect_timeout_seconds=0.5,
            rest_read_timeout_seconds=0.75,
            rest_retry_delay_seconds=0.0,
        ),
    )
    try:
        with pytest.raises(SnapshotTransportBudgetExceeded):
            provider.transport.call_client(method)
        assert len(calls) == 1
    finally:
        provider.close()


def test_method_exceeding_declared_transport_budget_is_rejected():
    now = [0.0]

    def monotonic():
        return now[0]

    def method(*, timeout=None):
        now[0] += 2.0
        return {}

    provider = SnapshotProvider(
        client=FakeClient(),
        monotonic_clock=monotonic,
        settings=SnapshotProviderSettings(
            rest_timeout_seconds=2.0,
            rest_transport_timeout_seconds=1.5,
            rest_connect_timeout_seconds=0.5,
            rest_read_timeout_seconds=0.75,
        ),
    )
    try:
        with pytest.raises(SnapshotTransportBudgetExceeded):
            provider.transport.call_client(method)
    finally:
        provider.close()


def test_real_transport_timeout_violation_appears_in_builder_health():
    import time

    class SlowClient(FakeClient):
        def ticker_price(self, symbol, *, timeout=None):
            time.sleep(0.3)
            return {"symbol": symbol, "price": "100"}

    provider = SnapshotProvider(
        client=SlowClient(),
        settings=_tight_settings(),
    )
    try:
        with pytest.raises(Exception):  # noqa: B017
            provider.get_light_snapshot("BTCUSDT")
        health = provider.health_snapshot()
        assert health.source_transport_timeout_violation_count >= 1
        assert health.source_transport_timeout_violations.get(SOURCE_REST_MARKET, 0) >= 1
    finally:
        provider.close()


def test_startup_policy_requires_snapshot_provider_in_live_modes():
    real_policy = resolve_startup_policy(
        execution_mode="REAL_LIVE",
        environment="REAL",
    )
    testnet_policy = resolve_startup_policy(
        execution_mode="TESTNET_LIVE",
        environment="TESTNET",
    )

    assert real_policy.required is True
    assert real_policy.fallback_allowed is False
    assert testnet_policy.required is True
    assert testnet_policy.fallback_allowed is False


def test_startup_policy_paper_requires_explicit_fallback():
    default_policy = resolve_startup_policy(execution_mode="PAPER_LIVE")
    explicit_policy = resolve_startup_policy(
        execution_mode="PAPER_LIVE",
        fallback_allowed=True,
    )

    assert default_policy.required is True
    assert default_policy.fallback_allowed is False
    assert explicit_policy.required is False
    assert explicit_policy.fallback_allowed is True
