from pathlib import Path

import pytest

from research.data_loader import load_candles_from_csv


def test_data_loader_loads_valid_csv_and_sorts(tmp_path: Path):
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2024-01-01T00:01:00Z,101,102,100,101,10\n"
        "2024-01-01T00:00:00Z,100,101,99,100,9\n",
        encoding="utf-8",
    )

    candles = load_candles_from_csv(str(csv_path))

    assert [candle.timestamp for candle in candles] == [
        "2024-01-01T00:00:00Z",
        "2024-01-01T00:01:00Z",
    ]
    assert candles[0].close == 100


def test_data_loader_rejects_missing_columns(tmp_path: Path):
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text("timestamp,open,high,low,close\n1,1,1,1,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="colunas obrigatorias"):
        load_candles_from_csv(str(csv_path))
