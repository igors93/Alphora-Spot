import math
import os
from pathlib import Path
from urllib.parse import urlparse

from config.enums import (
    ArchitectureMode,
    ExecutionMode,
    IntelligentActivationLevel,
)
from config.env_readers import (
    _validate_finite_number,
    _validate_non_negative_number,
    _validate_positive_number,
    _validate_probability,
)
from config.models import AppConfig, TradingSymbolConfig
from config.regime_policy_modes import normalize_regime_policy_mode
from config.rest_weight_env import validate_rest_weight_settings
from config.snapshot_env import snapshot_env_name, validate_candle_klines_url
from config.state_paths import (
    default_journal_path as environment_journal_path,
)
from config.state_paths import (
    default_state_path as environment_state_path,
)
from trading_cadence import (
    MAX_DAILY_TRADES_CEILING,
    MIN_LOOP_INTERVAL_SECONDS,
    MIN_SECONDS_BETWEEN_ENTRIES_FLOOR,
)


def _validate_config(config: AppConfig) -> None:
    _validate_connection(config)
    _validate_symbols_and_sizing(config)
    _validate_scheduling(config)
    _validate_order_policy(config)
    _validate_paths(config)
    _validate_learning_modes(config)
    _validate_costs_and_dispatch(config)
    _validate_ai_probabilistic_config(config)
    _validate_snapshot_config(config)


def _validate_connection(config: AppConfig) -> None:
    normalize_regime_policy_mode(config.trend_context_mode)
    if not config.base_url.startswith("https://"):
        raise ValueError("BINANCE_FUTURES_BASE_URL precisa comecar com https://")

    host = urlparse(config.base_url).netloc.lower()
    if os.getenv("ALPHORA_MARKET_TYPE", "").strip().upper() == "SPOT" and (
        "fapi" in host or "binancefuture" in host
    ):
        raise ValueError("ALPHORA_MARKET_TYPE=SPOT exige URL da API Spot")

    if not config.api_key:
        raise ValueError(_missing_credential_message("BINANCE_FUTURES_API_KEY", config))

    if not config.api_secret:
        raise ValueError(_missing_credential_message("BINANCE_FUTURES_API_SECRET", config))

    if config.market_ws_enabled:
        if not config.market_ws_base_url.startswith("wss://"):
            raise ValueError("ALPHORA_MARKET_WS_BASE_URL precisa comecar com wss://")

    if config.market_ws_stream != "bookTicker":
        raise ValueError("ALPHORA_MARKET_WS_STREAM aceita inicialmente apenas bookTicker")

    _validate_positive_number(
        "ALPHORA_MARKET_WS_HEARTBEAT_SECONDS",
        config.market_ws_heartbeat_seconds,
    )
    _validate_positive_number(
        "ALPHORA_MARKET_WS_MAX_PRICE_AGE_SECONDS",
        config.market_ws_max_price_age_seconds,
    )
    _validate_positive_number(
        "ALPHORA_MARKET_WS_RECONNECT_SECONDS",
        config.market_ws_reconnect_seconds,
    )

    if config.market_ws_warmup_seconds < 0:
        raise ValueError("ALPHORA_MARKET_WS_WARMUP_SECONDS precisa ser maior ou igual a zero")

    if config.market_ws_dirty_symbols_limit < 1:
        raise ValueError("ALPHORA_MARKET_WS_DIRTY_SYMBOLS_LIMIT precisa ser no minimo 1")

    if config.market_ws_price_window < 1:
        raise ValueError("ALPHORA_MARKET_WS_PRICE_WINDOW precisa ser no minimo 1")

    if config.market_ws_max_symbols_per_drain < 1:
        raise ValueError("ALPHORA_MARKET_WS_MAX_SYMBOLS_PER_DRAIN precisa ser no minimo 1")


def _validate_symbols_and_sizing(config: AppConfig) -> None:
    if not config.symbol:
        raise ValueError("ALPHORA_SYMBOL nao pode ficar vazio")

    if not config.symbols:
        raise ValueError("ALPHORA_SYMBOLS nao pode ficar vazio quando configurado")

    _validate_max_active_symbols(config.max_active_symbols)

    if config.leverage < 1:
        raise ValueError("ALPHORA_LEVERAGE precisa ser no minimo 1")

    if config.leverage > 5:
        raise ValueError("Por seguranca, esta versao limita a alavancagem a no maximo 5x")

    if urlparse(config.base_url).netloc.lower() in {
        "api.binance.com", "testnet.binance.vision"
    }:
        if config.leverage != 1:
            raise ValueError("Spot exige ALPHORA_LEVERAGE=1")
        if (
            config.strategy_module != "strategies.futures.fixed.spot_capital_rotation"
            or config.strategy_class != "SpotCapitalRotationStrategy"
        ):
            raise ValueError("Estrategia Futures nao e compativel com Spot")

    _validate_positive_number(
        "ALPHORA_POSITION_NOTIONAL_USDT",
        config.position_notional_usdt,
    )
    _validate_positive_number("ALPHORA_STOP_LOSS_PCT", config.stop_loss_pct)
    _validate_positive_number("ALPHORA_TAKE_PROFIT_PCT", config.take_profit_pct)
    _validate_positive_number(
        "ALPHORA_LOOP_INTERVAL_SECONDS",
        config.loop_interval_seconds,
    )
    _validate_trading_cadence(config)
    _validate_risk_bounds(config)


def _validate_scheduling(config: AppConfig) -> None:
    if config.governor_max_concurrent_symbol_snapshots < 1:
        raise ValueError("ALPHORA_GOVERNOR_MAX_CONCURRENT_SYMBOL_SNAPSHOTS precisa ser no minimo 1")
    _validate_positive_number(
        "ALPHORA_GOVERNOR_PREFETCHED_SNAPSHOT_MAX_AGE_SECONDS",
        config.governor_prefetched_snapshot_max_age_seconds,
    )
    _validate_non_negative_number(
        "ALPHORA_GOVERNOR_SHARED_PRIVATE_CYCLE_TTL_SECONDS",
        config.governor_shared_private_cycle_ttl_seconds,
    )
    _validate_positive_number(
        "ALPHORA_INITIAL_BACKOFF_SECONDS",
        config.initial_backoff_seconds,
    )
    _validate_positive_number(
        "ALPHORA_MAX_BACKOFF_SECONDS",
        config.max_backoff_seconds,
    )

    if config.max_backoff_seconds < config.initial_backoff_seconds:
        raise ValueError(
            "ALPHORA_MAX_BACKOFF_SECONDS precisa ser maior ou igual ao backoff inicial"
        )

    if config.max_consecutive_errors < 1:
        raise ValueError("ALPHORA_MAX_CONSECUTIVE_ERRORS precisa ser no minimo 1")


def _validate_order_policy(config: AppConfig) -> None:
    allowed_entry_styles = {
        "MARKET_SAFE",
        "LIMIT_MAKER_FIRST",
        "LIMIT_WITH_MARKET_FALLBACK",
        "MARKET_ONLY",
    }
    if config.entry_execution_style not in allowed_entry_styles:
        raise ValueError(
            "ALPHORA_ENTRY_EXECUTION_STYLE invalido: "
            f"{config.entry_execution_style}. "
            f"Valores aceitos: {', '.join(sorted(allowed_entry_styles))}."
        )

    if config.execution_mode is ExecutionMode.REAL_LIVE and not config.allow_real_trading:
        raise ValueError("ALPHORA_EXECUTION_MODE=REAL_LIVE exige ALPHORA_ALLOW_REAL_TRADING=true.")
    _validate_architecture_config(config)

    real_exchange_host = urlparse(config.base_url).netloc.lower() in {
        "fapi.binance.com",
        "api.binance.com",
    }
    if (
        config.execution_mode is ExecutionMode.TESTNET_LIVE
        and config.environment.value == "TESTNET"
        and real_exchange_host
        and os.getenv("ALPHORA_ALLOW_TESTNET_REAL_URL", "").strip().lower()
        not in {"true", "1", "yes", "y", "sim"}
    ):
        raise ValueError(
            "TESTNET_LIVE esta apontando para URL real da Binance. "
            "Use a URL testnet/demo ou defina ALPHORA_ALLOW_TESTNET_REAL_URL=true "
            "somente se souber exatamente o que esta fazendo."
        )

    if not config.bot_order_prefix:
        raise ValueError("ALPHORA_BOT_ORDER_PREFIX nao pode ficar vazio")

    if len(config.bot_order_prefix) > 12:
        raise ValueError("ALPHORA_BOT_ORDER_PREFIX deve ter no maximo 12 caracteres")

    allowed_prefix_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")

    if any(char not in allowed_prefix_chars for char in config.bot_order_prefix):
        raise ValueError("ALPHORA_BOT_ORDER_PREFIX so pode conter letras, numeros e underscore")


def _validate_paths(config: AppConfig) -> None:
    if not config.trade_journal_path:
        raise ValueError("ALPHORA_TRADE_JOURNAL_PATH nao pode ficar vazio")

    if not config.trade_audit_path:
        raise ValueError("ALPHORA_TRADE_AUDIT_PATH nao pode ficar vazio")

    if not config.runtime_dir:
        raise ValueError("ALPHORA_RUNTIME_DIR nao pode ficar vazio")

    if not config.state_path:
        raise ValueError("ALPHORA_STATE_PATH nao pode ficar vazio")

    if config.atr_period < 1:
        raise ValueError("ALPHORA_ATR_PERIOD precisa ser no minimo 1")

    if config.multi_symbol_enabled and config.execution_mode is ExecutionMode.REAL_LIVE:
        if not config.allow_real_trading:
            raise ValueError(
                "ALPHORA_MULTI_SYMBOL_ENABLED=true em REAL_LIVE exige "
                "ALPHORA_ALLOW_REAL_TRADING=true."
            )


def _validate_learning_modes(config: AppConfig) -> None:
    learning_paper_flags_enabled = (
        config.learning_paper_activation_enabled
        or config.learning_paper_reduction_enabled
        or config.learning_paper_block_enabled
    )

    if learning_paper_flags_enabled and not config.learning_store_enabled:
        raise ValueError(
            "Modo de learning em paper exige ALPHORA_LEARNING_ADVISORY_MODE "
            "REDUCE_PRIORITY ou PAPER_BLOCK."
        )

    if learning_paper_flags_enabled and config.execution_mode is not ExecutionMode.PAPER_LIVE:
        raise ValueError(
            "Modo de learning em paper so pode ser usado com ALPHORA_EXECUTION_MODE=PAPER_LIVE."
        )

    if (
        config.learning_paper_reduction_enabled or config.learning_paper_block_enabled
    ) and not config.learning_paper_activation_enabled:
        raise ValueError(
            "ALPHORA_LEARNING_ADVISORY_MODE=REDUCE_PRIORITY ou PAPER_BLOCK "
            "exige ativacao paper derivada do modo."
        )

    if config.learning_testnet_observation_enabled and not config.learning_store_enabled:
        raise ValueError("ALPHORA_LEARNING_ADVISORY_MODE=OBSERVE exige learning store ativo.")

    if (
        config.learning_testnet_observation_enabled
        and config.execution_mode is not ExecutionMode.TESTNET_LIVE
    ):
        raise ValueError(
            "ALPHORA_LEARNING_ADVISORY_MODE=OBSERVE so pode ser usado "
            "com ALPHORA_EXECUTION_MODE=TESTNET_LIVE."
        )


def _validate_costs_and_dispatch(config: AppConfig) -> None:
    _validate_non_negative_number("ALPHORA_MAKER_FEE_PCT", config.maker_fee_pct)
    _validate_non_negative_number("ALPHORA_TAKER_FEE_PCT", config.taker_fee_pct)
    _validate_non_negative_number(
        "ALPHORA_ESTIMATED_SPREAD_BPS/ALPHORA_SPREAD_BPS",
        config.estimated_spread_bps,
    )
    _validate_non_negative_number(
        "ALPHORA_ESTIMATED_SLIPPAGE_BPS/ALPHORA_SLIPPAGE_BPS",
        config.estimated_slippage_bps,
    )
    _validate_non_negative_number(
        "ALPHORA_PRE_DISPATCH_SOFT_PRICE_DRIFT_BPS",
        config.pre_dispatch_soft_price_drift_bps,
    )
    _validate_non_negative_number(
        "ALPHORA_PRE_DISPATCH_HARD_PRICE_DRIFT_BPS",
        config.pre_dispatch_hard_price_drift_bps,
    )
    if config.pre_dispatch_hard_price_drift_bps < config.pre_dispatch_soft_price_drift_bps:
        raise ValueError(
            "ALPHORA_PRE_DISPATCH_HARD_PRICE_DRIFT_BPS precisa ser maior ou igual ao limite soft."
        )
    _validate_non_negative_number(
        "ALPHORA_POST_FILL_MAX_RISK_INCREASE_PCT",
        config.post_fill_max_risk_increase_pct,
    )
    _validate_non_negative_number(
        "ALPHORA_PROTECTION_TRIGGER_BUFFER_BPS",
        config.protection_trigger_buffer_bps,
    )
    _validate_finite_number(
        "ALPHORA_ESTIMATED_FUNDING_RATE/ALPHORA_DEFAULT_FUNDING_RATE",
        config.estimated_funding_rate,
    )


def _missing_credential_message(name: str, config: AppConfig) -> str:
    if config.execution_mode.is_live:
        return f"{name} nao foi configurada no .env"
    # DRY_RUN and PAPER_LIVE never send orders, but they still read the account,
    # positions and open orders through signed Binance endpoints.
    return (
        f"{name} nao foi configurada no .env. "
        f"{config.execution_mode.value} nao envia ordens, mas le conta, posicoes e "
        "ordens abertas na Binance por endpoints assinados e por isso exige credenciais."
    )


def _validate_max_active_symbols(max_active_symbols: int) -> None:
    if max_active_symbols < 1:
        raise ValueError("ALPHORA_MAX_ACTIVE_SYMBOLS precisa ser no minimo 1")


def _validate_trading_cadence(config: AppConfig) -> None:
    """Reject configurations that would turn Alphora into a high-frequency trader."""

    if config.loop_interval_seconds < MIN_LOOP_INTERVAL_SECONDS:
        raise ValueError(
            "ALPHORA_LOOP_INTERVAL_SECONDS precisa ser no minimo "
            f"{MIN_LOOP_INTERVAL_SECONDS:g}s: o Alphora opera day trade/swing trade, "
            "nao alta frequencia."
        )

    min_spacing = config.min_seconds_between_entries
    if not math.isfinite(min_spacing) or min_spacing < MIN_SECONDS_BETWEEN_ENTRIES_FLOOR:
        raise ValueError(
            "ALPHORA_MIN_SECONDS_BETWEEN_ENTRIES precisa ser no minimo "
            f"{MIN_SECONDS_BETWEEN_ENTRIES_FLOOR:g}s por simbolo: o Alphora nao opera "
            "alta frequencia."
        )

    if config.max_daily_trades < 0 or config.max_daily_trades > MAX_DAILY_TRADES_CEILING:
        raise ValueError(
            "ALPHORA_MAX_DAILY_TRADES precisa ficar entre 0 (usa o teto) e "
            f"{MAX_DAILY_TRADES_CEILING} entradas por simbolo por dia."
        )


def _validate_risk_bounds(config: AppConfig) -> None:
    if config.stop_loss_pct >= 1:
        raise ValueError(
            "ALPHORA_STOP_LOSS_PCT e uma fracao (0.01 = 1%) e precisa ser menor que 1."
        )

    if config.take_profit_pct >= 1:
        raise ValueError(
            "ALPHORA_TAKE_PROFIT_PCT e uma fracao (0.02 = 2%) e precisa ser menor que 1."
        )

    _validate_positive_number("ALPHORA_ATR_STOP_MULTIPLIER", config.atr_stop_multiplier)
    _validate_positive_number("ALPHORA_ATR_TAKE_MULTIPLIER", config.atr_take_multiplier)

    for name, value in (
        ("ALPHORA_MAX_DAILY_LOSS_USDT", config.max_daily_loss_usdt),
        ("ALPHORA_COOLDOWN_AFTER_LOSS_SECONDS", config.cooldown_after_loss_seconds),
        ("ALPHORA_MAX_POSITION_HOLD_SECONDS", config.max_position_hold_seconds),
        ("ALPHORA_MAX_EXPOSURE_USDT", config.max_exposure_usdt),
        (
            "ALPHORA_STOP_TRADING_AFTER_DAILY_PROFIT_USDT",
            config.stop_trading_after_daily_profit_usdt,
        ),
    ):
        _validate_non_negative_number(name, value)

    if config.max_consecutive_losses_guard < 0:
        raise ValueError("ALPHORA_MAX_CONSECUTIVE_LOSSES nao pode ser negativo")

    if 0 < config.max_exposure_usdt < config.position_notional_usdt:
        raise ValueError(
            "ALPHORA_MAX_EXPOSURE_USDT menor que ALPHORA_POSITION_NOTIONAL_USDT "
            "bloquearia todas as entradas."
        )


def _validate_ai_probabilistic_config(config: AppConfig) -> None:
    if str(getattr(config, "candle_hub_mode", "AUTO") or "AUTO").upper() not in {
        "AUTO",
        "OFF",
        "SHADOW",
        "ACTIVE_PAPER",
        "ACTIVE_TESTNET",
        "ACTIVE_REAL",
    }:
        raise ValueError(
            "ALPHORA_CANDLE_HUB_MODE precisa ser AUTO, OFF, SHADOW, "
            "ACTIVE_PAPER, ACTIVE_TESTNET ou ACTIVE_REAL."
        )

    _validate_probability(
        "ALPHORA_AI_MIN_LONG_PROBABILITY",
        config.ai_min_long_probability,
    )
    _validate_probability(
        "ALPHORA_AI_MIN_SHORT_PROBABILITY",
        config.ai_min_short_probability,
    )
    _validate_probability(
        "ALPHORA_AI_MIN_DIRECTION_GAP",
        config.ai_min_direction_gap,
    )
    _validate_probability(
        "ALPHORA_AI_CLOSE_LONG_MIN_SHORT_PROBABILITY",
        config.ai_close_long_min_short_probability,
    )
    _validate_probability(
        "ALPHORA_AI_CLOSE_SHORT_MIN_LONG_PROBABILITY",
        config.ai_close_short_min_long_probability,
    )
    _validate_non_negative_number(
        "ALPHORA_AI_MIN_EXPECTED_VALUE_BPS",
        config.ai_min_expected_value_bps,
    )

    if not config.ai_probabilistic_enabled:
        return

    if not config.ai_global_model_path:
        raise ValueError(
            "ALPHORA_AI_PROBABILISTIC_ENABLED=true exige ALPHORA_AI_GLOBAL_MODEL_PATH."
        )

    if not Path(config.ai_global_model_path).exists():
        raise ValueError(
            f"ALPHORA_AI_GLOBAL_MODEL_PATH nao encontrado: {config.ai_global_model_path}"
        )

    if config.ai_use_symbol_refined_models and not config.ai_fallback_to_global_model:
        raise ValueError(
            "ALPHORA_AI_USE_SYMBOL_REFINED_MODELS=true exige "
            "ALPHORA_AI_FALLBACK_TO_GLOBAL_MODEL=true."
        )

    if config.strategy_module != "strategies.futures.ai.probabilistic_strategy":
        raise ValueError(
            "ALPHORA_AI_PROBABILISTIC_ENABLED=true exige "
            "ProbabilisticAIStrategy como estrategia efetiva."
        )


def _validate_snapshot_config(config: AppConfig) -> None:
    margin = 0.05
    env = snapshot_env_name
    for field_name in (
        "snapshot_deadline_seconds",
        "snapshot_rest_timeout_seconds",
        "snapshot_rest_connect_timeout_seconds",
        "snapshot_rest_read_timeout_seconds",
        "snapshot_rest_transport_timeout_seconds",
        "snapshot_execution_max_age_seconds",
        "snapshot_execution_max_skew_seconds",
    ):
        _validate_positive_number(env(field_name), getattr(config, field_name))
    _validate_non_negative_number(
        env("snapshot_rest_retry_delay_seconds"),
        config.snapshot_rest_retry_delay_seconds,
    )
    if config.snapshot_rest_max_attempts < 1:
        raise ValueError(f"{env('snapshot_rest_max_attempts')} precisa ser no minimo 1")
    if config.snapshot_rest_transport_timeout_seconds + margin >= (
        config.snapshot_rest_timeout_seconds
    ):
        raise ValueError(
            f"{env('snapshot_rest_transport_timeout_seconds')} precisa ficar ao menos "
            f"0.05s abaixo de {env('snapshot_rest_timeout_seconds')}"
        )
    if (
        config.snapshot_rest_connect_timeout_seconds
        + config.snapshot_rest_read_timeout_seconds
        + margin
        >= config.snapshot_rest_transport_timeout_seconds
    ):
        raise ValueError(
            f"{env('snapshot_rest_connect_timeout_seconds')} + "
            f"{env('snapshot_rest_read_timeout_seconds')} precisa ficar ao menos "
            f"0.05s abaixo de {env('snapshot_rest_transport_timeout_seconds')}"
        )
    validate_candle_klines_url(config.candle_klines_url)
    validate_rest_weight_settings(
        mode=config.rest_weight_guard_mode,
        ratio=config.rest_weight_guard_ratio,
        limit=config.rest_weight_limit_1m,
    )


def _validate_architecture_config(config: AppConfig) -> None:
    level = config.intelligent_activation_level

    if config.architecture_mode is not ArchitectureMode.INTELLIGENT_FLOW:
        raise ValueError(
            "ALPHORA_ARCHITECTURE_MODE aceita somente INTELLIGENT_FLOW no fluxo atual."
        )

    if (
        level is IntelligentActivationLevel.PAPER_ACTIVE
        and config.execution_mode is not ExecutionMode.PAPER_LIVE
    ):
        raise ValueError(
            "ALPHORA_INTELLIGENT_ACTIVATION_LEVEL=PAPER_ACTIVE exige "
            "ALPHORA_EXECUTION_MODE=PAPER_LIVE."
        )

    testnet_levels = {
        IntelligentActivationLevel.TESTNET_OBSERVE,
        IntelligentActivationLevel.TESTNET_REDUCE,
        IntelligentActivationLevel.TESTNET_BLOCK,
    }
    if level in testnet_levels and config.execution_mode is not ExecutionMode.TESTNET_LIVE:
        raise ValueError(
            f"ALPHORA_INTELLIGENT_ACTIVATION_LEVEL={level.value} exige "
            "ALPHORA_EXECUTION_MODE=TESTNET_LIVE."
        )

    real_levels = {
        IntelligentActivationLevel.REAL_OBSERVE,
        IntelligentActivationLevel.REAL_REDUCE,
        IntelligentActivationLevel.REAL_BLOCK,
    }
    if level in real_levels and config.execution_mode is not ExecutionMode.REAL_LIVE:
        raise ValueError(
            f"ALPHORA_INTELLIGENT_ACTIVATION_LEVEL={level.value} exige "
            "ALPHORA_EXECUTION_MODE=REAL_LIVE."
        )


def parse_symbols(raw_symbols: str, *, fallback: str) -> tuple[str, ...]:
    raw = raw_symbols.strip()
    if not raw:
        return (_validate_symbol(fallback.upper()),)

    seen = set()
    symbols = []
    for raw_symbol in raw.split(","):
        if not raw_symbol.strip():
            continue
        symbol = _validate_symbol(raw_symbol.strip().upper())
        if symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    if not symbols:
        return (_validate_symbol(fallback.upper()),)
    return tuple(symbols)


def _validate_symbol(symbol: str) -> str:
    allowed_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    if not symbol:
        raise ValueError("Symbol nao pode ficar vazio")
    if any(char not in allowed_chars for char in symbol):
        raise ValueError(f"Symbol invalido: {symbol}")
    return symbol


def _build_symbol_contexts(
    *,
    symbols: tuple[str, ...],
    runtime_dir: str,
    default_state_path: str,
    default_journal_path: str,
    environment_key: str,
) -> tuple[TradingSymbolConfig, ...]:
    contexts = []
    for index, symbol in enumerate(symbols):
        state_path = (
            default_state_path
            if index == 0
            else environment_state_path(runtime_dir, symbol, environment_key)
        )
        contexts.append(
            TradingSymbolConfig(
                symbol=symbol,
                state_path=state_path,
                lock_path=f"{runtime_dir}/alphora-{symbol.lower()}-{environment_key}.lock",
                journal_path=(
                    default_journal_path
                    if index == 0
                    else environment_journal_path(symbol, environment_key)
                ),
            )
        )
    return tuple(contexts)
