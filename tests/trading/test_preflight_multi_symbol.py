import json

from ops.preflight import run_preflight


class FakeClient:
    def ping(self):
        return {}

    def sync_time(self):
        return None

    def account_info(self, *, timeout=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "100", "marginBalance": "100"}]}

    def get_position_mode(self):
        return {"dualSidePosition": False}

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
                for symbol in ("BTCUSDT", "ETHUSDT")
            ]
        }

    def position_risk(self, symbol, *, timeout=None):
        return [{"symbol": symbol, "positionAmt": "0"}]

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []


def _set_base_env(monkeypatch, tmp_path, profiles):
    monkeypatch.chdir(tmp_path)
    profiles_path = tmp_path / ".alphora_symbols.local.json"
    profiles_path.write_text(json.dumps({"symbol_profiles": profiles}), encoding="utf-8")
    portfolio_path = tmp_path / ".alphora_portfolio.local.json"
    portfolio_path.write_text(json.dumps({"max_open_positions": "2"}), encoding="utf-8")
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_MULTI_SYMBOL_ENABLED", "true")
    monkeypatch.setenv("ALPHORA_SYMBOL_PROFILES_PATH", str(profiles_path))
    monkeypatch.setenv("ALPHORA_PORTFOLIO_RISK_PATH", str(portfolio_path))
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    return profiles_path


def test_preflight_multi_symbol_blocks_duplicate_symbol(monkeypatch, tmp_path):
    _set_base_env(
        monkeypatch,
        tmp_path,
        [
            {"symbol": "BTCUSDT", "enabled": True},
            {"symbol": "btcusdt", "enabled": True},
        ],
    )

    result = run_preflight()

    assert result["ok"] is False
    assert any(check["name"] == "symbol_profiles" for check in result["global_checks"])


def test_preflight_multi_symbol_rejects_profile_strategy_fields(monkeypatch, tmp_path):
    _set_base_env(
        monkeypatch,
        tmp_path,
        [
            {"symbol": "BTCUSDT", "enabled": True},
            {
                "symbol": "ETHUSDT",
                "enabled": True,
                "strategy_module": "strategies.futures.missing",
                "strategy_class": "MissingStrategy",
            },
        ],
    )
    monkeypatch.setattr(
        "ops.preflight.make_client", lambda config, mutation_enabled=False: FakeClient()
    )

    result = run_preflight()

    assert result["ok"] is False
    assert result["global_ok"] is False
    assert any(
        check["name"] == "symbol_profiles" and not check["ok"] for check in result["global_checks"]
    )


def test_preflight_multi_symbol_removes_dead_symbol_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "runtime" / "alphora-btcusdt-testnet.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    _set_base_env(
        monkeypatch,
        tmp_path,
        [
            {
                "symbol": "BTCUSDT",
                "enabled": True,
                "lock_path": str(lock_path),
                "position_notional_usdt": "50",
            }
        ],
    )
    monkeypatch.setattr(
        "ops.preflight.make_client", lambda config, mutation_enabled=False: FakeClient()
    )

    result = run_preflight()

    assert result["ok"] is True
    assert result["approved_symbols"] == ["BTCUSDT"]
    assert not lock_path.exists()
    assert any(check["name"] == "process_lock" for check in result["symbol_checks"]["BTCUSDT"])
