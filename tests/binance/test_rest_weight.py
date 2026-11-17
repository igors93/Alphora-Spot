"""The shared Binance REST weight count and the 429/418 back-off (OPT-036, ADR-005)."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any

import pytest
import requests

from binance.futures_client import (
    BinanceFuturesAPIError,
    BinanceFuturesClient,
    BinanceFuturesRateLimited,
)
from binance.rest_weight import (
    DEFAULT_BACKOFF_SECONDS,
    TOO_MANY_REQUESTS_CODE,
    RestWeightLedger,
    parse_retry_after,
    rest_source,
)

HOST = "fapi.binance.com"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def ledger(clock: _Clock) -> RestWeightLedger:
    return RestWeightLedger(monotonic=clock, weight_limit_1m=2400)


# --- Retry-After ------------------------------------------------------------------------------


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    assert parse_retry_after({"Retry-After": "7"}) == 7.0
    moment = datetime(2030, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
    reference = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    assert parse_retry_after(
        {"Retry-After": format_datetime(moment, usegmt=True)}, now=reference
    ) == pytest.approx(30.0)


@pytest.mark.parametrize("headers", [None, {}, {"Retry-After": "soon"}, {"Retry-After": "-5"}])
def test_missing_or_invalid_retry_after_is_none(headers: Any) -> None:
    assert parse_retry_after(headers) is None


# --- ledger -----------------------------------------------------------------------------------


def test_ledger_counts_requests_and_reads_the_used_weight_header(ledger: RestWeightLedger) -> None:
    with rest_source("snapshot:alphora-rest-private"):
        ledger.record(
            host=HOST,
            path="/fapi/v2/account",
            status_code=200,
            headers={"X-MBX-USED-WEIGHT-1M": "1200"},
        )
    ledger.record(
        host=HOST, path="/fapi/v1/klines", status_code=200, headers=None, source="candles"
    )

    snapshot = ledger.snapshot()["hosts"][HOST]
    assert snapshot["requests"] == 2
    assert snapshot["by_source"] == {"snapshot:alphora-rest-private": 1, "candles": 1}
    assert snapshot["used_weight_1m"] == 1200
    assert ledger.pressure() == pytest.approx(0.5)
    assert ledger.totals()["requests"] == 2


def test_a_used_weight_reading_expires_after_a_minute(
    ledger: RestWeightLedger, clock: _Clock
) -> None:
    ledger.record(host=HOST, path="/x", status_code=200, headers={"x-mbx-used-weight-1m": "2000"})
    clock.now += 61.0

    assert ledger.pressure() is None
    assert ledger.snapshot()["hosts"][HOST]["used_weight_1m"] is None


def test_429_starts_a_backoff_for_its_retry_after(ledger: RestWeightLedger, clock: _Clock) -> None:
    ledger.record(host=HOST, path="/x", status_code=429, headers={"Retry-After": "5"})

    backoff = ledger.active_backoff(HOST)
    assert backoff is not None
    assert backoff.status_code == 429
    assert backoff.remaining_seconds == pytest.approx(5.0)
    assert ledger.pressure() == 1.0
    assert ledger.active_backoff("testnet.binancefuture.com") is None

    clock.now += 5.0
    assert ledger.active_backoff(HOST) is None


@pytest.mark.parametrize("status", [429, 418])
def test_backoff_without_retry_after_uses_the_conservative_default(
    ledger: RestWeightLedger, status: int
) -> None:
    ledger.record(host=HOST, path="/x", status_code=status, headers={})

    backoff = ledger.active_backoff(HOST)
    assert backoff is not None
    assert backoff.remaining_seconds == pytest.approx(DEFAULT_BACKOFF_SECONDS[status])


def test_a_shorter_later_retry_after_never_shortens_a_ban(ledger: RestWeightLedger) -> None:
    ledger.record(host=HOST, path="/x", status_code=418, headers={"Retry-After": "300"})
    ledger.record(host=HOST, path="/x", status_code=429, headers={"Retry-After": "2"})

    backoff = ledger.active_backoff(HOST)
    assert backoff is not None
    assert backoff.status_code == 418
    assert backoff.remaining_seconds == pytest.approx(300.0)
    totals = ledger.totals()
    assert (totals["banned"], totals["rate_limited"], totals["refused_during_backoff"]) == (1, 1, 1)


# --- client -----------------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, payload: Any, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.text = "x"

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def request(self, **_kwargs: Any) -> _Response:
        self.calls += 1
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def _client(ledger: RestWeightLedger, session: _Session) -> BinanceFuturesClient:
    client = BinanceFuturesClient(
        base_url=f"https://{HOST}", api_key="k", api_secret="s", rest_weight=ledger
    )
    client.session = session  # type: ignore[assignment]
    return client


def test_client_refuses_to_send_while_the_host_backs_off(
    ledger: RestWeightLedger, clock: _Clock
) -> None:
    session = _Session(
        [
            _Response(429, {"code": -1003, "msg": "Too many requests"}, {"Retry-After": "3"}),
            _Response(200, {"serverTime": 1}, {"X-MBX-USED-WEIGHT-1M": "10"}),
        ]
    )
    client = _client(ledger, session)

    with pytest.raises(BinanceFuturesAPIError) as first:
        client.server_time()
    assert first.value.status_code == 429
    assert first.value.retry_after_seconds == 3.0

    with pytest.raises(BinanceFuturesRateLimited) as refused:
        client.server_time()
    assert session.calls == 1
    assert refused.value.code == TOO_MANY_REQUESTS_CODE
    assert refused.value.status_code == 429
    assert refused.value.retry_after_seconds == pytest.approx(3.0)

    clock.now += 3.0
    assert client.server_time() == {"serverTime": 1}
    assert session.calls == 2
    assert ledger.snapshot()["hosts"][HOST]["used_weight_1m"] == 10


def test_a_ban_blocks_orders_too(ledger: RestWeightLedger) -> None:
    ledger.record(host=HOST, path="/fapi/v1/klines", status_code=418, headers={"Retry-After": "60"})
    session = _Session([])
    client = _client(ledger, session)

    with pytest.raises(BinanceFuturesRateLimited) as refused:
        client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity="0.001")

    assert refused.value.status_code == 418
    assert session.calls == 0


def test_a_new_rate_limit_after_an_expired_ban_is_reported_as_429(
    ledger: RestWeightLedger, clock: _Clock
) -> None:
    ledger.record(host=HOST, path="/x", status_code=418, headers={"Retry-After": "10"})
    clock.now += 11.0
    ledger.record(host=HOST, path="/x", status_code=429, headers={"Retry-After": "2"})

    backoff = ledger.active_backoff(HOST)
    assert backoff is not None
    assert backoff.status_code == 429
    assert backoff.remaining_seconds == pytest.approx(2.0)
