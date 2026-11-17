"""Every REST caller honours the shared count: snapshot transport, candles, heavy-check budget."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from binance.futures_client import BinanceFuturesAPIError
from binance.rest_weight import RestWeightLedger, current_rest_source
from domain.models import FuturesPosition, PositionDirection
from governor.cycle.telemetry import CycleMetricsState, CycleTelemetry
from governor.cycle_context import CycleContext
from governor.request_budget import RequestBudget
from governor.services import RequestBudgetService
from market_data.candle_provider import CandleDataProvider, CandleProviderError
from market_data.snapshot_provider.settings import SnapshotProviderSettings
from market_data.snapshot_provider.transport import RestTransport
from scanner.signal_score import SignalPriority

HOST = "fapi.binance.com"


class _Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


# --- snapshot transport -----------------------------------------------------------------------


def _transport() -> RestTransport:
    clock = _Clock()
    return RestTransport(
        client=None,
        settings=SnapshotProviderSettings(rest_retry_delay_seconds=0.0),
        monotonic=clock,
        sleep=lambda _seconds: None,
    )


@pytest.mark.parametrize("status", [418, 429])
def test_snapshot_transport_does_not_repeat_a_rate_limited_read(status: int) -> None:
    calls: list[str] = []

    def operation(_timeout: Any) -> Any:
        calls.append(current_rest_source())
        raise BinanceFuturesAPIError(status, -1003, "Too many requests")

    with pytest.raises(BinanceFuturesAPIError):
        _transport().call_with_budget(
            operation,
            source_name="alphora-rest-private",
            budget_seconds=5.0,
            connect_timeout_seconds=1.0,
            read_timeout_seconds=2.0,
            max_attempts=4,
        )

    assert calls == ["snapshot:alphora-rest-private"]


def test_snapshot_transport_still_repeats_a_server_error() -> None:
    attempts = 0

    def operation(_timeout: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BinanceFuturesAPIError(503, -1, "Service unavailable")
        return "ok"

    result = _transport().call_with_budget(
        operation,
        source_name="alphora-rest-market",
        budget_seconds=5.0,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        max_attempts=4,
    )

    assert (result, attempts) == ("ok", 2)


# --- candles ----------------------------------------------------------------------------------


def _response(status: int, payload: Any = None, headers: dict[str, str] | None = None) -> Any:
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = requests.structures.CaseInsensitiveDict(headers or {})
    response.text = ""
    response.json.return_value = payload if payload is not None else []
    return response


def test_candles_record_into_the_shared_ledger_and_honour_its_backoff() -> None:
    clock = _Clock()
    ledger = RestWeightLedger(monotonic=clock)
    provider = CandleDataProvider(rest_weight=ledger)

    with (
        patch(
            "market_data.candle_provider.requests.get",
            return_value=_response(418, headers={"Retry-After": "120"}),
        ) as get,
        patch("time.sleep"),
    ):
        with pytest.raises(CandleProviderError):
            provider._fetch_batch(symbol="BTCUSDT", interval="1m", limit=2)
        assert get.call_count == 1  # a ban is never retried

        with pytest.raises(CandleProviderError, match="back-off"):
            provider._fetch_batch(symbol="BTCUSDT", interval="1m", limit=2)
        assert get.call_count == 1  # nothing sent during the ban

    assert ledger.snapshot()["hosts"][HOST]["by_source"] == {"candles": 1}


# --- heavy-check budget -----------------------------------------------------------------------


class _Journal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


def _pressured_ledger(used_weight: int) -> RestWeightLedger:
    ledger = RestWeightLedger(monotonic=_Clock(), weight_limit_1m=2400)
    ledger.record(
        host=HOST,
        path="/fapi/v2/account",
        status_code=200,
        headers={"X-MBX-USED-WEIGHT-1M": str(used_weight)},
    )
    return ledger


def _budget_service(mode: str, ledger: RestWeightLedger) -> RequestBudgetService:
    config = SimpleNamespace(
        governor_request_budget_enabled=True,
        governor_request_budget_max_heavy_checks_per_cycle=5,
        governor_request_budget_open_position_override=True,
        rest_weight_guard_mode=mode,
        rest_weight_guard_ratio=0.8,
    )
    return RequestBudgetService(
        config=config,
        journal=_Journal(),
        strategy=SimpleNamespace(global_strategy_name="test"),
        rest_weight=ledger,
    )


def _position(*, open_position: bool) -> FuturesPosition:
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG if open_position else PositionDirection.NONE,
        quantity=Decimal("0.01") if open_position else Decimal("0"),
        entry_price=Decimal("100") if open_position else Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _consume(service: RequestBudgetService, *, open_position: bool = False) -> Any:
    context = CycleContext(now=0.0, request_budget=service.build_budget())
    return service.consume_heavy_check(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        score=SimpleNamespace(score=80, priority=SignalPriority.HOT, action="OPEN_LONG"),
        position=_position(open_position=open_position),
        context=context,
    )


@pytest.fixture
def audits(capture_audit_events) -> list[tuple[str, dict[str, Any]]]:
    return capture_audit_events("governor.services.request_budget")


def test_observe_mode_reports_pressure_without_postponing(audits: list[Any]) -> None:
    decision = _consume(_budget_service("OBSERVE", _pressured_ledger(2100)))

    assert decision.allowed is True
    assert [event for event, _ in audits] == ["rest_weight_pressure"]
    assert audits[0][1]["heavy_check_postponed"] is False


def test_enforce_mode_postpones_optional_heavy_checks(audits: list[Any]) -> None:
    service = _budget_service("ENFORCE", _pressured_ledger(2100))

    decision = _consume(service)

    assert decision.allowed is False
    assert decision.reason == "REST_WEIGHT_PRESSURE"
    assert service.journal.events[0][0] == "heavy_check_skipped_by_request_budget"


def test_enforce_mode_never_postpones_an_open_position(audits: list[Any]) -> None:
    decision = _consume(_budget_service("ENFORCE", _pressured_ledger(2400)), open_position=True)

    assert decision.allowed is True
    assert audits == []


@pytest.mark.parametrize(("mode", "used_weight"), [("OFF", 2400), ("ENFORCE", 1000)])
def test_no_pressure_or_guard_off_leaves_the_budget_alone(
    audits: list[Any], mode: str, used_weight: int
) -> None:
    decision = _consume(_budget_service(mode, _pressured_ledger(used_weight)))

    assert decision.allowed is True
    assert audits == []


def test_request_budget_deny_counts_the_denial() -> None:
    budget = RequestBudget(max_heavy_checks_per_cycle=2)

    decision = budget.deny(
        reason="REST_WEIGHT_PRESSURE",
        symbol="btcusdt",
        priority="hot",
        action="open_long",
        position_open=False,
        fast_lane=False,
        category="hot",
    )

    assert (decision.allowed, decision.symbol, decision.category) == (False, "BTCUSDT", "HOT")
    assert budget.summary()["denied_heavy_checks"] == 1


# --- cycle metrics ----------------------------------------------------------------------------


def test_cycle_metrics_report_rest_traffic_since_the_previous_event() -> None:
    ledger = _pressured_ledger(600)
    telemetry = CycleTelemetry(
        config=SimpleNamespace(),
        journal=_Journal(),
        logger=MagicMock(),
        market_data_hub=None,
        state=CycleMetricsState(),
        rest_weight=ledger,
    )

    first = telemetry.rest_weight_payload()
    ledger.record(host=HOST, path="/fapi/v1/ticker/price", status_code=200, headers=None)
    ledger.record(host=HOST, path="/fapi/v1/ticker/price", status_code=429, headers={})
    second = telemetry.rest_weight_payload()

    assert first["since_last_metrics"]["requests"] == 1
    assert second["since_last_metrics"] == {
        "requests": 2,
        "rate_limited": 1,
        "banned": 0,
        "refused_during_backoff": 0,
    }
    assert second["pressure"] == 1.0
    assert second["hosts"][HOST]["requests"] == 3
