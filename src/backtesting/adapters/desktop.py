from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backtesting.domain.requests import (
    BacktestDataRequest,
    BacktestExecutionRequest,
    BacktestOutputRequest,
    BacktestPolicyRequest,
    BacktestPortfolioRequest,
    BacktestRequest,
    BacktestStrategyRequest,
)
from backtesting.infrastructure.manifest_writer import write_json_atomic
from domain.symbol_rules import format_decimal
from futures.exchange_rules import extract_symbol_rules


@dataclass(frozen=True)
class BacktestArtifactPaths:
    manifest_json: Path | None = None
    summary_json: Path | None = None
    trades_csv: Path | None = None
    decisions_jsonl: Path | None = None
    rejected_jsonl: Path | None = None
    equity_csv: Path | None = None


def build_backtest_command(
    *,
    request_path: str | Path,
    python_executable: str | None = None,
) -> list[str]:
    return [
        python_executable or sys.executable,
        "-m",
        "backtesting.adapters.cli",
        "run",
        "--request",
        str(request_path),
    ]


def build_download_klines_command(
    *,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    output: str | None = None,
    python_executable: str | None = None,
) -> list[str]:
    command = [
        python_executable or sys.executable,
        "-m",
        "research.cli",
        "download-klines",
        "--symbol",
        symbol,
        "--interval",
        interval,
        "--start",
        start,
        "--end",
        end,
    ]
    if output:
        command.extend(["--output", output])
    return command


def build_backtest_request(
    *,
    csv_path: str | None,
    csv_map: str | None,
    csv_by_symbol_timeframe: str | Mapping[str, Mapping[str, str]] | None = None,
    funding_csv_map: str | Mapping[str, str] | None = None,
    symbol: str,
    symbols: str | None,
    strategy_mode: str,
    strategy_module: str,
    strategy_class: str,
    timeframe: str,
    strategy_params: str | Mapping[str, Any] | None,
    global_model_path: str | None,
    symbol_model_map: str | Mapping[str, str] | None,
    use_symbol_refined_models: bool,
    fallback_to_global_model: bool,
    min_long_probability: str | None,
    min_short_probability: str | None,
    min_direction_gap: str | None,
    min_expected_value_bps: str | None,
    initial_balance: str,
    maker_fee_pct: str,
    taker_fee_pct: str,
    slippage_bps: str,
    spread_bps: str,
    funding_enabled: str | bool | None,
    default_funding_rate: str | None,
    position_notional: str | None,
    stop_loss_pct: str | None,
    take_profit_pct: str | None,
    leverage: str | None,
    quantity_step: str,
    min_quantity: str,
    min_notional: str,
    price_tick: str,
    max_open_positions: str | None,
    max_total_exposure: str | None,
    max_long_exposure: str | None,
    max_short_exposure: str | None,
    max_symbol_exposure: str | None,
    max_group_exposure: str | None,
    max_correlated_positions: str | None,
    max_new_positions_per_cycle: str | None,
    allow_untradable_symbols: str | bool | None = None,
    output_dir: str = "reports",
    same_candle_exit_policy: str = "CONSERVATIVE_STOP_FIRST",
    end_of_data_policy: str = "CLOSE_AT_LAST_PRICE",
    evaluation_mode: str = "IN_SAMPLE",
    market_impact_bps_at_full_participation: str = "5",
    max_volume_participation: str = "0.05",
    reliable_volume_participation: str = "0.05",
    bootstrap_samples: int = 1000,
    walk_forward_folds: int = 5,
    data_start_timestamp: str | None = None,
    data_end_timestamp: str | None = None,
    evaluation_start_timestamp: str | None = None,
    evaluation_end_timestamp: str | None = None,
    fail_on_quality_warnings: bool = False,
    purge_candles: int = 0,
    embargo_candles: int = 0,
    min_profitable_walk_forward_folds: int = 3,
    confidence_level: str = "0.95",
    parameter_trials: int = 1,
    min_probabilistic_sharpe: str = "0.95",
    min_deflated_sharpe: str = "0.95",
    max_risk_of_ruin: str = "0.01",
    risk_of_ruin_horizon_trades: int = 1000,
    intrabar_limit_volume_fraction: str = "0.25",
) -> BacktestRequest:
    resolved_symbols = _parse_symbols(symbols or symbol)
    csv_by_symbol = _csv_by_symbol(csv_path=csv_path, csv_map=csv_map, symbols=resolved_symbols)
    csv_by_symbol_timeframe_dict = _multi_timeframe_map(csv_by_symbol_timeframe)
    funding_csv_by_symbol = _key_value_map(funding_csv_map)
    params = _json_object(strategy_params)
    symbol_models = _key_value_map(symbol_model_map)
    return BacktestRequest(
        symbols=tuple(resolved_symbols),
        data=BacktestDataRequest(
            csv_by_symbol=csv_by_symbol,
            timeframe=timeframe,
            csv_by_symbol_timeframe=csv_by_symbol_timeframe_dict,
            funding_csv_by_symbol=funding_csv_by_symbol,
            data_start_timestamp=data_start_timestamp,
            data_end_timestamp=data_end_timestamp,
            evaluation_start_timestamp=evaluation_start_timestamp,
            evaluation_end_timestamp=evaluation_end_timestamp,
            fail_on_quality_warnings=bool(fail_on_quality_warnings),
        ),
        strategy=BacktestStrategyRequest(
            mode=strategy_mode,
            module=strategy_module,
            class_name=strategy_class,
            params=params,
            global_model_path=global_model_path,
            symbol_model_paths=symbol_models,
            use_symbol_refined_models=bool(use_symbol_refined_models),
            fallback_to_global_model=bool(fallback_to_global_model),
            min_long_probability=_optional_float(min_long_probability, 0.62),
            min_short_probability=_optional_float(min_short_probability, 0.62),
            min_direction_gap=_optional_float(min_direction_gap, 0.18),
            min_expected_value_bps=_optional_float(min_expected_value_bps, 3.0),
        ),
        execution=BacktestExecutionRequest(
            maker_fee_pct=maker_fee_pct,
            taker_fee_pct=taker_fee_pct,
            slippage_bps=slippage_bps,
            spread_bps=spread_bps,
            funding_enabled=_as_bool(funding_enabled, default=True),
            default_funding_rate=default_funding_rate or "0",
            quantity_step=quantity_step,
            min_quantity=min_quantity,
            min_notional=min_notional,
            price_tick=price_tick,
            position_notional=position_notional or "60",
            stop_loss_pct=stop_loss_pct or "0.01",
            take_profit_pct=take_profit_pct or "0.02",
            leverage=int(leverage or 1),
            max_volume_participation=max_volume_participation,
            reliable_volume_participation=reliable_volume_participation,
            market_impact_bps_at_full_participation=(market_impact_bps_at_full_participation),
            intrabar_limit_volume_fraction=intrabar_limit_volume_fraction,
        ),
        portfolio=BacktestPortfolioRequest(
            initial_balance=initial_balance,
            max_open_positions=int(max_open_positions or 1),
            max_total_exposure=max_total_exposure or "0",
            max_long_exposure=max_long_exposure or "0",
            max_short_exposure=max_short_exposure or "0",
            max_symbol_exposure=max_symbol_exposure or "0",
            max_group_exposure=max_group_exposure or "0",
            max_correlated_positions=int(max_correlated_positions or 0),
            max_new_positions_per_cycle=int(max_new_positions_per_cycle or 1),
            allow_untradable_symbols=_as_bool(allow_untradable_symbols, default=False),
        ),
        policies=BacktestPolicyRequest(
            same_candle_exit=same_candle_exit_policy,
            end_of_data=end_of_data_policy,
            evaluation_mode=evaluation_mode,
            purge_candles=int(purge_candles),
            embargo_candles=int(embargo_candles),
            bootstrap_samples=int(bootstrap_samples),
            walk_forward_folds=int(walk_forward_folds),
            min_profitable_walk_forward_folds=int(min_profitable_walk_forward_folds),
            confidence_level=confidence_level,
            parameter_trials=int(parameter_trials),
            min_probabilistic_sharpe=min_probabilistic_sharpe,
            min_deflated_sharpe=min_deflated_sharpe,
            max_risk_of_ruin=max_risk_of_ruin,
            risk_of_ruin_horizon_trades=int(risk_of_ruin_horizon_trades),
        ),
        output=BacktestOutputRequest(output_dir=output_dir),
    )


def write_backtest_request(path: str | Path, request: BacktestRequest) -> Path:
    request_path = Path(path)
    write_json_atomic(request_path, asdict(request))
    return request_path


def parse_backtest_artifact_paths(output_lines: list[str]) -> BacktestArtifactPaths:
    from domain.subprocess_protocol import decode_task_event

    manifest_path = None
    for line in output_lines:
        event = decode_task_event(line)
        if (
            event is not None
            and event["type"] == "backtest_manifest"
            and isinstance(event.get("path"), str)
            and event["path"].strip()
        ):
            manifest_path = Path(event["path"])
            break
    if manifest_path is None or not manifest_path.is_file():
        return BacktestArtifactPaths(manifest_json=manifest_path)

    with manifest_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return BacktestArtifactPaths(
        manifest_json=manifest_path,
        summary_json=_optional_path(payload.get("summary_json")),
        trades_csv=_optional_path(payload.get("trades_csv")),
        decisions_jsonl=_optional_path(payload.get("decisions_jsonl")),
        rejected_jsonl=_optional_path(payload.get("rejected_jsonl")),
        equity_csv=_optional_path(payload.get("equity_csv")),
    )


def load_backtest_summary(json_path: str | Path) -> dict[str, Any]:
    path = Path(json_path)
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    metrics = data.get("summary", data.get("metrics", {}))
    readiness = data.get("readiness", {})
    return {
        "total_trades": metrics.get("total_trades", 0),
        "symbols_tested": metrics.get("symbols_tested", data.get("symbols", [])),
        "symbols_with_trades": metrics.get("symbols_with_trades", []),
        "symbols_without_trades": metrics.get("symbols_without_trades", []),
        "rejected_signals": metrics.get(
            "rejected_signals_count", data.get("rejected_signal_count", 0)
        ),
        "gross_pnl": metrics.get("gross_pnl", "0"),
        "net_pnl": metrics.get("net_pnl", "0"),
        "total_fees": metrics.get("total_fees", metrics.get("fees", "0")),
        "total_slippage": metrics.get("total_slippage", metrics.get("slippage", "0")),
        "total_spread": metrics.get("total_spread", metrics.get("spread", "0")),
        "total_funding": metrics.get("total_funding", metrics.get("funding", "0")),
        "win_rate": metrics.get("win_rate", "0"),
        "profit_factor": metrics.get("profit_factor", "0"),
        "expectancy": metrics.get("expectancy", "0"),
        "max_drawdown": metrics.get("max_drawdown", "0"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct", "0"),
        "total_return_pct": metrics.get("total_return_pct", "0"),
        "sharpe_ratio": metrics.get("sharpe_ratio", "0"),
        "sortino_ratio": metrics.get("sortino_ratio", "0"),
        "calmar_ratio": metrics.get("calmar_ratio", "0"),
        "reliability_passed": metrics.get("reliability_passed", False),
        "reliability_gates": metrics.get("reliability_gates", {}),
        "statistical_validation": metrics.get("statistical_validation", {}),
        "max_consecutive_losses": metrics.get("max_consecutive_losses", 0),
        "best_trade": metrics.get("best_trade", "0"),
        "worst_trade": metrics.get("worst_trade", "0"),
        "trades_long": metrics.get("trades_long", 0),
        "trades_short": metrics.get("trades_short", 0),
        "pnl_long": metrics.get("pnl_long", "0"),
        "pnl_short": metrics.get("pnl_short", "0"),
        "operational_verdict": metrics.get("operational_verdict", "UNKNOWN"),
        "best_symbol": metrics.get("best_symbol"),
        "worst_symbol": metrics.get("worst_symbol"),
        "ai_global_signals": metrics.get("ai_global_signals", 0),
        "ai_refined_signals": metrics.get("ai_refined_signals", 0),
        "readiness_status": metrics.get("readiness_status", readiness.get("status", "UNKNOWN")),
        "readiness_reasons": readiness.get("reasons", []),
        "readiness_warnings": readiness.get("warnings", []),
    }


def extract_backtest_rules_from_exchange_info(exchange_info: dict, symbol: str) -> dict[str, str]:
    rules = extract_symbol_rules(exchange_info, symbol.upper())
    return {
        "quantity_step": format_decimal(rules.quantity_step),
        "min_quantity": format_decimal(rules.min_quantity),
        "min_notional": format_decimal(rules.min_notional),
        "price_tick": format_decimal(rules.price_tick),
    }


def _parse_symbols(value: str) -> list[str]:
    symbols = [
        item.upper().strip()
        for item in str(value or "").replace(";", ",").split(",")
        if item.strip()
    ]
    if not symbols:
        raise ValueError("Informe ao menos um simbolo")
    return symbols


def _csv_by_symbol(
    *,
    csv_path: str | None,
    csv_map: str | None,
    symbols: list[str],
) -> dict[str, str]:
    mapped = _key_value_map(csv_map)
    if mapped:
        missing = [symbol for symbol in symbols if symbol not in mapped]
        if missing:
            raise ValueError(
                f"CSV nao informado para os simbolos: {', '.join(missing)}. "
                "Inclua todos os simbolos no csv_map (SIMBOLO=caminho)."
            )
        return {symbol: mapped[symbol] for symbol in symbols}
    if not csv_path:
        raise ValueError("CSV obrigatorio para backtest")
    if len(symbols) == 1:
        return {symbols[0]: str(csv_path)}
    return {
        symbol: str(csv_path).replace("{symbol}", symbol).replace("{SYMBOL}", symbol)
        for symbol in symbols
    }


def _key_value_map(value: str | Mapping[str, Any] | None) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, Mapping):
        return {
            str(key).upper().strip(): str(item).strip()
            for key, item in value.items()
            if str(key).strip() and str(item).strip()
        }
    stripped = str(value).strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError("Mapa JSON precisa ser objeto")
        return _key_value_map(payload)
    result: dict[str, str] = {}
    for item in stripped.split(","):
        if not item.strip():
            continue
        key, mapped_value = item.split("=", 1)
        result[key.upper().strip()] = mapped_value.strip()
    return result


def _multi_timeframe_map(
    value: str | Mapping[str, Mapping[str, str]] | None,
) -> dict[str, dict[str, str]]:
    if not value:
        return {}
    if isinstance(value, Mapping):
        return {
            str(k).upper().strip(): {str(tk).strip(): str(tv).strip() for tk, tv in v.items()}
            for k, v in value.items()
        }
    payload = json.loads(str(value))
    if not isinstance(payload, dict):
        raise ValueError("Mapa multi-timeframe JSON precisa ser objeto")
    return _multi_timeframe_map(payload)


def _json_object(value: str | Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    payload = json.loads(str(value))
    if not isinstance(payload, dict):
        raise ValueError("Objeto JSON esperado")
    return payload


def _optional_float(value: str | None, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def _as_bool(value: str | bool | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "sim"}


def _optional_path(value: Any) -> Path | None:
    if not value:
        return None
    return Path(str(value))
