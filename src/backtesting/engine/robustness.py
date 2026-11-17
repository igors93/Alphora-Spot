from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtesting.domain.config import BacktestConfig
from backtesting.engine.engine import run_backtest
from backtesting.engine.strategy_runtime import BacktestStrategyRuntime
from backtesting.infrastructure.csv_feed import HistoricalDataFeed


def run_robust_backtest(
    config: BacktestConfig,
    *,
    write_reports: bool = False,
) -> dict[str, Any]:
    """Run isolated walk-forward folds plus local parameter perturbations.

    Each fold starts with fresh strategy/portfolio state, receives only a bounded
    warm-up prefix and trades exclusively inside its evaluation window.
    """

    feed = HistoricalDataFeed.from_symbol_configs(
        list(config.symbols),
        start_timestamp=config.data_start_timestamp,
        end_timestamp=config.data_end_timestamp,
    )
    common_times = _common_open_times(feed)
    if len(common_times) < 10:
        raise ValueError("Robust backtest exige ao menos 10 timestamps comuns")
    runtime = BacktestStrategyRuntime(config.strategy, config.symbol_names)
    warmup = max(1, runtime.required_history)
    isolation_cutoff = _model_isolation_cutoff_ms(config)
    first_test_index = max(
        warmup,
        len(common_times) // 2,
        bisect_right(common_times, isolation_cutoff) if isolation_cutoff is not None else 0,
    )
    test_times = common_times[first_test_index:]
    fold_count = min(config.validation_settings.walk_forward_folds, len(test_times))
    if fold_count < 2:
        raise ValueError("Dados insuficientes para ao menos dois folds walk-forward")

    folds = []
    gap = config.validation_settings.purge_candles + config.validation_settings.embargo_candles
    for fold_index in range(fold_count):
        raw_start_offset = (len(test_times) * fold_index) // fold_count
        end_offset = (len(test_times) * (fold_index + 1)) // fold_count - 1
        start_offset = raw_start_offset + gap
        if start_offset > end_offset:
            folds.append(
                {
                    "fold": fold_index + 1,
                    "status": "FAILED",
                    "error": "INSUFFICIENT_FOLD_AFTER_PURGE_AND_EMBARGO",
                }
            )
            continue
        evaluation_start = test_times[start_offset]
        evaluation_end = test_times[end_offset]
        absolute_start = first_test_index + start_offset
        data_start = common_times[max(0, absolute_start - warmup)]
        fold_config = _window_config(
            config,
            data_start=data_start,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            mode="WALK_FORWARD",
            output_suffix=f"fold_{fold_index + 1}",
        )
        try:
            result = run_backtest(fold_config, write_reports=write_reports)
            operational_reliability = _operational_reliability_passed(result.summary)
            folds.append(
                {
                    "fold": fold_index + 1,
                    "status": "OK",
                    "evaluation_start": _iso(evaluation_start),
                    "evaluation_end": _iso(evaluation_end),
                    "trades": result.metrics.total_trades,
                    "net_pnl": str(result.metrics.net_pnl),
                    "expectancy": str(result.metrics.expectancy),
                    "profit_factor": str(result.metrics.profit_factor),
                    "max_drawdown_pct": str(result.metrics.max_drawdown_pct),
                    "reliability_passed": bool(result.summary.get("reliability_passed")),
                    "operational_reliability_passed": operational_reliability,
                }
            )
        except Exception as error:
            folds.append(
                {
                    "fold": fold_index + 1,
                    "status": "FAILED",
                    "evaluation_start": _iso(evaluation_start),
                    "evaluation_end": _iso(evaluation_end),
                    "error": str(error),
                }
            )

    if gap >= len(test_times):
        raise ValueError("Dados insuficientes apos purge e embargo para baseline OOS")
    evaluation_start = test_times[gap]
    evaluation_end = test_times[-1]
    data_start = common_times[max(0, first_test_index + gap - warmup)]
    baseline_config = _window_config(
        config,
        data_start=data_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        mode="OUT_OF_SAMPLE",
        output_suffix="oos_baseline",
    )
    baseline = run_backtest(baseline_config, write_reports=write_reports)
    perturbations = _parameter_perturbations(baseline_config)
    sensitivity = []
    for name, variant in perturbations:
        try:
            result = run_backtest(variant, write_reports=False)
            operational_reliability = _operational_reliability_passed(result.summary)
            sensitivity.append(
                {
                    "variant": name,
                    "status": "OK",
                    "trades": result.metrics.total_trades,
                    "net_pnl": str(result.metrics.net_pnl),
                    "expectancy": str(result.metrics.expectancy),
                    "max_drawdown_pct": str(result.metrics.max_drawdown_pct),
                    "operational_reliability_passed": operational_reliability,
                }
            )
        except Exception as error:
            sensitivity.append({"variant": name, "status": "FAILED", "error": str(error)})

    successful_folds = [item for item in folds if item["status"] == "OK"]
    profitable_folds = [item for item in successful_folds if Decimal(str(item["net_pnl"])) > 0]
    successful_variants = [item for item in sensitivity if item["status"] == "OK"]
    profitable_variants = [
        item
        for item in successful_variants
        if Decimal(str(item["net_pnl"])) > 0 and bool(item.get("operational_reliability_passed"))
    ]
    fold_passed = (
        len(successful_folds) == fold_count
        and all(bool(item.get("operational_reliability_passed")) for item in successful_folds)
        and len(profitable_folds)
        >= min(
            config.validation_settings.min_profitable_walk_forward_folds,
            fold_count,
        )
    )
    parameter_stability_passed = (
        bool(sensitivity)
        and len(successful_variants) == len(sensitivity)
        and Decimal(str(len(profitable_variants))) / Decimal(str(len(sensitivity)))
        >= Decimal("0.70")
    )
    passed = (
        fold_passed
        and parameter_stability_passed
        and bool(baseline.summary.get("reliability_passed"))
    )
    return {
        "method": "BOUNDED_WARMUP_ISOLATED_WALK_FORWARD_V2",
        "baseline_summary": baseline.summary,
        "folds": folds,
        "folds_passed": fold_passed,
        "profitable_folds": len(profitable_folds),
        "parameter_sensitivity": sensitivity,
        "parameter_stability_passed": parameter_stability_passed,
        "profitable_parameter_variants": len(profitable_variants),
        "passed": passed,
        "operational_verdict": "ROBUST_BACKTEST_OK" if passed else "ROBUST_BACKTEST_REJECTED",
    }


def _common_open_times(feed: HistoricalDataFeed) -> list[int]:
    sets = [
        {candle.open_time_ms for candle in candles} for candles in feed.candles_by_symbol.values()
    ]
    common = set.intersection(*sets) if sets else set()
    return sorted(common)


def _window_config(
    config: BacktestConfig,
    *,
    data_start: int,
    evaluation_start: int,
    evaluation_end: int,
    mode: str,
    output_suffix: str,
) -> BacktestConfig:
    return replace(
        config,
        data_start_timestamp=_iso(data_start),
        data_end_timestamp=_iso(evaluation_end),
        evaluation_start_timestamp=_iso(evaluation_start),
        evaluation_end_timestamp=_iso(evaluation_end),
        output_dir=str(Path(config.output_dir) / "robustness" / output_suffix),
        validation_settings=replace(
            config.validation_settings,
            evaluation_mode=mode,
            parameter_trials=max(
                config.validation_settings.parameter_trials,
                1 + len(_parameter_perturbations(config)),
            ),
        ),
    )


def _parameter_perturbations(config: BacktestConfig) -> list[tuple[str, BacktestConfig]]:
    variants: list[tuple[str, BacktestConfig]] = []
    for field_name in ("stop_loss_pct", "take_profit_pct", "position_notional_usdt"):
        for factor in (Decimal("0.90"), Decimal("1.10")):
            symbols = tuple(
                replace(
                    symbol,
                    **{field_name: getattr(symbol, field_name) * factor},
                )
                for symbol in config.symbols
            )
            variants.append((f"symbols.{field_name}_x{factor}", replace(config, symbols=symbols)))
    for factor in (Decimal("0.90"), Decimal("1.10")):
        symbols = tuple(
            replace(
                symbol,
                stop_loss_pct=symbol.stop_loss_pct * factor,
                take_profit_pct=symbol.take_profit_pct * factor,
            )
            for symbol in config.symbols
        )
        variants.append((f"risk_exit_x{factor}", replace(config, symbols=symbols)))
    for key, value in _tunable_strategy_params(config)[:6]:
        for perturbation_factor in (0.90, 1.10):
            adjusted: int | float | str
            if isinstance(value, int):
                adjusted = int(round(value * perturbation_factor))
            else:
                adjusted = str(Decimal(str(value)) * Decimal(str(perturbation_factor)))
            params = {**config.strategy.strategy_params, key: adjusted}
            variants.append(
                (
                    f"strategy.{key}_x{perturbation_factor:.2f}",
                    replace(config, strategy=replace(config.strategy, strategy_params=params)),
                )
            )
    return variants


_PREFERRED_TUNABLE_PARAMETERS = (
    "volume_zscore_threshold",
    "max_volume_zscore",
    "min_rejection_wick_fraction",
    "min_sweep_depth_atr",
    "max_sweep_depth_atr",
    "stop_buffer_atr",
    "reward_risk_ratio",
    "adx_threshold",
    "volume_multiplier",
    "stop_atr_multiplier",
)


def _tunable_strategy_params(config: BacktestConfig) -> list[tuple[str, int | float | str]]:
    params = dict(config.strategy.strategy_params)
    try:
        runtime = BacktestStrategyRuntime(config.strategy, config.symbol_names)
        describe = getattr(runtime.strategy, "describe_parameters", None)
        descriptions = describe() if callable(describe) else {}
    except Exception:
        descriptions = {}

    tunable: list[tuple[str, int | float | str]] = []
    for key in _PREFERRED_TUNABLE_PARAMETERS:
        value: Any = params.get(key)
        if value is None and isinstance(descriptions, dict):
            description = descriptions.get(key, {})
            if isinstance(description, dict):
                value = description.get("default")
        if isinstance(value, bool) or value is None:
            continue
        try:
            Decimal(str(value))
        except Exception:
            continue
        tunable.append((key, value))
    return tunable


def _operational_reliability_passed(summary: dict[str, Any]) -> bool:
    gates = dict(summary.get("reliability_gates", {}) or {})
    gates.pop("statistical_validation_passed", None)
    return bool(gates) and all(bool(value) for value in gates.values())


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _model_isolation_cutoff_ms(config: BacktestConfig) -> int | None:
    paths = [config.strategy.global_model_path, *config.strategy.symbol_model_paths.values()]
    cutoffs = []
    for raw_path in paths:
        if not raw_path or not Path(raw_path).exists():
            continue
        with Path(raw_path).open("r", encoding="utf-8") as file:
            model = json.load(file)
        test = model.get("test_window")
        validation = model.get("validation_window")
        training = model.get("training_window")
        value = ""
        if isinstance(test, dict):
            value = str(test.get("end", ""))
        if not value:
            value = str(model.get("data_end", ""))
        if isinstance(validation, dict):
            value = value or str(validation.get("end", ""))
        if not value and isinstance(training, dict):
            value = str(training.get("end", ""))
        if not value:
            value = str(model.get("data_end", ""))
        if not value:
            continue
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        cutoffs.append(int(parsed.timestamp() * 1000))
    return max(cutoffs) if cutoffs else None
