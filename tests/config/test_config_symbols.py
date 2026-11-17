import pytest

from config import load_config, parse_symbols
from config.snapshot_env import LEGACY_ENV_PREFIX


def test_parse_symbols_uses_single_symbol_fallback_when_empty():
    assert parse_symbols("", fallback="btcusdt") == ("BTCUSDT",)


def test_parse_symbols_parses_comma_separated_values():
    assert parse_symbols("btcusdt, ethusdt, BTCUSDT, SOLUSDT", fallback="BNBUSDT") == (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    )


def test_load_config_limits_active_symbols(monkeypatch):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT")
    monkeypatch.setenv("ALPHORA_MAX_ACTIVE_SYMBOLS", "2")

    config = load_config()

    assert config.symbols == ("BTCUSDT", "ETHUSDT")
    assert config.symbol == "BTCUSDT"
    assert config.market_ws_enabled is True
    assert config.market_ws_base_url == "wss://fstream.binancefuture.com"
    assert config.trade_plan_shadow is True
    assert config.trade_planning_enabled is False
    assert config.economic_gate_enabled is True
    assert config.min_net_gain_bps == 5.0
    assert config.min_net_reward_risk == 1.5
    assert config.max_round_trip_cost_bps == 20.0
    assert config.require_profitable_trade_plan is True
    assert config.economic_exit_adjustment_enabled is True
    assert config.max_economic_take_bps == 120.0
    assert config.max_economic_take_atr_multiplier == 3.0
    assert config.economic_exit_diagnostics_enabled is True
    assert config.entry_execution_style == "MARKET_SAFE"
    assert config.limit_entry_enabled is False
    assert config.smart_sizing_settings.min_notional_policy.value == "BLOCK"
    assert config.smart_execution_settings.max_spread_bps == 5
    assert config.market_entry_require_economic_gate is True


def test_load_config_parses_smart_sizing_and_execution_settings(monkeypatch):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_SMART_SIZING_MIN_NOTIONAL_POLICY", "FLOOR_TO_MIN")
    monkeypatch.setenv("ALPHORA_SMART_SIZING_LOW_REWARD_RISK", "0.9")
    monkeypatch.setenv("ALPHORA_SMART_EXECUTION_MAX_SPREAD_BPS", "35")
    monkeypatch.setenv("ALPHORA_SMART_EXECUTION_MAX_PRICE_DRIFT_BPS", "7")
    monkeypatch.setenv("ALPHORA_SMART_EXECUTION_MAX_MARKET_DATA_AGE_SECONDS", "5")

    config = load_config()

    assert config.smart_sizing_settings.min_notional_policy.value == "FLOOR_TO_MIN"
    assert str(config.smart_sizing_settings.low_reward_risk) == "0.9"
    assert config.smart_execution_settings.max_spread_bps == 35
    assert config.smart_execution_settings.max_price_drift_bps == 7
    assert config.smart_execution_settings.max_market_data_age_seconds == 5


def _base_env(monkeypatch):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")


def test_load_config_parses_snapshot_settings(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_SNAPSHOT_DEADLINE_SECONDS", "4.5")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_TIMEOUT_SECONDS", "3.8")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_CONNECT_TIMEOUT_SECONDS", "0.6")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_READ_TIMEOUT_SECONDS", "2.4")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_TRANSPORT_TIMEOUT_SECONDS", "3.1")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_RETRY_DELAY_SECONDS", "0.02")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_EXECUTION_MAX_AGE_SECONDS", "2.7")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_EXECUTION_MAX_SKEW_SECONDS", "2.8")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_EVALUATION_MAX_AGE_SECONDS", "30")

    config = load_config()

    assert config.snapshot_deadline_seconds == 4.5
    assert config.snapshot_rest_timeout_seconds == 3.8
    assert config.snapshot_rest_connect_timeout_seconds == 0.6
    assert config.snapshot_rest_read_timeout_seconds == 2.4
    assert config.snapshot_rest_transport_timeout_seconds == 3.1
    assert config.snapshot_rest_max_attempts == 3
    assert config.snapshot_rest_retry_delay_seconds == 0.02
    assert config.snapshot_execution_max_age_seconds == 2.7
    assert config.snapshot_execution_max_skew_seconds == 2.8
    assert config.snapshot_evaluation_max_age_seconds == 30.0
    assert config.snapshot_fallback_allowed is None


def test_load_config_still_reads_snapshot_settings_under_their_legacy_names(monkeypatch):
    """ADR-002: .env files written before M5 keep working."""
    _base_env(monkeypatch)
    monkeypatch.setenv(f"{LEGACY_ENV_PREFIX}DEADLINE_SECONDS", "4.5")
    monkeypatch.setenv(f"{LEGACY_ENV_PREFIX}REST_MAX_ATTEMPTS", "2")

    config = load_config()

    assert config.snapshot_deadline_seconds == 4.5
    assert config.snapshot_rest_max_attempts == 2


def test_new_snapshot_setting_name_wins_over_the_legacy_one(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv(f"{LEGACY_ENV_PREFIX}DEADLINE_SECONDS", "4.5")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_DEADLINE_SECONDS", "5.5")

    assert load_config().snapshot_deadline_seconds == 5.5


def test_invalid_legacy_snapshot_setting_is_reported_under_the_name_that_was_set(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv(f"{LEGACY_ENV_PREFIX}DEADLINE_SECONDS", "abc")

    with pytest.raises(ValueError, match=f"{LEGACY_ENV_PREFIX}DEADLINE_SECONDS"):
        load_config()


def test_load_config_rejects_invalid_snapshot_rest_budget(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_TIMEOUT_SECONDS", "2.0")
    monkeypatch.setenv("ALPHORA_SNAPSHOT_REST_TRANSPORT_TIMEOUT_SECONDS", "2.0")

    with pytest.raises(ValueError, match="ALPHORA_SNAPSHOT_REST_TRANSPORT_TIMEOUT_SECONDS"):
        load_config()


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_load_config_reads_snapshot_fallback_flag(monkeypatch, raw, expected):
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_SNAPSHOT_FALLBACK_ALLOWED", raw)

    assert load_config().snapshot_fallback_allowed is expected


def test_candles_come_from_the_production_endpoint_by_default_even_on_testnet(monkeypatch):
    """ADR-004: the endpoint is explicit configuration, production by default."""
    _base_env(monkeypatch)
    monkeypatch.delenv("ALPHORA_CANDLE_KLINES_URL", raising=False)

    assert load_config().candle_klines_url == "https://fapi.binance.com/fapi/v1/klines"


def test_candle_endpoint_can_point_to_the_testnet(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv(
        "ALPHORA_CANDLE_KLINES_URL", "https://testnet.binancefuture.com/fapi/v1/klines"
    )

    assert load_config().candle_klines_url == "https://testnet.binancefuture.com/fapi/v1/klines"


@pytest.mark.parametrize(
    "url",
    [
        "http://fapi.binance.com/fapi/v1/klines",
        "https://fapi.binance.com/fapi/v1/ticker/price",
        "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT",
        "fapi.binance.com/fapi/v1/klines",
    ],
)
def test_load_config_rejects_a_candle_endpoint_that_is_not_https_klines(monkeypatch, url):
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_CANDLE_KLINES_URL", url)

    with pytest.raises(ValueError, match="ALPHORA_CANDLE_KLINES_URL"):
        load_config()


def test_load_config_accepts_trade_cost_aliases_from_gui(monkeypatch):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_SPREAD_BPS", "1.5")
    monkeypatch.setenv("ALPHORA_SLIPPAGE_BPS", "2.5")
    monkeypatch.setenv("ALPHORA_DEFAULT_FUNDING_RATE", "-0.0001")

    config = load_config()

    assert config.estimated_spread_bps == 1.5
    assert config.estimated_slippage_bps == 2.5
    assert config.estimated_funding_rate == -0.0001


def test_load_config_prefers_new_trade_cost_names_over_legacy_aliases(monkeypatch):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_SPREAD_BPS", "1")
    monkeypatch.setenv("ALPHORA_ESTIMATED_SPREAD_BPS", "4")
    monkeypatch.setenv("ALPHORA_SLIPPAGE_BPS", "2")
    monkeypatch.setenv("ALPHORA_ESTIMATED_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("ALPHORA_DEFAULT_FUNDING_RATE", "0.0001")
    monkeypatch.setenv("ALPHORA_ESTIMATED_FUNDING_RATE", "0.0003")

    config = load_config()

    assert config.estimated_spread_bps == 4
    assert config.estimated_slippage_bps == 5
    assert config.estimated_funding_rate == 0.0003


def test_load_config_rejects_unsupported_market_ws_stream(monkeypatch):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_MARKET_WS_STREAM", "aggTrade")

    with pytest.raises(ValueError, match="ALPHORA_MARKET_WS_STREAM"):
        load_config()


@pytest.mark.parametrize(
    "symbols,max_active_symbols",
    [
        ("", "0"),
        ("", "-1"),
        ("BTCUSDT,ETHUSDT,SOLUSDT", "0"),
        ("BTCUSDT,ETHUSDT,SOLUSDT", "-3"),
    ],
)
def test_load_config_rejects_non_positive_max_active_symbols_by_name(
    monkeypatch,
    symbols,
    max_active_symbols,
):
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_SYMBOLS", symbols)
    monkeypatch.setenv("ALPHORA_MAX_ACTIVE_SYMBOLS", max_active_symbols)

    with pytest.raises(ValueError, match="ALPHORA_MAX_ACTIVE_SYMBOLS precisa ser no minimo 1"):
        load_config()


def test_rest_weight_guard_observes_by_default(monkeypatch):
    _base_env(monkeypatch)

    config = load_config()

    assert config.rest_weight_guard_mode == "OBSERVE"
    assert config.rest_weight_guard_ratio == 0.8
    assert config.rest_weight_limit_1m == 2400


def test_load_config_reads_the_rest_weight_guard(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_REST_WEIGHT_GUARD_MODE", "enforce")
    monkeypatch.setenv("ALPHORA_REST_WEIGHT_GUARD_RATIO", "0.7")
    monkeypatch.setenv("ALPHORA_REST_WEIGHT_LIMIT_1M", "1200")

    config = load_config()

    assert (
        config.rest_weight_guard_mode,
        config.rest_weight_guard_ratio,
        config.rest_weight_limit_1m,
    ) == ("ENFORCE", 0.7, 1200)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALPHORA_REST_WEIGHT_GUARD_MODE", "BLOCK"),
        ("ALPHORA_REST_WEIGHT_GUARD_RATIO", "0"),
        ("ALPHORA_REST_WEIGHT_GUARD_RATIO", "1.5"),
        ("ALPHORA_REST_WEIGHT_LIMIT_1M", "0"),
    ],
)
def test_load_config_rejects_an_invalid_rest_weight_guard(monkeypatch, name, value):
    _base_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        load_config()
