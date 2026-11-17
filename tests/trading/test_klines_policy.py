"""Regression tests for centralized klines HTTP policy.

Verifies that:
- build_binance_public_read_policy retries 5xx / transport errors.
- 4xx permanent errors are NOT retried.
- Exhaustion by result raises RetryExhaustedError from the factory.
- Both klines consumers (candle_provider, binance_klines_downloader) use the
  centralized factory and convert exhaustion to their own domain errors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from binance.retry_policies import (
    BINANCE_PUBLIC_READ_RETRY_STATUSES,
    build_binance_public_read_policy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status_code: int, json_data=None, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = dict(headers or {})
    resp.text = ""
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


def _fresh_policy(name: str = "test_policy"):
    """Policy without budget — each test runs in isolation without budget side-effects."""
    return build_binance_public_read_policy(policy_name=name)


# ---------------------------------------------------------------------------
# Retry statuses constant
# ---------------------------------------------------------------------------


def test_retry_statuses_are_complete():
    assert BINANCE_PUBLIC_READ_RETRY_STATUSES == frozenset({429, 500, 502, 503, 504})


def test_factory_does_not_retry_an_ip_ban():
    """418 is an IP ban: a repeated request extends it, so the response comes back at once."""
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        return _resp(418, headers={"Retry-After": "120"})

    with patch("time.sleep") as sleep:
        response = _fresh_policy("test_418").run(_fn)

    assert response.status_code == 418
    assert call_count == 1
    sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Factory: retry behavior
# ---------------------------------------------------------------------------


def test_factory_retries_500_and_succeeds_on_second_attempt():
    call_count = 0
    good_data: list = []

    def _fn():
        nonlocal call_count
        call_count += 1
        return _resp(500) if call_count == 1 else _resp(200, good_data)

    with patch("time.sleep"):
        response = _fresh_policy("test_500").run(_fn)

    assert response.status_code == 200
    assert call_count == 2


def test_factory_retries_503():
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        return _resp(503) if call_count == 1 else _resp(200, [])

    with patch("time.sleep"):
        response = _fresh_policy("test_503").run(_fn)

    assert response.status_code == 200
    assert call_count == 2


def test_factory_retries_429():
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        return _resp(429) if call_count == 1 else _resp(200, [])

    with patch("time.sleep"):
        response = _fresh_policy("test_429").run(_fn)

    assert response.status_code == 200
    assert call_count == 2


def test_factory_respects_retry_after_header():
    """When 429 carries Retry-After, the policy sleeps for at least that value."""
    sleep_calls: list[float] = []
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _resp(429, headers={"Retry-After": "2"})
        return _resp(200, [])

    with patch("time.sleep", side_effect=sleep_calls.append):
        _fresh_policy("test_ra").run(_fn)

    assert call_count == 2
    assert any(t >= 2.0 for t in sleep_calls), f"Expected >=2s sleep, got: {sleep_calls}"


def test_factory_does_not_retry_400():
    """400 is not in retry statuses; the response is returned after a single attempt."""
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        return _resp(400)

    response = _fresh_policy("test_400").run(_fn)

    assert response.status_code == 400
    assert call_count == 1


def test_factory_retries_transport_exception():
    """requests.RequestException is a transport exception and is retried."""
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise requests.RequestException("connection refused")
        return _resp(200, [])

    with patch("time.sleep"):
        response = _fresh_policy("test_transport").run(_fn)

    assert response.status_code == 200
    assert call_count == 3


def test_factory_raises_retry_exhausted_when_5xx_exhausted():
    """All 3 attempts return 5xx; RetryExhaustedError is raised (raise_on_result_exhausted)."""
    from resilience.retry_engine import RetryExhaustedError

    def _fn():
        return _resp(500)

    with patch("time.sleep"):
        with pytest.raises(RetryExhaustedError):
            _fresh_policy("test_exhaust_5xx").run(_fn)


# ---------------------------------------------------------------------------
# Consumer: centralized policy names (no HTTP)
# ---------------------------------------------------------------------------


def test_candle_provider_policy_carries_correct_name():
    from market_data.candle_provider import _KLINES_POLICY

    explanation = _KLINES_POLICY.explain()
    assert "candle_provider_fetch" in explanation


def test_klines_downloader_policy_carries_correct_name():
    from research.binance_klines_downloader import _KLINES_BATCH_POLICY

    explanation = _KLINES_BATCH_POLICY.explain()
    assert "klines_batch_fetch" in explanation


# ---------------------------------------------------------------------------
# Consumer: domain error conversion
# ---------------------------------------------------------------------------


def test_candle_provider_400_not_retried_and_raises_domain_error():
    """CandleProviderError is raised for 400; exactly 1 HTTP call (no retry)."""
    from market_data.candle_provider import CandleDataProvider, CandleProviderError

    call_count = 0

    def _fake(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        return _resp(400)

    provider = CandleDataProvider(base_url="https://test.example.com/klines")
    with patch("requests.get", side_effect=_fake):
        with pytest.raises(CandleProviderError):
            provider._fetch_batch(symbol="BTCUSDT", interval="1m", limit=1)

    assert call_count == 1


def test_candle_provider_converts_retry_exhausted_to_domain_error():
    """RetryExhaustedError from the policy is wrapped as CandleProviderError."""
    from market_data.candle_provider import CandleDataProvider, CandleProviderError
    from resilience.retry_engine import RetryExhaustedError

    provider = CandleDataProvider(base_url="https://test.example.com/klines")
    with patch("market_data.candle_provider._KLINES_POLICY") as mock_policy:
        mock_policy.run.side_effect = RetryExhaustedError("all retries consumed")
        with pytest.raises(CandleProviderError):
            provider._fetch_batch(symbol="BTCUSDT", interval="1m", limit=1)


def test_candle_provider_invalid_json_raises_domain_error():
    """A 200 with invalid JSON still raises CandleProviderError (not ValueError)."""
    from market_data.candle_provider import CandleDataProvider, CandleProviderError

    bad_resp = MagicMock(spec=requests.Response)
    bad_resp.status_code = 200
    bad_resp.headers = {}
    bad_resp.text = "not-json"
    bad_resp.json.side_effect = ValueError("bad json")

    provider = CandleDataProvider(base_url="https://test.example.com/klines")
    with patch("requests.get", return_value=bad_resp):
        with pytest.raises(CandleProviderError):
            provider._fetch_batch(symbol="BTCUSDT", interval="1m", limit=1)


def test_klines_downloader_400_not_retried_and_raises_domain_error():
    """BinanceKlinesDownloadError is raised for 400; exactly 1 HTTP call."""
    from research.binance_klines_downloader import (
        BinanceKlinesDownloadError,
        _fetch_klines_batch,
    )

    call_count = 0

    def _fake(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        return _resp(400)

    with patch("requests.get", side_effect=_fake):
        with pytest.raises(BinanceKlinesDownloadError):
            _fetch_klines_batch(
                symbol="BTCUSDT",
                interval="1m",
                start_time_ms=1_000_000,
                end_time_ms=2_000_000,
            )

    assert call_count == 1


def test_klines_downloader_converts_retry_exhausted_to_domain_error():
    """RetryExhaustedError from the batch policy is wrapped as BinanceKlinesDownloadError."""
    from research.binance_klines_downloader import (
        BinanceKlinesDownloadError,
        _fetch_klines_batch,
    )
    from resilience.retry_engine import RetryExhaustedError

    with patch("research.binance_klines_downloader._KLINES_BATCH_POLICY") as mock_policy:
        mock_policy.run.side_effect = RetryExhaustedError("all retries consumed")
        with pytest.raises(BinanceKlinesDownloadError):
            _fetch_klines_batch(
                symbol="BTCUSDT",
                interval="1m",
                start_time_ms=1_000_000,
                end_time_ms=2_000_000,
            )
