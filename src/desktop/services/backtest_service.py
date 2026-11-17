from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backtesting.adapters.desktop import (
    BacktestArtifactPaths,
    build_backtest_command,
    build_backtest_request,
    build_download_klines_command,
    extract_backtest_rules_from_exchange_info,
    load_backtest_summary,
    parse_backtest_artifact_paths,
    write_backtest_request,
)
from binance.public_rest import tracked_public_get
from binance.rest_weight import shared_rest_weight_ledger
from desktop.settings_store import normalize_environment, settings_to_env

HttpGet = Callable[..., Any]
PathExists = Callable[[str | Path], bool]

_BACKTEST_STRING_FIELDS = (
    "backtest_interval",
    "backtest_csv_path",
    "backtest_start",
    "backtest_end",
    "backtest_quantity_step",
    "backtest_min_quantity",
    "backtest_min_notional",
    "backtest_price_tick",
    "backtest_initial_balance",
    "maker_fee_pct",
    "taker_fee_pct",
    "slippage_bps",
    "spread_bps",
    "default_funding_rate",
)

_POSITIVE_FIELDS = {
    "backtest_quantity_step": "Quantity step",
    "backtest_min_quantity": "Min quantity",
    "backtest_min_notional": "Min notional",
    "backtest_price_tick": "Price tick",
    "backtest_initial_balance": "Initial balance",
}

_NON_NEGATIVE_FIELDS = {
    "maker_fee_pct": "Maker fee",
    "taker_fee_pct": "Taker fee",
    "slippage_bps": "Slippage",
    "spread_bps": "Spread",
}


@dataclass(frozen=True, slots=True)
class BacktestLaunchPlan:
    command: tuple[str, ...]
    environment: dict[str, str]
    settings: dict[str, Any]
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DownloadLaunchPlan:
    command: tuple[str, ...]
    settings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    summary: dict[str, Any]
    artifacts: BacktestArtifactPaths
    summary_text: str


def normalize_backtest_settings(
    current_settings: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    settings = deepcopy(dict(current_settings))
    settings.update(deepcopy(dict(patch)))
    for key in _BACKTEST_STRING_FIELDS:
        settings[key] = str(settings.get(key, "")).strip()
    settings["symbol"] = str(settings.get("symbol", "BTCUSDT")).upper().strip()
    settings["backtest_symbol_scope"] = str(
        settings.get("backtest_symbol_scope", "CURRENT")
    ).upper()
    settings["strategy_selection_mode"] = str(
        settings.get("strategy_selection_mode", "FIXED")
    ).upper()
    settings["funding_enabled"] = _as_bool(settings.get("funding_enabled", True))
    return settings


def validate_backtest_settings(settings: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    if not str(settings.get("symbol", "")).strip():
        errors.append("Informe o símbolo do backtest.")
    if not str(settings.get("backtest_interval", "")).strip():
        errors.append("Informe o intervalo dos candles.")
    if not str(settings.get("backtest_start", "")).strip():
        errors.append("Informe a data inicial.")
    if not str(settings.get("backtest_end", "")).strip():
        errors.append("Informe a data final.")
    if str(settings.get("backtest_symbol_scope", "CURRENT")).upper() not in {
        "CURRENT",
        "ENABLED",
    }:
        errors.append("Escopo de símbolos inválido.")
    if str(settings.get("strategy_selection_mode", "FIXED")).upper() not in {
        "FIXED",
        "PROBABILISTIC_AI",
    }:
        errors.append("Modo de estratégia inválido.")

    for key, label in _POSITIVE_FIELDS.items():
        value = _number(settings.get(key), label, errors)
        if value is not None and value <= 0:
            errors.append(f"{label} precisa ser maior que zero.")
    for key, label in _NON_NEGATIVE_FIELDS.items():
        value = _number(settings.get(key), label, errors)
        if value is not None and value < 0:
            errors.append(f"{label} não pode ser negativo.")
    return tuple(errors)


def resolve_backtest_symbols(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    scope = str(settings.get("backtest_symbol_scope", "CURRENT")).upper()
    if scope == "ENABLED":
        symbols = tuple(
            dict.fromkeys(
                str(profile.get("symbol", "")).upper().strip()
                for profile in profiles
                if _as_bool(profile.get("enabled", True)) and str(profile.get("symbol", "")).strip()
            )
        )
        if symbols:
            return symbols
    symbol = str(settings.get("symbol", "BTCUSDT")).upper().strip()
    return (symbol or "BTCUSDT",)


def resolve_csv_arguments(
    csv_path: str,
    symbols: Sequence[str],
    *,
    project_root: str | Path,
    exists: PathExists | None = None,
) -> tuple[str | None, str | None]:
    checker = exists or _path_exists
    raw_path = str(csv_path or "").strip()
    if not raw_path:
        raise ValueError("Informe um arquivo CSV ou baixe os candles históricos antes do backtest.")
    root = Path(project_root)
    normalized_symbols = tuple(
        str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()
    )
    if len(normalized_symbols) <= 1:
        resolved = _resolve_path(raw_path, root)
        if not checker(resolved):
            raise FileNotFoundError(f"CSV não encontrado: {resolved}")
        return str(resolved), None

    if "{symbol}" not in raw_path and "{SYMBOL}" not in raw_path:
        raise ValueError(
            "Para vários símbolos, use {symbol} no caminho do CSV, por exemplo "
            "data/market/{symbol}_1m.csv."
        )
    csv_map: list[str] = []
    for symbol in normalized_symbols:
        item = raw_path.replace("{symbol}", symbol).replace("{SYMBOL}", symbol)
        resolved = _resolve_path(item, root)
        if not checker(resolved):
            raise FileNotFoundError(f"CSV não encontrado para {symbol}: {resolved}")
        csv_map.append(f"{symbol}={resolved}")
    return None, ",".join(csv_map)


def _validate_download_settings(settings: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    if not str(settings.get("symbol", "")).strip():
        errors.append("Informe o símbolo do backtest.")
    if not str(settings.get("backtest_interval", "")).strip():
        errors.append("Informe o intervalo dos candles.")
    if not str(settings.get("backtest_start", "")).strip():
        errors.append("Informe a data inicial.")
    if not str(settings.get("backtest_end", "")).strip():
        errors.append("Informe a data final.")
    return tuple(errors)


def prepare_download_plan(
    settings: Mapping[str, Any],
    *,
    project_root: str | Path,
) -> DownloadLaunchPlan:
    normalized = normalize_backtest_settings(settings, {})
    errors = _validate_download_settings(normalized)
    if errors:
        raise ValueError(errors[0])

    # Do not use backtest_csv_path as output, because it leads to overwriting files
    # if the user changes the timeframe in the UI and clicks download again.
    # By passing output=None, the CLI will automatically use the standard path
    # in data/market/SYMBOL_INTERVAL_START_END.csv.
    command = build_download_klines_command(
        symbol=str(normalized["symbol"]),
        interval=str(normalized["backtest_interval"]),
        start=str(normalized["backtest_start"]),
        end=str(normalized["backtest_end"]),
        output=None,
    )
    return DownloadLaunchPlan(command=tuple(command), settings=normalized)


def prepare_backtest_plan(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    portfolio_settings: Mapping[str, Any],
    *,
    selected_strategy: str,
    project_root: str | Path,
    exists: PathExists | None = None,
) -> BacktestLaunchPlan:
    normalized = normalize_backtest_settings(settings, {})
    errors = validate_backtest_settings(normalized)
    if errors:
        raise ValueError(errors[0])

    strategy_mode = str(normalized.get("strategy_selection_mode", "FIXED")).upper()
    strategy_params = _strategy_params(normalized, strategy_mode)
    if strategy_mode == "PROBABILISTIC_AI":
        strategy_module = "strategies.futures.ai.probabilistic_strategy"
        strategy_class = "ProbabilisticAIStrategy"
        if not str(strategy_params.get("global_model_path", "")).strip():
            raise ValueError("Selecione um modelo global de IA antes do backtest probabilístico.")
    else:
        strategy_module, strategy_class = split_qualified_strategy(
            selected_strategy or _qualified_strategy(normalized)
        )
        if f"{strategy_module}.{strategy_class}" != _qualified_strategy(normalized):
            strategy_params = {}

    symbols = resolve_backtest_symbols(normalized, profiles)
    csv_arg, csv_map_arg = resolve_csv_arguments(
        str(normalized.get("backtest_csv_path", "")),
        symbols,
        project_root=project_root,
        exists=exists,
    )

    checker = exists or _path_exists
    primary_interval = str(normalized.get("backtest_interval", "1m") or "1m")
    csv_by_symbol_timeframe = {}

    from domain.timeframe import SUPPORTED_TIMEFRAMES

    if csv_map_arg:
        primary_csvs = {
            item.split("=")[0]: item.split("=")[1] for item in csv_map_arg.split(",") if "=" in item
        }
    else:
        primary_csvs = {
            symbol: str(csv_arg).replace("{symbol}", symbol).replace("{SYMBOL}", symbol)
            for symbol in symbols
        }

    for symbol, primary_csv in primary_csvs.items():
        found_timeframes = {}
        path = Path(primary_csv)
        path_name = path.name

        search_str = f"_{primary_interval}_"
        if search_str not in path_name:
            search_str = f"{primary_interval}"

        for tf in SUPPORTED_TIMEFRAMES:
            if tf == primary_interval:
                continue
            if search_str in path_name:
                new_name = path_name.replace(search_str, f"_{tf}_")
                if new_name == path_name:
                    new_name = path_name.replace(primary_interval, tf)
                candidate = path.with_name(new_name)
                if checker(candidate):
                    found_timeframes[tf] = str(candidate)

        if _as_bool(normalized.get("use_secondary_csv")):
            sec_path_raw = str(normalized.get("secondary_csv_path", "")).strip()
            if sec_path_raw:
                sec_path_str = (
                    str(_resolve_path(sec_path_raw, Path(project_root)))
                    .replace("{symbol}", symbol)
                    .replace("{SYMBOL}", symbol)
                )
                if checker(sec_path_str):
                    sec_name = Path(sec_path_str).name
                    detected_tf = None
                    for tf in SUPPORTED_TIMEFRAMES:
                        if f"_{tf}_" in sec_name or f"_{tf}." in sec_name:
                            detected_tf = tf
                            break
                    if detected_tf and detected_tf != primary_interval:
                        found_timeframes[detected_tf] = sec_path_str

        if found_timeframes:
            csv_by_symbol_timeframe[symbol] = found_timeframes

    csv_by_symbol_timeframe_json = (
        json.dumps(csv_by_symbol_timeframe, ensure_ascii=False) if csv_by_symbol_timeframe else None
    )
    funding_csv_by_symbol = _discover_funding_csvs(
        project_root=Path(project_root),
        symbols=symbols,
        start=str(normalized.get("backtest_start", "") or ""),
        end=str(normalized.get("backtest_end", "") or ""),
        enabled=_as_bool(normalized.get("funding_enabled", True)),
        explicit_path=str(normalized.get("backtest_funding_csv_path", "") or ""),
        path_exists=checker,
    )
    funding_csv_map_json = (
        json.dumps(funding_csv_by_symbol, ensure_ascii=False) if funding_csv_by_symbol else None
    )
    strategy_params_json = (
        json.dumps(strategy_params, ensure_ascii=False, sort_keys=True) if strategy_params else None
    )
    symbol_model_map = strategy_params.get("symbol_model_paths", {})
    symbol_model_map_json = (
        json.dumps(symbol_model_map, ensure_ascii=False, sort_keys=True)
        if strategy_mode == "PROBABILISTIC_AI"
        else None
    )
    request = build_backtest_request(
        csv_path=csv_arg,
        csv_map=csv_map_arg,
        csv_by_symbol_timeframe=csv_by_symbol_timeframe_json,
        funding_csv_map=funding_csv_map_json,
        symbol=str(normalized["symbol"]),
        symbols=",".join(symbols) if len(symbols) > 1 else None,
        strategy_mode=strategy_mode,
        strategy_module=strategy_module,
        strategy_class=strategy_class,
        timeframe=str(normalized.get("backtest_interval", "1m") or "1m"),
        strategy_params=strategy_params_json,
        global_model_path=(
            str(strategy_params.get("global_model_path", "") or "")
            if strategy_mode == "PROBABILISTIC_AI"
            else None
        ),
        symbol_model_map=symbol_model_map_json,
        use_symbol_refined_models=_as_bool(strategy_params.get("use_symbol_refined_models", True)),
        fallback_to_global_model=_as_bool(strategy_params.get("fallback_to_global_model", True)),
        min_long_probability=_optional_backtest_param(strategy_params, "min_long_probability"),
        min_short_probability=_optional_backtest_param(strategy_params, "min_short_probability"),
        min_direction_gap=_optional_backtest_param(strategy_params, "min_direction_gap"),
        min_expected_value_bps=_optional_backtest_param(strategy_params, "min_expected_value_bps"),
        initial_balance=str(normalized["backtest_initial_balance"]),
        maker_fee_pct=str(normalized["maker_fee_pct"]),
        taker_fee_pct=str(normalized["taker_fee_pct"]),
        slippage_bps=str(normalized["slippage_bps"]),
        spread_bps=str(normalized["spread_bps"]),
        funding_enabled=normalized.get("funding_enabled", True),
        default_funding_rate=str(normalized.get("default_funding_rate", "0")),
        position_notional=str(normalized.get("position_notional_usdt", "60")),
        stop_loss_pct=str(normalized.get("stop_loss_pct", "0.01")),
        take_profit_pct=str(normalized.get("take_profit_pct", "0.02")),
        leverage=str(normalized.get("leverage", "1")),
        quantity_step=str(normalized["backtest_quantity_step"]),
        min_quantity=str(normalized["backtest_min_quantity"]),
        min_notional=str(normalized["backtest_min_notional"]),
        price_tick=str(normalized["backtest_price_tick"]),
        max_open_positions=str(portfolio_settings.get("max_open_positions", "1")),
        max_total_exposure=str(portfolio_settings.get("max_total_exposure_usdt", "0")),
        max_long_exposure=str(portfolio_settings.get("max_long_exposure_usdt", "0")),
        max_short_exposure=str(portfolio_settings.get("max_short_exposure_usdt", "0")),
        max_symbol_exposure=str(portfolio_settings.get("max_symbol_exposure_usdt", "0")),
        max_group_exposure=str(portfolio_settings.get("max_group_exposure_usdt", "0")),
        max_correlated_positions=str(portfolio_settings.get("max_correlated_positions", "0")),
        max_new_positions_per_cycle=str(portfolio_settings.get("max_new_positions_per_cycle", "1")),
        output_dir=str(_resolve_path("reports", Path(project_root))),
        data_start_timestamp=str(normalized.get("backtest_start", "") or "") or None,
        data_end_timestamp=str(normalized.get("backtest_end", "") or "") or None,
        evaluation_mode=str(normalized.get("backtest_evaluation_mode", "IN_SAMPLE")),
        evaluation_start_timestamp=(
            str(normalized.get("backtest_evaluation_start", "") or "") or None
        ),
        evaluation_end_timestamp=(str(normalized.get("backtest_evaluation_end", "") or "") or None),
        fail_on_quality_warnings=_as_bool(
            normalized.get("backtest_fail_on_quality_warnings", False)
        ),
        purge_candles=int(normalized.get("backtest_purge_candles", 0) or 0),
        embargo_candles=int(normalized.get("backtest_embargo_candles", 0) or 0),
        bootstrap_samples=int(normalized.get("backtest_bootstrap_samples", 1000) or 1000),
        walk_forward_folds=int(normalized.get("backtest_walk_forward_folds", 5) or 5),
        parameter_trials=int(normalized.get("backtest_parameter_trials", 1) or 1),
    )
    request_path = Path(project_root) / "runtime" / "backtests" / request.run_id / "request.json"
    write_backtest_request(request_path, request)
    command = build_backtest_command(request_path=request_path)
    return BacktestLaunchPlan(
        command=tuple(command),
        environment=settings_to_env(normalized),
        settings=normalized,
        symbols=tuple(symbols),
    )


def _discover_funding_csvs(
    *,
    project_root: Path,
    symbols: Sequence[str],
    start: str,
    end: str,
    enabled: bool,
    explicit_path: str,
    path_exists: PathExists,
) -> dict[str, str]:
    if not enabled:
        return {}
    discovered: dict[str, str] = {}
    if explicit_path:
        for symbol in symbols:
            resolved = (
                str(_resolve_path(explicit_path, project_root))
                .replace("{symbol}", symbol)
                .replace("{SYMBOL}", symbol)
            )
            if path_exists(resolved):
                discovered[symbol] = resolved
        return discovered

    market_dir = project_root / "data" / "market"
    for symbol in symbols:
        exact = market_dir / f"{symbol}_funding_{start}_{end}.csv"
        if start and end and path_exists(exact):
            discovered[symbol] = str(exact)
    return discovered


def fetch_symbol_rules(
    settings: Mapping[str, Any],
    *,
    http_get: HttpGet | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, str]:
    environment = normalize_environment(dict(settings))
    base_url = (
        "https://demo-fapi.binance.com" if environment == "TESTNET" else "https://fapi.binance.com"
    )
    symbol = str(settings.get("symbol", "BTCUSDT")).upper().strip()
    if not symbol:
        raise ValueError("Informe o símbolo antes de buscar as regras.")
    url = f"{base_url}/fapi/v1/exchangeInfo"
    params = {"symbol": symbol}
    if http_get is None:
        response = tracked_public_get(
            url,
            params=params,
            timeout=float(timeout_seconds),
            source="desktop:backtest_rules",
            ledger=shared_rest_weight_ledger(),
        )
    else:
        response = http_get(url, params=params, timeout=float(timeout_seconds))
    response.raise_for_status()
    return extract_backtest_rules_from_exchange_info(response.json(), symbol)


def parse_downloaded_path(
    output_lines: Sequence[str],
    *,
    project_root: str | Path,
) -> Path | None:
    for line in output_lines:
        if line.startswith("Candles baixados:"):
            raw = line.split(":", 1)[1].strip()
            return _resolve_path(raw, Path(project_root))
    return None


def load_backtest_result(
    output_lines: Sequence[str],
    *,
    project_root: str | Path,
) -> BacktestResult:
    raw_artifacts = parse_backtest_artifact_paths(list(output_lines))
    artifacts = BacktestArtifactPaths(
        manifest_json=_absolute_artifact(raw_artifacts.manifest_json, project_root),
        summary_json=_absolute_artifact(raw_artifacts.summary_json, project_root),
        trades_csv=_absolute_artifact(raw_artifacts.trades_csv, project_root),
        decisions_jsonl=_absolute_artifact(raw_artifacts.decisions_jsonl, project_root),
        rejected_jsonl=_absolute_artifact(raw_artifacts.rejected_jsonl, project_root),
        equity_csv=_absolute_artifact(raw_artifacts.equity_csv, project_root),
    )
    if artifacts.summary_json is None or not artifacts.summary_json.is_file():
        raise FileNotFoundError("O backtest terminou sem gerar um relatório JSON válido.")
    summary = load_backtest_summary(artifacts.summary_json)
    return BacktestResult(
        summary=summary,
        artifacts=artifacts,
        summary_text=format_backtest_summary(summary),
    )


def format_backtest_summary(summary: Mapping[str, Any]) -> str:
    reasons = [str(item) for item in summary.get("readiness_reasons", [])]
    warnings = [str(item) for item in summary.get("readiness_warnings", [])]
    lines = [
        "Símbolos testados: " + ", ".join(summary.get("symbols_tested", []) or ["-"]),
        "Símbolos com trades: " + ", ".join(summary.get("symbols_with_trades", []) or ["-"]),
        f"Total de trades: {summary.get('total_trades', 0)}",
        f"Sinais rejeitados: {summary.get('rejected_signals', 0)}",
        f"PnL líquido: {summary.get('net_pnl', '0')}",
        f"Win rate: {summary.get('win_rate', '0')}",
        f"Profit factor: {summary.get('profit_factor', '0')}",
        f"Max drawdown: {summary.get('max_drawdown', '0')}",
        f"Sharpe: {summary.get('sharpe_ratio', '0')}",
        f"Sortino: {summary.get('sortino_ratio', '0')}",
        f"Confiabilidade: {'APROVADA' if summary.get('reliability_passed') else 'REPROVADA'}",
        f"Veredito operacional: {summary.get('operational_verdict', 'UNKNOWN')}",
        f"Readiness: {summary.get('readiness_status', 'UNKNOWN')}",
    ]
    if reasons:
        lines.extend(["", "Razões:", *[f"- {item}" for item in reasons]])
    if warnings:
        lines.extend(["", "Avisos:", *[f"- {item}" for item in warnings]])
    lines.extend(
        [
            "",
            "BACKTEST_OK não significa operar em conta real. Valide em paper e testnet antes.",
        ]
    )
    return "\n".join(lines)


def artifact_map(paths: BacktestArtifactPaths) -> dict[str, str]:
    return {
        "manifestJson": str(paths.manifest_json or ""),
        "summaryJson": str(paths.summary_json or ""),
        "tradesCsv": str(paths.trades_csv or ""),
        "eventsJsonl": str(paths.decisions_jsonl or ""),
        "rejectedJsonl": str(paths.rejected_jsonl or ""),
        "equityCsv": str(paths.equity_csv or ""),
    }


def split_qualified_strategy(value: str) -> tuple[str, str]:
    normalized = str(value or "").strip()
    if "." not in normalized:
        raise ValueError("Estratégia precisa estar no formato módulo.Classe.")
    module_name, class_name = normalized.rsplit(".", 1)
    if not module_name or not class_name:
        raise ValueError("Estratégia precisa estar no formato módulo.Classe.")
    return module_name, class_name


def _strategy_params(settings: Mapping[str, Any], strategy_mode: str) -> dict[str, Any]:
    if strategy_mode != "PROBABILISTIC_AI":
        raw = settings.get("strategy_params", {})
        return deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
    symbol_paths = settings.get("ai_symbol_model_paths", {})
    return {
        "global_model_path": str(settings.get("ai_global_model_path", "")).strip(),
        "symbol_model_paths": deepcopy(dict(symbol_paths))
        if isinstance(symbol_paths, Mapping)
        else {},
        "use_symbol_refined_models": _as_bool(settings.get("ai_use_symbol_refined_models", True)),
        "fallback_to_global_model": _as_bool(settings.get("ai_fallback_to_global_model", True)),
        "min_long_probability": str(settings.get("ai_min_long_probability", "0.62")),
        "min_short_probability": str(settings.get("ai_min_short_probability", "0.62")),
        "min_direction_gap": str(settings.get("ai_min_direction_gap", "0.18")),
        "min_expected_value_bps": str(settings.get("ai_min_expected_value_bps", "3.0")),
    }


def _optional_backtest_param(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _qualified_strategy(settings: Mapping[str, Any]) -> str:
    module_name = str(settings.get("strategy_module", "")).strip()
    class_name = str(settings.get("strategy_class", "")).strip()
    return f"{module_name}.{class_name}" if module_name and class_name else ""


def _number(value: Any, label: str, errors: list[str]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{label} precisa ser numérico.")
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "sim"}


def _resolve_path(value: str | Path, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def _path_exists(value: str | Path) -> bool:
    return Path(value).is_file()


def _absolute_artifact(path: Path | None, project_root: str | Path) -> Path | None:
    if path is None:
        return None
    return _resolve_path(path, Path(project_root))
