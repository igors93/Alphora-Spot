"""Research downloads share the same REST ledger and ban as live market data."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from binance.rest_weight import RestWeightLedger
from desktop.services import backtest_service
from research import binance_funding_downloader, binance_klines_downloader


@pytest.mark.parametrize(
    ("module", "source", "path", "exception"),
    [
        (
            binance_klines_downloader,
            "research:klines",
            "/fapi/v1/klines",
            binance_klines_downloader.BinanceKlinesDownloadError,
        ),
        (
            binance_funding_downloader,
            "research:funding",
            "/fapi/v1/fundingRate",
            binance_funding_downloader.BinanceFundingDownloadError,
        ),
    ],
)
def test_research_downloads_count_responses_and_obey_shared_backoff(
    monkeypatch: pytest.MonkeyPatch, module, source, path, exception
) -> None:
    ledger = RestWeightLedger()
    monkeypatch.setattr(module, "shared_rest_weight_ledger", lambda: ledger)

    response = MagicMock(spec=requests.Response)
    response.status_code = 418
    response.headers = {"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "12"}
    response.text = "IP banned"

    def fetch() -> None:
        if module is binance_klines_downloader:
            module._fetch_klines_batch(
                symbol="BTCUSDT", interval="1m", start_time_ms=1, end_time_ms=2
            )
        else:
            module._fetch_funding_batch(symbol="BTCUSDT", start_time_ms=1, end_time_ms=2)

    with patch("requests.get", return_value=response) as get:
        with pytest.raises(exception):
            fetch()
        with pytest.raises(exception, match="back-off"):
            fetch()
        assert get.call_count == 1

    host = ledger.snapshot()["hosts"]["fapi.binance.com"]
    assert host["requests"] == 1
    assert host["by_source"] == {source: 1}
    assert host["top_paths"] == {path: 1}
    assert host["used_weight_1m"] == 12
    assert host["refused_during_backoff"] == 1


def test_desktop_rules_request_uses_the_same_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = RestWeightLedger()
    monkeypatch.setattr(backtest_service, "shared_rest_weight_ledger", lambda: ledger)
    response = MagicMock(spec=requests.Response)
    response.status_code = 429
    response.headers = {"Retry-After": "20"}
    response.raise_for_status.side_effect = requests.HTTPError("429")

    with patch("requests.get", return_value=response) as get:
        with pytest.raises(requests.HTTPError):
            backtest_service.fetch_symbol_rules({"symbol": "BTCUSDT", "environment": "REAL"})
        with pytest.raises(RuntimeError, match="back-off"):
            backtest_service.fetch_symbol_rules({"symbol": "BTCUSDT", "environment": "REAL"})
        assert get.call_count == 1

    host = ledger.snapshot()["hosts"]["fapi.binance.com"]
    assert host["by_source"] == {"desktop:backtest_rules": 1}
    assert host["top_paths"] == {"/fapi/v1/exchangeInfo": 1}
