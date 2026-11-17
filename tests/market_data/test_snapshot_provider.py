import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from domain.candle import Candle
from domain.models import FuturesPosition, PositionDirection
from domain.strategy_requirements import StrategyDataRequirements
from governor.market_data_hub import MarketDataHub
from governor.user_data_cache import UserDataShadowCache
from market_data.snapshot_engine import (
    ERROR_DIAGNOSTICS_SCHEMA,
    ERROR_DIAGNOSTICS_SCHEMA_VERSION,
    ResourceResolutionError,
    SnapshotBuildError,
    SnapshotRequest,
    SourceFailure,
)
from market_data.snapshot_provider import (
    ACCOUNT,
    EXCHANGE_INFO,
    SnapshotProvider,
    SnapshotProviderSettings,
    exchange_rules,
    mark_price,
    market_state,
    position,
    snapshot_diagnostics_payload,
    snapshot_error_diagnostics_payload,
    snapshot_error_failure_summary,
    snapshot_error_failure_summary_text,
)
from market_data.snapshot_provider.observed_at import normalize_observed_at
from market_data.snapshot_provider.sources import (
    SOURCE_REST_PRIVATE,
    SOURCE_USER_DATA,
    SOURCE_USER_DATA_CACHE,
)


class FakeClient:
    def __init__(self):
        self.ticker_calls = 0
        self.position_risk_calls = 0

    def ticker_price(self, symbol, *, timeout=None):
        self.ticker_calls += 1
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100.5"}

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

    def position_risk(self, symbol, *, timeout=None):
        self.position_risk_calls += 1
        return [{"symbol": symbol, "positionAmt": "0"}]

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "100"}]}


class FakeCandleHub:
    def __init__(self):
        self.requirements = StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 1},
        )
        self.candle = Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=0,
            close_time_ms=59_999,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("10"),
            is_closed=True,
            source="test",
        )

    def candle_snapshot_for_symbol(self, symbol):
        return {"1m": (self.candle,)}

    def diagnostics_for_symbol(self, symbol):
        return {"1m": {"warmup_complete": True}}

    def requirements_for_symbol(self, symbol):
        return self.requirements


def test_snapshot_provider_uses_rest_response_time_instead_of_cycle_reference():
    base = time.time()
    timestamps = iter((base, base + 0.025))
    provider = SnapshotProvider(
        client=FakeClient(),
        wall_clock=lambda: next(timestamps),
    )
    try:
        snapshot = provider.get_light_snapshot("btcusdt", now=base + 0.05)

        assert snapshot.symbol == "BTCUSDT"
        assert snapshot.timestamp == pytest.approx(base + 0.025)
        assert snapshot.data_age_seconds == pytest.approx(0.025, abs=1e-6)
        assert snapshot.market.price == Decimal("100")
        item = provider.last_snapshot[next(iter(provider.last_snapshot.resources))]
        assert item.metadata["request_started_at"] == pytest.approx(base)
        assert item.metadata["response_received_at"] == pytest.approx(base + 0.025)
        assert item.metadata["source_latency_ms"] == pytest.approx(25.0, abs=0.001)
        assert item.metadata["cycle_reference_at"] == pytest.approx(base + 0.05)
        diagnostics = snapshot_diagnostics_payload(provider.last_snapshot)
        assert diagnostics["enabled"] is True
        assert diagnostics["resolved_resources"] == 1
    finally:
        provider.close()


def test_snapshot_provider_injects_candle_snapshot_into_market_state():
    provider = SnapshotProvider(
        client=FakeClient(),
        candle_data_hub=FakeCandleHub(),
    )
    try:
        snapshot = provider.get_light_snapshot("BTCUSDT", now=time.time())

        assert snapshot.market.candles["1m"][0].close == Decimal("100")
        assert snapshot.market.primary_timeframe == "1m"
        assert snapshot.market.candle_diagnostics["1m"]["warmup_complete"] is True
    finally:
        provider.close()


def test_snapshot_provider_cached_position_does_not_pollute_future_rest_snapshot():
    client = FakeClient()
    provider = SnapshotProvider(client=client)
    cached_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    try:
        assisted = provider.get_heavy_snapshot(
            "BTCUSDT",
            now=100.0,
            cached_position=cached_position,
        )
        rest_backed = provider.get_heavy_snapshot("BTCUSDT", now=101.0)

        assert assisted.position == cached_position
        assert rest_backed.position.direction is PositionDirection.NONE
        assert client.position_risk_calls == 1
    finally:
        provider.close()


def test_cycle_session_resolves_global_positions_once_and_pins_them():
    class GlobalClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.position_risk_arguments = []
            self.account_calls = 0

        def position_risk(self, symbol=None, *, timeout=None):
            self.position_risk_arguments.append(symbol)
            if symbol is None:
                return [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.25",
                        "entryPrice": "100",
                        "unRealizedProfit": "1",
                    },
                    {"symbol": "ETHUSDT", "positionAmt": "0"},
                ]
            return super().position_risk(symbol)

        def account_info(self, *, timeout=None):
            self.account_calls += 1
            return super().account_info()

    client = GlobalClient()
    provider = SnapshotProvider(client=client)
    now = time.time()
    cycle = provider.open_cycle_session(
        cycle_id="cycle-1",
        now=now,
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    try:
        baseline = cycle.resolve_baseline()
        baseline_position = baseline.value(position("BTCUSDT"), FuturesPosition)
        assert baseline_position.is_open is True
        assert client.position_risk_arguments == [None]
        assert client.account_calls == 1

        provider.publisher.publish(
            position("BTCUSDT"),
            FuturesPosition(
                symbol="BTCUSDT",
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            ),
            source="late-user-data-event",
            observed_at=now + 0.1,
            force=True,
        )

        selected = cycle.resolve_selected(["BTCUSDT"])
        assert selected.value(position("BTCUSDT"), FuturesPosition) == baseline_position
        assert client.position_risk_arguments == [None]
        assert client.account_calls == 1
        assert selected.snapshot_id == "cycle-1"
    finally:
        cycle.close()
        provider.close()


def test_user_data_cache_position_is_published_and_used_before_global_fallback():
    from governor.user_data_cache import UserDataShadowCache

    class GlobalClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.position_risk_arguments = []

        def position_risk(self, symbol=None, *, timeout=None):
            self.position_risk_arguments.append(symbol)
            if symbol is None:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return super().position_risk(symbol)

    client = GlobalClient()
    cache = UserDataShadowCache()
    provider = SnapshotProvider(
        client=client,
        user_data_cache=cache,
    )
    observed_at = time.time()
    event = {
        "event_type": "ACCOUNT_UPDATE",
        "event_time": observed_at,
        "transaction_time": observed_at,
        "received_at": observed_at,
        "symbol": "BTCUSDT",
        "operational_category": "ACCOUNT",
        "operational_type": "POSITION_UPDATE",
        "severity": "INFO",
        "shadow_only": True,
        "classification": {
            "symbols": ["BTCUSDT"],
            "should_update_order_cache": False,
            "should_update_position_cache": True,
            "should_update_account_cache": False,
        },
        "raw": {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "0.5",
                        "ep": "100",
                        "up": "2",
                    }
                ]
            },
        },
    }
    update = cache.apply_event(event)
    assert update is not None
    provider.publish_user_data_cache_update(cache=cache, update=update, event=event)

    cycle = provider.open_cycle_session(
        cycle_id="cycle-user-data",
        now=observed_at,
        symbols=["BTCUSDT"],
    )
    try:
        snapshot = cycle.resolve_baseline()
        item = snapshot[position("BTCUSDT")]
        assert item.value.is_open is True
        assert item.value.quantity == Decimal("0.5")
        assert item.source == "alphora-user-data-cache"
        assert client.position_risk_arguments == []
    finally:
        cycle.close()
        provider.close()


def test_revalidate_observes_new_user_data_after_local_baseline():
    """Pre-execution revalidation must observe a WebSocket USER_DATA position update.

    In production, USER_DATA events update both user_data_cache AND the Coalestra
    publisher via publish_user_data_cache_update.  Pre-execution revalidation
    invalidates the LOCAL_RECONCILED cycle baseline so the derived/cache source
    can write fresh data.  The user_data_cache (priority=130) provides the LONG
    position, superseding what POSITION_DERIVED would derive from REST.
    """

    class GlobalClient(FakeClient):
        def position_risk(self, symbol=None, *, timeout=None):
            if symbol is None:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return super().position_risk(symbol)

    long_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.4"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    # Simulate USER_DATA stream event: update user_data_cache (production always does this)
    user_data_cache = UserDataShadowCache()
    user_data_cache.reconcile_position_from_rest(
        symbol="BTCUSDT",
        position=long_position,
        received_at=time.time(),
    )

    provider = SnapshotProvider(
        client=GlobalClient(),
        settings=SnapshotProviderSettings(heavy_ttl_seconds=30.0),
        user_data_cache=user_data_cache,
    )
    baseline_at = time.time() - 2.0
    cycle = provider.open_cycle_session(
        cycle_id="cycle-user-data-revalidate",
        now=baseline_at,
        symbols=["BTCUSDT"],
        local_positions={
            "BTCUSDT": FuturesPosition(
                symbol="BTCUSDT",
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
        },
    )
    try:
        cycle.resolve_baseline()

        snapshot = cycle.revalidate_for_execution("BTCUSDT")
        item = snapshot[position("BTCUSDT")]

        # Position comes from SOURCE_USER_DATA_CACHE (reads user_data_cache, priority=130)
        # which supersedes POSITION_DERIVED (from REST flat, priority=120).
        assert item.source == SOURCE_USER_DATA_CACHE
        assert item.value.is_open is True
        assert item.value.quantity == Decimal("0.4")
        # Execution revalidation uses an isolated session; snapshot_id starts with "exec:".
        assert snapshot.snapshot_id.startswith("exec:")
    finally:
        cycle.close()
        provider.close()


def test_rest_private_does_not_downgrade_user_data_even_when_newer():
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(heavy_ttl_seconds=30.0),
    )
    now = time.time() - 20.0
    cycle = provider.open_cycle_session(
        cycle_id="cycle-rest-lower-authority",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    try:
        provider.publisher.publish(
            position("BTCUSDT"),
            FuturesPosition(
                symbol="BTCUSDT",
                direction=PositionDirection.LONG,
                quantity=Decimal("0.5"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
            ),
            source=SOURCE_USER_DATA,
            observed_at=now,
        )
        provider.publisher.publish(
            position("BTCUSDT"),
            FuturesPosition(
                symbol="BTCUSDT",
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            ),
            source=SOURCE_REST_PRIVATE,
            observed_at=now + 10.0,
        )

        try:
            snapshot = cycle.resolve_baseline()
            item = snapshot[position("BTCUSDT")]
            assert item.source == SOURCE_USER_DATA
            assert item.value.quantity == Decimal("0.5")
        finally:
            cycle.close()
    finally:
        provider.close()


def test_newer_local_reconciled_state_still_wins_over_older_user_data():
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(heavy_ttl_seconds=30.0),
    )
    now = time.time() - 2.0
    cycle = provider.open_cycle_session(
        cycle_id="cycle-local-newer",
        now=now + 1.0,
        symbols=["BTCUSDT"],
        local_positions={
            "BTCUSDT": FuturesPosition(
                symbol="BTCUSDT",
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
        },
    )
    try:
        provider.publisher.publish(
            position("BTCUSDT"),
            FuturesPosition(
                symbol="BTCUSDT",
                direction=PositionDirection.LONG,
                quantity=Decimal("0.2"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
            ),
            source=SOURCE_USER_DATA,
            observed_at=now,
        )
        try:
            snapshot = cycle.resolve_baseline()
            item = snapshot[position("BTCUSDT")]
            assert item.value.is_open is False
            assert item.observed_at == pytest.approx(now + 1.0)
        finally:
            cycle.close()
    finally:
        provider.close()


def test_cycle_start_consumes_pending_submission_reconciliation():
    provider = SnapshotProvider(client=FakeClient())
    provider.submissions.mark_reconciliation_needed([position("BTCUSDT")])

    cycle = provider.open_cycle_session(
        cycle_id="cycle-reconcile-start",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    try:
        assert position("BTCUSDT") not in provider.submissions.reconciliation_needed
    finally:
        cycle.close()
        provider.close()


def test_failed_pending_submission_reconciliation_keeps_key(monkeypatch):
    provider = SnapshotProvider(client=FakeClient())
    provider.submissions.mark_reconciliation_needed([position("BTCUSDT")])

    def fail_invalidate(*args, **kwargs):
        raise RuntimeError("synthetic invalidation failure")

    monkeypatch.setattr(provider.publisher, "invalidate_many", fail_invalidate)
    try:
        result = provider.reconcile_pending_submissions(reason="test")

        assert result.success is False
        assert position("BTCUSDT") in provider.submissions.reconciliation_needed
    finally:
        provider.close()


def test_session_pending_submission_reconciliation_revalidates_and_clears_key():
    class GlobalClient(FakeClient):
        def position_risk(self, symbol=None, *, timeout=None):
            if symbol is None:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return super().position_risk(symbol)

    provider = SnapshotProvider(
        client=GlobalClient(),
        settings=SnapshotProviderSettings(heavy_ttl_seconds=30.0),
    )
    cycle = provider.open_cycle_session(
        cycle_id="cycle-reconcile-session",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    try:
        cycle.resolve_selected(["BTCUSDT"])
        provider.submissions.mark_reconciliation_needed([position("BTCUSDT")])

        result = provider.reconcile_pending_submissions(
            session=cycle,
            keys=[position("BTCUSDT")],
            reason="test",
        )

        assert result.success is True
        assert result.revalidated == 1
        assert position("BTCUSDT") not in provider.submissions.reconciliation_needed
    finally:
        cycle.close()
        provider.close()


def test_cycle_selected_snapshot_prefetches_all_heavy_resources_once():
    class CountingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.account_calls = 0
            self.position_calls = []
            self.mark_calls = 0
            self.exchange_calls = 0
            self.open_order_calls = 0
            self.open_algo_calls = 0

        def account_info(self, *, timeout=None):
            self.account_calls += 1
            return super().account_info()

        def position_risk(self, symbol=None, *, timeout=None):
            self.position_calls.append(symbol)
            if symbol is None:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return super().position_risk(symbol)

        def mark_price(self, symbol, *, timeout=None):
            self.mark_calls += 1
            return super().mark_price(symbol)

        def exchange_info(self, *, timeout=None):
            self.exchange_calls += 1
            return super().exchange_info()

        def open_orders(self, symbol, *, timeout=None):
            self.open_order_calls += 1
            return super().open_orders(symbol)

        def open_algo_orders(self, symbol, *, timeout=None):
            self.open_algo_calls += 1
            return super().open_algo_orders(symbol)

    client = CountingClient()
    provider = SnapshotProvider(client=client)
    now = time.time()
    cycle = provider.open_cycle_session(
        cycle_id="cycle-heavy",
        now=now,
        symbols=["BTCUSDT"],
    )
    try:
        cycle.resolve_baseline()
        snapshot = cycle.resolve_selected(["BTCUSDT"])
        heavy = provider.heavy_snapshot_from_snapshot(
            snapshot,
            "BTCUSDT",
            now=now,
        )
        assert heavy.symbol == "BTCUSDT"
        assert client.account_calls == 1
        assert client.position_calls == [None]
        assert client.mark_calls == 1
        assert client.exchange_calls == 1
        assert client.open_order_calls == 1
        assert client.open_algo_calls == 1
    finally:
        cycle.close()
        provider.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (100.0, 100.0),
        (1_700_000_000.0, 1_700_000_000.0),
        (1_700_000_000_000, 1_700_000_000.0),
        (1_700_000_000_000_000, 1_700_000_000.0),
        (1_700_000_000_000_000_000, 1_700_000_000.0),
    ],
)
def test_observation_timestamp_normalizes_units_without_freshening_old_values(raw, expected):
    assert normalize_observed_at(
        domain_observed_at=raw,
        reference_time=2_000_000_000.0,
    ) == pytest.approx(expected)


def test_rest_payload_records_real_completion_time_even_with_synthetic_cycle_time():
    timestamps = iter((1_000.0, 1_000.125))
    provider = SnapshotProvider(
        client=FakeClient(),
        wall_clock=lambda: next(timestamps),
    )
    context = SimpleNamespace(requested_at=500.0, metadata={"alphora_now": 100.0})
    try:
        payload = provider.fetchers.rest_private(ACCOUNT, context)

        assert payload.observed_at == pytest.approx(1_000.125)
        assert payload.metadata["alphora_observed_at"] == pytest.approx(1_000.125)
        assert payload.metadata["request_started_at"] == pytest.approx(1_000.0)
        assert payload.metadata["response_received_at"] == pytest.approx(1_000.125)
        assert payload.metadata["source_latency_ms"] == pytest.approx(125.0)
        assert payload.metadata["cycle_reference_at"] == pytest.approx(100.0)
    finally:
        provider.close()


def test_mark_price_uses_response_time_even_when_exchange_time_is_available():
    exchange_observed_at = time.time() - 0.25

    class TimestampedClient(FakeClient):
        def mark_price(self, symbol, *, timeout=None):
            return {
                "symbol": symbol,
                "markPrice": "100.5",
                "time": int(exchange_observed_at * 1000),
            }

    now = time.time()
    timestamps = iter((now, now + 0.01))
    provider = SnapshotProvider(
        client=TimestampedClient(),
        wall_clock=lambda: next(timestamps),
    )
    context = SimpleNamespace(requested_at=now, metadata={"alphora_now": now})
    try:
        payload = provider.fetchers.rest_market(mark_price("BTCUSDT"), context)

        assert payload.observed_at == pytest.approx(now + 0.01)
        assert payload.metadata["response_received_at"] == pytest.approx(now + 0.01)
        assert payload.value == Decimal("100.5")
    finally:
        provider.close()


def test_exchange_rules_inherit_dependency_timestamp_and_refresh_after_dependency_update():
    client = FakeClient()
    provider = SnapshotProvider(client=client)
    now = time.time()
    first_cycle = provider.open_cycle_session(
        cycle_id="rules-cycle-1",
        now=now,
        symbols=["BTCUSDT"],
    )
    try:
        first_cycle.resolve_selected(["BTCUSDT"])
        first = first_cycle.session.resolve_request(SnapshotRequest(optional=[EXCHANGE_INFO]))
        first_rule_item = first[exchange_rules("BTCUSDT")]
        first_dependency = first[EXCHANGE_INFO]
        assert first_rule_item.observed_at == first_dependency.observed_at
        assert first_rule_item.metadata["dependency_observed_at"] == first_dependency.observed_at
        assert first_rule_item.metadata["dependency_fetched_at"] == first_dependency.fetched_at
    finally:
        first_cycle.close()

    updated_at = time.time()
    updated_exchange_info = client.exchange_info()
    updated_exchange_info["symbols"][0]["filters"][1]["tickSize"] = "0.25"
    provider.publisher.publish(
        EXCHANGE_INFO,
        updated_exchange_info,
        source="exchange-info-reconciliation",
        observed_at=updated_at,
        force=True,
    )

    second_cycle = provider.open_cycle_session(
        cycle_id="rules-cycle-2",
        now=updated_at,
        symbols=["BTCUSDT"],
    )
    try:
        second_cycle.resolve_selected(["BTCUSDT"])
        second = second_cycle.session.resolve_request(SnapshotRequest(optional=[EXCHANGE_INFO]))
        rule_item = second[exchange_rules("BTCUSDT")]
        dependency = second[EXCHANGE_INFO]

        assert rule_item.value.price_tick == Decimal("0.25")
        assert rule_item.observed_at == pytest.approx(updated_at)
        assert rule_item.observed_at == dependency.observed_at
        assert rule_item.metadata["dependency_source"] == "exchange-info-reconciliation"
    finally:
        second_cycle.close()
        provider.close()


def test_provider_reads_atomic_hub_window_without_per_tick_publication():
    client = FakeClient()
    hub = MarketDataHub(
        symbols=["BTCUSDT"],
        base_url="wss://stream.binancefuture.com",
        price_window=4,
        market_shock_detector_enabled=False,
    )
    provider = SnapshotProvider(
        client=client,
        market_data_hub=hub,
        settings=SnapshotProviderSettings(light_ttl_seconds=0.0, price_window=4),
    )
    try:
        base_time = time.time()
        for index, price in enumerate(("100", "101", "102"), start=1):
            observed_at = base_time + index * 0.01
            payload = {
                "s": "BTCUSDT",
                "b": price,
                "a": price,
                "E": int(observed_at * 1000),
            }
            assert hub.ingest_message(json.dumps(payload), received_at=observed_at) is True

        now = base_time + 0.03
        first = provider.get_light_snapshot("BTCUSDT", now=now)
        second = provider.get_light_snapshot("BTCUSDT", now=now)

        assert first.market.prices == [Decimal("100"), Decimal("101"), Decimal("102")]
        assert second.market.prices == first.market.prices
        assert tuple(hub.prices_by_symbol["BTCUSDT"]) == tuple(first.market.prices)
        assert client.ticker_calls == 0
        assert hub.price_snapshot_callback is None
    finally:
        provider.close()


def test_provider_forwards_transport_timeout_to_timeout_aware_client():
    class TimeoutAwareClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.received_timeout = None

        def ticker_price(self, symbol, *, timeout=None):
            self.ticker_calls += 1
            self.received_timeout = timeout
            return {"symbol": symbol, "price": "100"}

    client = TimeoutAwareClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            rest_timeout_seconds=2.0,
            rest_transport_timeout_seconds=1.9,
            rest_connect_timeout_seconds=0.4,
            rest_read_timeout_seconds=1.4,
        ),
    )
    try:
        provider.get_light_snapshot("BTCUSDT", now=100.0)
        assert client.received_timeout == (0.4, 1.4)
    finally:
        provider.close()


def test_transport_timeout_budget_cannot_exceed_engine_source_timeout():
    import pytest

    with pytest.raises(ValueError, match="rest_transport_timeout_seconds"):
        SnapshotProviderSettings(
            rest_timeout_seconds=1.0,
            rest_connect_timeout_seconds=0.5,
            rest_read_timeout_seconds=0.75,
        )


def test_exchange_info_uses_dedicated_transport_timeout():
    class TimeoutAwareExchangeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.received_timeout = None

        def exchange_info(self, *, timeout=None):
            self.received_timeout = timeout
            return super().exchange_info()

    client = TimeoutAwareExchangeClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            rest_timeout_seconds=2.0,
            rest_transport_timeout_seconds=1.9,
            rest_connect_timeout_seconds=0.5,
            rest_read_timeout_seconds=1.25,
            exchange_info_timeout_seconds=8.0,
            exchange_info_transport_timeout_seconds=7.8,
            exchange_info_connect_timeout_seconds=1.5,
            exchange_info_read_timeout_seconds=6.0,
            deadline_seconds=9.0,
            cycle_deadline_seconds=10.0,
        ),
    )

    try:
        exchange_info = provider.get_exchange_info(now=time.time())

        assert exchange_info["symbols"]
        assert client.received_timeout == (1.5, 6.0)
    finally:
        provider.close()


def test_exchange_info_can_outlive_general_rest_timeout():
    class SlowExchangeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.received_timeout = None

        def exchange_info(self, *, timeout=None):
            self.received_timeout = timeout
            time.sleep(0.12)
            return super().exchange_info()

    client = SlowExchangeClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            deadline_seconds=0.8,
            cycle_deadline_seconds=1.0,
            rest_timeout_seconds=0.2,
            rest_transport_timeout_seconds=0.12,
            rest_min_attempt_timeout_seconds=0.05,
            rest_connect_timeout_seconds=0.01,
            rest_read_timeout_seconds=0.04,
            exchange_info_timeout_seconds=0.7,
            exchange_info_transport_timeout_seconds=0.5,
            exchange_info_connect_timeout_seconds=0.05,
            exchange_info_read_timeout_seconds=0.35,
        ),
    )

    try:
        exchange_info = provider.get_exchange_info(now=time.time())

        assert exchange_info["symbols"]
        assert client.received_timeout == (0.05, 0.35)
    finally:
        provider.close()


def test_exchange_info_transport_budget_cannot_exceed_source_timeout():
    with pytest.raises(
        ValueError,
        match="exchange_info_transport_timeout_seconds",
    ):
        SnapshotProviderSettings(
            exchange_info_timeout_seconds=0.30,
            exchange_info_connect_timeout_seconds=0.20,
            exchange_info_read_timeout_seconds=0.20,
        )


def test_exchange_info_source_timeout_must_fit_inside_cycle():
    with pytest.raises(
        ValueError,
        match="exchange_info_timeout_seconds",
    ):
        SnapshotProviderSettings(
            exchange_info_timeout_seconds=10.0,
            cycle_deadline_seconds=10.0,
        )


def test_snapshot_error_diagnostics_preserves_nested_failures():
    resource_error = ResourceResolutionError(
        EXCHANGE_INFO,
        (
            SourceFailure(
                source="alphora-rest-exchange",
                error_type="SourceTimeoutError",
                message=("source alphora-rest-exchange timed out after 9.500s"),
                attempts=2,
            ),
        ),
    )

    error = SnapshotBuildError(
        {
            EXCHANGE_INFO: resource_error,
        }
    )

    payload = snapshot_error_diagnostics_payload(error)

    assert payload["error_type"] == "SnapshotBuildError"

    failure = payload["details"]["errors"][0]["failures"][0]

    # v0.5.5+: SourceFailure.to_dict() now includes schema/schema_version fields
    assert failure["schema"] == ERROR_DIAGNOSTICS_SCHEMA
    assert failure["schema_version"] == ERROR_DIAGNOSTICS_SCHEMA_VERSION
    assert failure["source"] == "alphora-rest-exchange"
    assert failure["error_type"] == "SourceTimeoutError"
    assert failure["message"] == "source alphora-rest-exchange timed out after 9.500s"
    assert failure["attempts"] == 2


def test_snapshot_error_failure_summary_compacts_nested_failures():
    resource_error = ResourceResolutionError(
        EXCHANGE_INFO,
        (
            SourceFailure(
                source="alphora-rest-private",
                error_type="SnapshotTransportBudgetExceeded",
                message="retry delay would exceed the declared transport budget",
                attempts=1,
            ),
        ),
    )
    error = SnapshotBuildError({EXCHANGE_INFO: resource_error})

    summary = snapshot_error_failure_summary(error)
    text = snapshot_error_failure_summary_text(error)

    assert summary == [
        {
            "resource": str(EXCHANGE_INFO),
            "source": "alphora-rest-private",
            "error_type": "SnapshotTransportBudgetExceeded",
            "message": "retry delay would exceed the declared transport budget",
            "attempts": 1,
        }
    ]
    assert "exchange:info <- alphora-rest-private: SnapshotTransportBudgetExceeded" in text


def test_exchange_info_bootstrap_retries_and_warms_cache():
    class FlakyExchangeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.exchange_info_calls = 0

        def exchange_info(self, *, timeout=None):
            self.exchange_info_calls += 1

            if self.exchange_info_calls <= 2:
                raise OSError("synthetic temporary connection failure")

            return super().exchange_info()

    client = FlakyExchangeClient()

    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            exchange_info_bootstrap_attempts=3,
            exchange_info_bootstrap_base_delay_seconds=0.0,
            exchange_info_bootstrap_max_delay_seconds=0.0,
        ),
        sleeper=lambda _seconds: None,
    )

    try:
        result = provider.bootstrap_exchange_info()

        assert result.attempts == 3
        assert result.symbol_count == 1
        assert client.exchange_info_calls == 3

        provider.get_exchange_info()

        assert client.exchange_info_calls == 3
    finally:
        provider.close()


def test_exchange_info_bootstrap_rejects_empty_symbol_list():
    class EmptyThenValidExchangeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.exchange_info_calls = 0

        def exchange_info(self, *, timeout=None):
            self.exchange_info_calls += 1

            if self.exchange_info_calls == 1:
                return {"symbols": []}

            return super().exchange_info()

    client = EmptyThenValidExchangeClient()

    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            exchange_info_bootstrap_attempts=2,
            exchange_info_bootstrap_base_delay_seconds=0.0,
            exchange_info_bootstrap_max_delay_seconds=0.0,
        ),
        sleeper=lambda _seconds: None,
    )

    try:
        result = provider.bootstrap_exchange_info()

        assert result.attempts == 2
        assert result.symbol_count == 1
        assert client.exchange_info_calls == 2
    finally:
        provider.close()


def test_ticker_price_uses_response_time_when_exchange_timestamp_lags():
    exchange_observed_at = time.time() - 45.0

    class TimestampedTickerClient(FakeClient):
        def ticker_price(self, symbol, *, timeout=None):
            self.ticker_calls += 1
            return {
                "symbol": symbol,
                "price": "100",
                "time": int(exchange_observed_at * 1000),
            }

    now = time.time()
    timestamps = iter((now, now + 0.02))
    provider = SnapshotProvider(
        client=TimestampedTickerClient(),
        wall_clock=lambda: next(timestamps),
    )
    context = SimpleNamespace(requested_at=now, metadata={"alphora_now": now})
    try:
        payload = provider.fetchers.rest_market(market_state("BTCUSDT"), context)

        assert payload.observed_at == pytest.approx(now + 0.02)
        assert payload.metadata["response_received_at"] == pytest.approx(now + 0.02)
        assert payload.value.price == Decimal("100")
        assert payload.observed_at > exchange_observed_at + 40.0
    finally:
        provider.close()


def test_light_selected_prefetch_does_not_call_heavy_or_private_endpoints():
    class CountingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.account_calls = 0
            self.mark_calls = 0
            self.exchange_calls = 0
            self.open_order_calls = 0
            self.open_algo_calls = 0

        def account_info(self, *, timeout=None):
            self.account_calls += 1
            return super().account_info(timeout=timeout)

        def mark_price(self, symbol, *, timeout=None):
            self.mark_calls += 1
            return super().mark_price(symbol, timeout=timeout)

        def exchange_info(self, *, timeout=None):
            self.exchange_calls += 1
            return super().exchange_info(timeout=timeout)

        def open_orders(self, symbol, *, timeout=None):
            self.open_order_calls += 1
            return super().open_orders(symbol, timeout=timeout)

        def open_algo_orders(self, symbol, *, timeout=None):
            self.open_algo_calls += 1
            return super().open_algo_orders(symbol, timeout=timeout)

    client = CountingClient()
    provider = SnapshotProvider(client=client)
    cycle = provider.open_cycle_session(
        cycle_id="cycle-light-selected",
        now=time.time(),
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    try:
        snapshot = cycle.resolve_light_selected(["BTCUSDT", "ETHUSDT"])

        assert market_state("BTCUSDT") in snapshot.resources
        assert market_state("ETHUSDT") in snapshot.resources
        assert client.ticker_calls == 2
        assert client.account_calls == 0
        assert client.position_risk_calls == 0
        assert client.mark_calls == 0
        assert client.exchange_calls == 0
        assert client.open_order_calls == 0
        assert client.open_algo_calls == 0
    finally:
        cycle.close()
        provider.close()


def test_symbol_evaluation_resolves_heavy_data_only_when_symbol_reaches_heavy_stage():
    class CountingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.account_calls = 0
            self.mark_symbols = []
            self.open_order_symbols = []
            self.open_algo_symbols = []
            self.position_risk_arguments = []

        def account_info(self, *, timeout=None):
            self.account_calls += 1
            return super().account_info(timeout=timeout)

        def mark_price(self, symbol, *, timeout=None):
            self.mark_symbols.append(symbol)
            return super().mark_price(symbol, timeout=timeout)

        def position_risk(self, symbol=None, *, timeout=None):
            self.position_risk_arguments.append(symbol)
            if symbol is None:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return super().position_risk(symbol, timeout=timeout)

        def open_orders(self, symbol, *, timeout=None):
            self.open_order_symbols.append(symbol)
            return super().open_orders(symbol, timeout=timeout)

        def open_algo_orders(self, symbol, *, timeout=None):
            self.open_algo_symbols.append(symbol)
            return super().open_algo_orders(symbol, timeout=timeout)

    client = CountingClient()
    provider = SnapshotProvider(client=client)
    cycle = provider.open_cycle_session(
        cycle_id="cycle-jit-heavy",
        now=time.time(),
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    try:
        cycle.resolve_light_selected(["BTCUSDT", "ETHUSDT"])
        assert client.mark_symbols == []
        assert client.account_calls == 0

        snapshot = cycle.resolve_symbol_evaluation("BTCUSDT")

        assert mark_price("BTCUSDT") in snapshot.resources
        assert position("BTCUSDT") in snapshot.resources
        assert client.mark_symbols == ["BTCUSDT"]
        assert client.open_order_symbols == ["BTCUSDT"]
        assert client.open_algo_symbols == ["BTCUSDT"]
        assert client.position_risk_arguments == [None]
        assert client.account_calls == 1
    finally:
        cycle.close()
        provider.close()


def test_disabled_user_data_cache_is_not_queried_or_counted_as_a_source():
    class CacheTrapProvider(SnapshotProvider):
        def _fetch_user_data_cache_batch(self, keys, context):
            raise AssertionError("disabled user-data cache source must not be called")

    provider = CacheTrapProvider(
        client=FakeClient(),
        user_data_cache=UserDataShadowCache(),
    )
    cycle = provider.open_cycle_session(
        cycle_id="cycle-cache-disabled",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    try:
        snapshot = cycle.resolve_baseline()

        assert ACCOUNT in snapshot.resources
        assert position("BTCUSDT") in snapshot.resources
        assert all(
            failure.source != SOURCE_USER_DATA_CACHE
            for error in snapshot.errors.values()
            for failure in getattr(error, "failures", ())
        )
    finally:
        cycle.close()
        provider.close()
