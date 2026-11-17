import json
import re
import warnings as _warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import (
    AppConfig,
    ExchangeEnvironment,
    ExitModelType,
    MarginType,
    ProtectionPolicy,
    ProtectionWorkingType,
    RecoveryAction,
)
from config.state_paths import financial_path_key
from config.symbol_profile_schema import (
    SymbolProfileStrategyConflict,
    canonicalize_symbol_profile_payload,
)

SYMBOL_PROFILES_PATH = "config/alphora_symbols.local.json"
PORTFOLIO_RISK_PATH = "config/alphora_portfolio.local.json"


@dataclass(frozen=True)
class SymbolTradingConfig:
    enabled: bool
    symbol: str
    strategy_params: dict[str, Any]
    leverage: int
    margin_type: MarginType
    position_notional_usdt: float
    stop_loss_pct: float
    take_profit_pct: float
    exit_model: ExitModelType
    atr_period: int
    atr_stop_multiplier: float
    atr_take_multiplier: float
    protection_policy: ProtectionPolicy
    protection_working_type: ProtectionWorkingType
    recovery_action: RecoveryAction
    max_daily_loss_usdt: float
    max_daily_trades: int
    max_consecutive_losses_guard: int
    cooldown_after_loss_seconds: float
    max_position_hold_seconds: float
    max_exposure_usdt: float
    bot_order_prefix: str
    state_path: str
    journal_path: str
    lock_path: str
    runtime_dir: str


@dataclass(frozen=True)
class PortfolioRiskSettings:
    max_open_positions: int = 1
    max_total_exposure_usdt: float = 0.0
    max_long_exposure_usdt: float = 0.0
    max_short_exposure_usdt: float = 0.0
    max_symbol_exposure_usdt: float = 0.0
    max_group_exposure_usdt: float = 0.0
    max_daily_loss_usdt: float = 0.0
    stop_after_daily_profit_usdt: float = 0.0
    max_correlated_positions: int = 0
    allow_multiple_same_direction: bool = True
    allow_multiple_positions_same_symbol: bool = False
    max_new_positions_per_cycle: int = 1
    governor_max_symbols_per_cycle: int = 20
    correlation_groups: dict[str, str] = field(default_factory=dict)
    correlation_group_mode: str = "OBSERVE"
    spot_usdt_reserve: float = 5.0
    spot_fee_buffer_pct: float = 0.002

    def __post_init__(self) -> None:
        if self.spot_usdt_reserve < 0 or self.spot_fee_buffer_pct < 0:
            raise ValueError("Reserva USDT e buffer de taxas Spot nao podem ser negativos")
        object.__setattr__(
            self,
            "correlation_groups",
            {
                str(symbol).upper().strip(): str(group).upper().strip()
                for symbol, group in (self.correlation_groups or {}).items()
                if str(symbol).strip() and str(group).strip()
            },
        )
        object.__setattr__(
            self,
            "correlation_group_mode",
            str(self.correlation_group_mode or "OBSERVE").upper().strip() or "OBSERVE",
        )


def load_symbol_profiles(
    path: str | Path = SYMBOL_PROFILES_PATH,
    *,
    base_config: AppConfig | None = None,
    environment_key: str = "testnet",
    runtime_dir: str = "runtime",
) -> list[SymbolTradingConfig]:
    profile_path = Path(path)
    if not profile_path.exists():
        if base_config is None:
            return []
        return [
            symbol_config_from_app_config(
                base_config,
                symbol=symbol,
                environment_key=base_config.environment_key,
            )
            for symbol in base_config.symbols
        ]

    with profile_path.open("r", encoding="utf-8") as file:
        raw_payload = json.load(file)

    if isinstance(raw_payload, dict):
        raw_profiles = raw_payload.get("symbol_profiles", [])
    elif isinstance(raw_payload, list):
        raw_profiles = raw_payload
    else:
        raise ValueError("Arquivo de perfis multi-moedas precisa conter objeto ou lista JSON")

    if not isinstance(raw_profiles, list):
        raise ValueError("symbol_profiles precisa ser uma lista")

    profiles = [
        parse_symbol_profile(
            raw_profile,
            base_config=base_config,
            environment_key=environment_key if base_config is None else base_config.environment_key,
            runtime_dir=runtime_dir if base_config is None else base_config.runtime_dir,
        )
        for raw_profile in raw_profiles
    ]
    validate_symbol_profiles(profiles)
    if base_config is not None and base_config.base_url in {
        "https://api.binance.com", "https://testnet.binance.vision"
    }:
        for profile in profiles:
            if profile.enabled and profile.leverage != 1:
                raise ValueError(f"{profile.symbol}: Spot exige leverage=1")
    return profiles


def resolve_governor_symbol_configs(
    config: AppConfig,
    *,
    environment_key: str | None = None,
    runtime_dir: str | None = None,
) -> list[SymbolTradingConfig]:
    try:
        return load_symbol_profiles(
            config.symbol_profiles_path,
            base_config=config,
            environment_key=environment_key or config.environment_key,
            runtime_dir=runtime_dir or config.runtime_dir,
        )
    except ValueError as error:
        if "Nenhum perfil multi-moedas habilitado" not in str(error):
            raise
        return [symbol_config_from_app_config(config, environment_key=config.environment_key)]


def save_symbol_profiles(
    profiles: list[dict[str, Any]] | list[SymbolTradingConfig],
    path: str | Path = SYMBOL_PROFILES_PATH,
) -> None:
    payload = {
        "symbol_profiles": [
            profile_to_json(profile) if isinstance(profile, SymbolTradingConfig) else profile
            for profile in profiles
        ]
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def load_portfolio_risk_settings(path: str | Path = PORTFOLIO_RISK_PATH) -> PortfolioRiskSettings:
    settings_path = Path(path)
    if not settings_path.exists():
        return PortfolioRiskSettings()

    try:
        with settings_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Arquivo de risco de portfolio corrompido ou inválido: {settings_path}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError("Arquivo de risco de portfolio precisa conter objeto JSON")

    return portfolio_settings_from_dict(payload)


def save_portfolio_risk_settings(
    settings: dict[str, Any] | PortfolioRiskSettings,
    path: str | Path = PORTFOLIO_RISK_PATH,
) -> None:
    payload = asdict(settings) if isinstance(settings, PortfolioRiskSettings) else dict(settings)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def parse_symbol_profile(
    payload: dict[str, Any],
    *,
    base_config: AppConfig | None = None,
    environment_key: str = "testnet",
    runtime_dir: str = "runtime",
) -> SymbolTradingConfig:
    if not isinstance(payload, dict):
        raise ValueError("Cada perfil de moeda precisa ser um objeto JSON")

    try:
        canonical = canonicalize_symbol_profile_payload(
            payload,
            global_strategy_module=(
                base_config.strategy_module if base_config is not None else None
            ),
            global_strategy_class=(base_config.strategy_class if base_config is not None else None),
            reject_strategy_conflicts=True,
        )
    except SymbolProfileStrategyConflict:
        raise
    if canonical.warnings:
        for warning in canonical.warnings:
            _warnings.warn(warning, stacklevel=3)
    payload = canonical.payload

    symbol = _normalize_symbol(
        str(payload.get("symbol") or _base("symbol", base_config, "BTCUSDT"))
    )
    runtime = str(payload.get("runtime_dir") or _base("runtime_dir", base_config, runtime_dir))
    env_key = environment_key.lower().strip()
    path_key = (
        financial_path_key(env_key, base_config.execution_mode.value)
        if base_config is not None
        else env_key
    )
    return SymbolTradingConfig(
        enabled=_bool(payload.get("enabled", True)),
        symbol=symbol,
        strategy_params=_dict(payload.get("strategy_params", {})),
        leverage=_int(payload.get("leverage", _base("leverage", base_config, 1))),
        margin_type=_enum(
            MarginType,
            payload.get("margin_type", _base("margin_type", base_config, MarginType.ISOLATED)),
        ),
        position_notional_usdt=_float(
            payload.get(
                "position_notional_usdt",
                _base("position_notional_usdt", base_config, 20.0),
            )
        ),
        stop_loss_pct=_float(
            payload.get("stop_loss_pct", _base("stop_loss_pct", base_config, 0.01))
        ),
        take_profit_pct=_float(
            payload.get("take_profit_pct", _base("take_profit_pct", base_config, 0.02))
        ),
        exit_model=_enum(
            ExitModelType,
            payload.get("exit_model", _base("exit_model", base_config, ExitModelType.FIXED_PCT)),
        ),
        atr_period=_int(payload.get("atr_period", _base("atr_period", base_config, 14))),
        atr_stop_multiplier=_float(
            payload.get(
                "atr_stop_multiplier",
                _base("atr_stop_multiplier", base_config, 1.5),
            )
        ),
        atr_take_multiplier=_float(
            payload.get(
                "atr_take_multiplier",
                _base("atr_take_multiplier", base_config, 2.0),
            )
        ),
        protection_policy=_enum(
            ProtectionPolicy,
            payload.get(
                "protection_policy",
                _base("protection_policy", base_config, ProtectionPolicy.AUTO_REPAIR),
            ),
        ),
        protection_working_type=(
            ProtectionWorkingType.CONTRACT_PRICE
            if base_config is not None
            and getattr(base_config, "environment", None) is ExchangeEnvironment.TESTNET
            else _enum(
                ProtectionWorkingType,
                payload.get(
                    "protection_working_type",
                    _base(
                        "protection_working_type",
                        base_config,
                        ProtectionWorkingType.MARK_PRICE,
                    ),
                ),
            )
        ),
        recovery_action=_enum(
            RecoveryAction,
            payload.get(
                "recovery_action", _base("recovery_action", base_config, RecoveryAction.PROTECT)
            ),
        ),
        max_daily_loss_usdt=_float(
            payload.get("max_daily_loss_usdt", _base("max_daily_loss_usdt", base_config, 0.0))
        ),
        max_daily_trades=_int(
            payload.get("max_daily_trades", _base("max_daily_trades", base_config, 0))
        ),
        max_consecutive_losses_guard=_int(
            payload.get(
                "max_consecutive_losses_guard",
                payload.get(
                    "max_consecutive_losses",
                    _base("max_consecutive_losses_guard", base_config, 0),
                ),
            )
        ),
        cooldown_after_loss_seconds=_float(
            payload.get(
                "cooldown_after_loss_seconds",
                _base("cooldown_after_loss_seconds", base_config, 0.0),
            )
        ),
        max_position_hold_seconds=_float(
            payload.get(
                "max_position_hold_seconds",
                _base("max_position_hold_seconds", base_config, 0.0),
            )
        ),
        max_exposure_usdt=_float(
            payload.get("max_exposure_usdt", _base("max_exposure_usdt", base_config, 0.0))
        ),
        bot_order_prefix=_normalize_prefix(
            str(payload.get("bot_order_prefix") or make_symbol_order_prefix(symbol))
        ),
        state_path=str(
            payload.get("state_path") or Path(runtime) / f"state_{symbol.lower()}_{path_key}.json"
        ),
        journal_path=str(
            payload.get("journal_path")
            or Path("data") / f"trades_{symbol.lower()}_{path_key}.jsonl"
        ),
        lock_path=str(
            payload.get("lock_path") or Path(runtime) / f"alphora-{symbol.lower()}-{path_key}.lock"
        ),
        runtime_dir=runtime,
    )


def symbol_config_from_app_config(
    config: AppConfig,
    *,
    symbol: str | None = None,
    environment_key: str | None = None,
) -> SymbolTradingConfig:
    selected_symbol = _normalize_symbol(symbol or config.symbol)
    env_key = (environment_key or config.environment_key).lower()
    path_key = financial_path_key(env_key, config.execution_mode.value)
    first_symbol = selected_symbol == config.symbol
    return SymbolTradingConfig(
        enabled=True,
        symbol=selected_symbol,
        strategy_params=dict(config.strategy_params),
        leverage=config.leverage,
        margin_type=config.margin_type,
        position_notional_usdt=config.position_notional_usdt,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        exit_model=config.exit_model,
        atr_period=config.atr_period,
        atr_stop_multiplier=config.atr_stop_multiplier,
        atr_take_multiplier=config.atr_take_multiplier,
        protection_policy=config.protection_policy,
        protection_working_type=config.protection_working_type,
        recovery_action=config.recovery_action,
        max_daily_loss_usdt=config.max_daily_loss_usdt,
        max_daily_trades=config.max_daily_trades,
        max_consecutive_losses_guard=config.max_consecutive_losses_guard,
        cooldown_after_loss_seconds=config.cooldown_after_loss_seconds,
        max_position_hold_seconds=config.max_position_hold_seconds,
        max_exposure_usdt=config.max_exposure_usdt,
        bot_order_prefix=(
            _normalize_prefix(config.bot_order_prefix)
            if first_symbol
            else make_symbol_order_prefix(selected_symbol)
        ),
        state_path=(
            config.state_path
            if first_symbol
            else (
                Path(config.runtime_dir) / f"state_{selected_symbol.lower()}_{path_key}.json"
            ).as_posix()
        ),
        journal_path=(
            config.trade_journal_path
            if first_symbol
            else (Path("data") / f"trades_{selected_symbol.lower()}_{path_key}.jsonl").as_posix()
        ),
        lock_path=(
            Path(config.runtime_dir) / f"alphora-{selected_symbol.lower()}-{path_key}.lock"
        ).as_posix(),
        runtime_dir=config.runtime_dir,
    )


def validate_symbol_profiles(profiles: list[SymbolTradingConfig]) -> None:
    enabled = [profile for profile in profiles if profile.enabled]
    if not enabled:
        raise ValueError("Nenhum perfil multi-moedas habilitado")

    _validate_unique(enabled, "symbol", "simbolo duplicado")
    _validate_unique(enabled, "bot_order_prefix", "prefixo de ordem duplicado")
    _validate_unique(enabled, "state_path", "state_path duplicado")
    _validate_unique(enabled, "journal_path", "journal_path duplicado")
    _validate_unique(enabled, "lock_path", "lock_path duplicado")

    for profile in enabled:
        if profile.leverage < 1 or profile.leverage > 5:
            raise ValueError(f"{profile.symbol}: leverage precisa ficar entre 1 e 5")
        if profile.position_notional_usdt <= 0:
            raise ValueError(f"{profile.symbol}: position_notional_usdt precisa ser maior que zero")
        if profile.stop_loss_pct <= 0 or profile.take_profit_pct <= 0:
            raise ValueError(f"{profile.symbol}: stop/take precisam ser positivos")
        if len(profile.bot_order_prefix) > 12:
            raise ValueError(f"{profile.symbol}: bot_order_prefix deve ter no maximo 12 caracteres")


def portfolio_settings_from_dict(payload: dict[str, Any]) -> PortfolioRiskSettings:
    return PortfolioRiskSettings(
        max_open_positions=_int(payload.get("max_open_positions", 1)),
        max_total_exposure_usdt=_float(payload.get("max_total_exposure_usdt", 0)),
        max_long_exposure_usdt=_float(payload.get("max_long_exposure_usdt", 0)),
        max_short_exposure_usdt=_float(payload.get("max_short_exposure_usdt", 0)),
        max_symbol_exposure_usdt=_float(payload.get("max_symbol_exposure_usdt", 0)),
        max_group_exposure_usdt=_float(payload.get("max_group_exposure_usdt", 0)),
        max_daily_loss_usdt=_float(payload.get("max_daily_loss_usdt", 0)),
        stop_after_daily_profit_usdt=_float(payload.get("stop_after_daily_profit_usdt", 0)),
        max_correlated_positions=_int(payload.get("max_correlated_positions", 0)),
        allow_multiple_same_direction=_bool(payload.get("allow_multiple_same_direction", True)),
        allow_multiple_positions_same_symbol=_bool(
            payload.get("allow_multiple_positions_same_symbol", False)
        ),
        max_new_positions_per_cycle=_int(payload.get("max_new_positions_per_cycle", 1)),
        governor_max_symbols_per_cycle=_int(payload.get("governor_max_symbols_per_cycle", 20)),
        correlation_groups=_correlation_groups(payload.get("correlation_groups", {})),
        correlation_group_mode=_normalize_correlation_group_mode(
            payload.get("correlation_group_mode", "OBSERVE")
        ),
        spot_usdt_reserve=_float(payload.get("spot_usdt_reserve", 5.0)),
        spot_fee_buffer_pct=_float(payload.get("spot_fee_buffer_pct", 0.002)),
    )


def profile_to_json(profile: SymbolTradingConfig) -> dict[str, Any]:
    payload = asdict(profile)
    for key in [
        "margin_type",
        "exit_model",
        "protection_policy",
        "protection_working_type",
        "recovery_action",
    ]:
        payload[key] = payload[key].value
    return payload


def make_symbol_order_prefix(symbol: str) -> str:
    base = _normalize_symbol(symbol)
    for suffix in ("USDT", "USDC", "BUSD"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return _normalize_prefix(f"ALP_{base[:8]}")


def _base(name: str, config: AppConfig | None, default):
    if config is None:
        return default
    return getattr(config, name)


def _enum(enum_type, value):
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value).upper())


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {"true", "1", "yes", "y", "sim"}:
        return True

    if normalized in {"false", "0", "no", "n", "nao", "não"}:
        return False

    raise ValueError(f"Valor booleano invalido: {value}")


def _int(value: Any) -> int:
    return int(str(value).strip())


def _float(value: Any) -> float:
    return float(str(value).strip())


def _dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise ValueError("strategy_params precisa ser um objeto JSON")


def _correlation_groups(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("correlation_groups precisa ser um objeto JSON")

    groups: dict[str, str] = {}
    for raw_symbol, raw_group in value.items():
        symbol = _normalize_symbol(str(raw_symbol))
        group = str(raw_group or "").upper().strip()
        if group:
            groups[symbol] = group
    return groups


def _normalize_correlation_group_mode(value: Any) -> str:
    normalized = str(value or "OBSERVE").upper().strip()
    return normalized or "OBSERVE"


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.upper().strip()
    if not normalized:
        raise ValueError("Symbol nao pode ficar vazio")
    if not re.fullmatch(r"[A-Z0-9]+", normalized):
        raise ValueError(f"Symbol invalido: {symbol}")
    return normalized


def _normalize_prefix(prefix: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", prefix.upper().strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("bot_order_prefix nao pode ficar vazio")
    return normalized[:12]


def _validate_unique(profiles: list[SymbolTradingConfig], attr: str, message: str) -> None:
    seen: dict[str, str] = {}
    for profile in profiles:
        value = str(getattr(profile, attr)).lower()
        if value in seen:
            raise ValueError(f"{message}: {seen[value]} e {profile.symbol}")
        seen[value] = profile.symbol
