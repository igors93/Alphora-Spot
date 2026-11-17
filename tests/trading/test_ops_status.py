from dataclasses import replace

from config.symbol_context import symbol_config_from_app_config
from ops.status import get_binance_status


class FakeStatusClient:
    def ping(self):
        return {}

    def server_time(self):
        return {"serverTime": 1}

    def sync_time(self):
        return None

    def account_info(self, *, timeout=None):
        return {
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                    "availableBalance": "90",
                    "marginBalance": "100",
                    "unrealizedProfit": "0",
                }
            ]
        }

    def get_position_mode(self):
        return {"dualSidePosition": False}

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
                for symbol in ("BTCUSDT", "ETHUSDT")
            ]
        }

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100"}

    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}

    def position_risk(self, symbol, *, timeout=None):
        return [{"symbol": symbol, "positionAmt": "0"}]

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []


def test_status_simple_reports_current_symbol(config_factory, monkeypatch):
    import ops.status as status_module

    config = config_factory(symbols=("BTCUSDT",))
    monkeypatch.setattr(status_module, "load_config", lambda: config)
    monkeypatch.setattr(
        status_module, "make_client", lambda config, mutation_enabled=False: FakeStatusClient()
    )

    status = get_binance_status()

    assert status["operation_mode"] == "GOVERNOR_SINGLE_SYMBOL"
    assert list(status["symbols"]) == ["BTCUSDT"]
    assert status["connection_ok"] is True


def test_status_multi_reports_enabled_profiles(config_factory, monkeypatch):
    import ops.status as status_module

    config = replace(config_factory(symbols=("BTCUSDT",)), multi_symbol_enabled=True)
    profiles = [
        symbol_config_from_app_config(config, symbol="BTCUSDT"),
        symbol_config_from_app_config(config, symbol="ETHUSDT"),
    ]
    monkeypatch.setattr(status_module, "load_config", lambda: config)
    monkeypatch.setattr(
        status_module, "make_client", lambda config, mutation_enabled=False: FakeStatusClient()
    )
    monkeypatch.setattr(
        status_module, "resolve_governor_symbol_configs", lambda *args, **kwargs: profiles
    )

    status = get_binance_status()

    assert status["operation_mode"] == "GOVERNOR_MULTI_SYMBOL"
    assert list(status["symbols"]) == ["BTCUSDT", "ETHUSDT"]


def test_status_governor_single_reports_one_enabled_profile(config_factory, monkeypatch):
    import ops.status as status_module

    config = replace(config_factory(symbols=("BTCUSDT",)), multi_symbol_enabled=True)
    profiles = [symbol_config_from_app_config(config, symbol="BTCUSDT")]
    monkeypatch.setattr(status_module, "load_config", lambda: config)
    monkeypatch.setattr(
        status_module, "make_client", lambda config, mutation_enabled=False: FakeStatusClient()
    )
    monkeypatch.setattr(
        status_module, "resolve_governor_symbol_configs", lambda *args, **kwargs: profiles
    )

    status = get_binance_status()

    assert status["operation_mode"] == "GOVERNOR_SINGLE_SYMBOL"
    assert list(status["symbols"]) == ["BTCUSDT"]
