from decimal import Decimal

import pytest

from backtesting.infrastructure.funding_loader import load_funding_rates_from_csv
from research.binance_funding_downloader import (
    BinanceFundingDownloadError,
    write_funding_csv,
)


def test_write_funding_csv_uses_backtest_loader_contract(tmp_path):
    path = tmp_path / "BTCUSDT_funding.csv"
    write_funding_csv(
        [
            {
                "fundingTime": 1767225600000,
                "fundingRate": "0.0001",
            },
            {
                "fundingTime": 1767254400000,
                "fundingRate": "-0.0002",
            },
        ],
        path,
    )

    points = load_funding_rates_from_csv(path)

    assert path.read_text(encoding="utf-8").splitlines()[0] == "timestamp,rate"
    assert [point.rate for point in points] == [Decimal("0.0001"), Decimal("-0.0002")]


def test_write_funding_csv_rejects_empty_or_invalid_payload(tmp_path):
    with pytest.raises(BinanceFundingDownloadError, match="Nenhum funding"):
        write_funding_csv([], tmp_path / "empty.csv")

    with pytest.raises(BinanceFundingDownloadError, match="invalido"):
        write_funding_csv(
            [{"fundingTime": "bad", "fundingRate": "oops"}],
            tmp_path / "invalid.csv",
        )
