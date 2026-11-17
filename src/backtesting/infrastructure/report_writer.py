import csv
import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from backtesting.domain.artifacts import MANIFEST_SCHEMA_VERSION, BacktestManifest
from backtesting.domain.config import (
    BacktestArtifactPaths,
    BacktestConfig,
    BacktestRunResult,
)
from backtesting.infrastructure.manifest_writer import write_manifest
from utils.audit_logger import get_run_id
from utils.events import EventChannel, observe_event


def write_backtest_report(result: BacktestRunResult) -> BacktestArtifactPaths:
    output_dir = Path(result.config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    symbols = "_".join(result.config.symbol_names)
    base = output_dir / f"backtest_{symbols}_{timestamp}"
    paths = BacktestArtifactPaths(
        summary_json=base.with_suffix(".json"),
        trades_csv=output_dir / f"{base.name}_trades.csv",
        decisions_jsonl=output_dir / f"{base.name}_decisions.jsonl",
        rejected_jsonl=output_dir / f"{base.name}_rejected_signals.jsonl",
        equity_csv=output_dir / f"{base.name}_equity.csv",
        manifest_json=output_dir / f"{base.name}_manifest.json",
    )

    _write_summary_json(paths.summary_json, result, generated_at=generated_at)
    _write_trades_csv(paths.trades_csv, result)
    _write_jsonl(paths.decisions_jsonl, result.decisions)
    _write_jsonl(paths.rejected_jsonl, result.rejected_signals)
    if paths.equity_csv is not None:
        _write_equity_csv(paths.equity_csv, result)
    manifest_path = paths.manifest_json
    if manifest_path is None:
        raise RuntimeError("Manifest path nao foi construido para o backtest")
    reproducibility = build_reproducibility_metadata(result.config)
    write_manifest(
        manifest_path,
        BacktestManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            run_id=result.config.run_id or get_run_id(),
            status="SUCCEEDED",
            started_at=result.started_at or generated_at.isoformat(),
            finished_at=result.finished_at or datetime.now(timezone.utc).isoformat(),
            request_path=None,
            summary_json=str(paths.summary_json),
            trades_csv=str(paths.trades_csv),
            decisions_jsonl=str(paths.decisions_jsonl),
            rejected_jsonl=str(paths.rejected_jsonl),
            diagnostics_json=None,
            error=None,
            equity_csv=str(paths.equity_csv) if paths.equity_csv else None,
            request_hash=None,
            config_hash=_config_hash(result),
            git_commit=reproducibility["git_commit"],
            git_dirty=bool(reproducibility["git_dirty"]),
            source_tree_sha256=reproducibility["source_tree_sha256"],
            config=_json_safe(asdict(result.config)),
            input_files=reproducibility["input_files"],
            model_files=reproducibility["model_files"],
            output_files=_output_file_manifest(paths),
            python_version=reproducibility["python_version"],
            platform=reproducibility["platform"],
            dependencies=reproducibility["dependencies"],
            random_seed=result.config.validation_settings.random_seed,
        ),
    )
    result.paths = paths
    observe_event(
        "report_written",
        component=__name__,
        category="backtesting",
        channel=EventChannel.AUDIT,
        run_id=result.config.run_id or get_run_id(),
        symbols=result.config.symbol_names,
        summary_json=paths.summary_json,
        trades_csv=paths.trades_csv,
        decisions_jsonl=paths.decisions_jsonl,
        rejected_jsonl=paths.rejected_jsonl,
        manifest_json=paths.manifest_json,
    )
    return paths


def _write_summary_json(
    path: Path,
    result: BacktestRunResult,
    *,
    generated_at: datetime,
) -> None:
    payload = {
        "run_id": result.config.run_id or get_run_id(),
        "generated_at": generated_at.isoformat(),
        "symbols": result.config.symbol_names,
        "strategy": {
            "mode": result.config.strategy.normalized_mode,
            "module": result.config.strategy.load_target()[0],
            "class": result.config.strategy.load_target()[1],
            "params": result.config.strategy.load_target()[2],
        },
        "config": {
            "initial_balance": result.config.initial_balance,
            "output_dir": result.config.output_dir,
            "portfolio_settings": result.config.portfolio_settings,
            "cost_model": result.config.cost_model,
            "signal_timing_policy": result.config.signal_timing_policy,
            "market_entry_policy": result.config.market_entry_policy,
            "limit_activation_policy": result.config.limit_activation_policy,
            "same_candle_exit_policy": result.config.same_candle_exit_policy,
            "end_of_data_policy": result.config.end_of_data_policy,
            "rank_cycle_candidates": result.config.rank_cycle_candidates,
            "allow_untradable_symbols": result.config.allow_untradable_symbols,
            "operational_module_modes": result.config.operational_module_modes,
            "smart_sizing_settings": result.config.smart_sizing_settings.to_dict(),
            "smart_execution_settings": result.config.smart_execution_settings.to_dict(),
            "limit_entry_timeout_candles": result.config.limit_entry_timeout_candles,
            "timeframes": {item.symbol: item.timeframe for item in result.config.symbols},
            "symbols": [asdict(item) for item in result.config.symbols],
            "validation_settings": asdict(result.config.validation_settings),
            "fill_settings": asdict(result.config.fill_settings),
            "data_window": {
                "start": result.config.data_start_timestamp,
                "end": result.config.data_end_timestamp,
                "evaluation_start": result.config.evaluation_start_timestamp,
                "evaluation_end": result.config.evaluation_end_timestamp,
                "fail_on_quality_warnings": (result.config.fail_on_data_quality_warnings),
            },
            "funding_sources": result.config.funding_source_paths_by_symbol,
            "trend_context": {
                "enabled": result.config.trend_context_enabled,
                "mode": result.config.trend_context_mode,
                "fast_ma_period": result.config.trend_fast_ma_period,
                "slow_ma_period": result.config.trend_slow_ma_period,
                "slope_lookback": result.config.trend_slope_lookback,
                "volatility_lookback": result.config.trend_volatility_lookback,
                "high_volatility_threshold_pct": (
                    result.config.trend_high_volatility_threshold_pct
                ),
                "chop_threshold": result.config.trend_chop_threshold,
            },
        },
        "data_feed": result.data_diagnostics,
        "summary": result.summary,
        "metrics": result.summary,
        "readiness": {
            "status": result.summary.get("readiness_status", result.readiness.status.value),
            "passed": bool(result.summary.get("readiness_passed", False)),
            "economic_passed": result.readiness.passed,
            "reasons": result.summary.get("readiness_reasons", result.readiness.reasons),
            "warnings": result.summary.get(
                "readiness_warnings",
                result.readiness.warnings,
            ),
            "metrics": result.readiness.metrics,
        },
        "rejected_signal_count": len(result.rejected_signals),
        "rejected_signals_preview": result.rejected_signals[:50],
        "artifact_files": {
            "summary_json": str(path),
            "trades_csv": str(path.with_name(f"{path.stem}_trades.csv")),
            "decisions_jsonl": str(path.with_name(f"{path.stem}_decisions.jsonl")),
            "rejected_jsonl": str(path.with_name(f"{path.stem}_rejected_signals.jsonl")),
            "manifest_json": str(path.with_name(f"{path.stem}_manifest.json")),
            "equity_csv": str(path.with_name(f"{path.stem}_equity.csv")),
        },
    }
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=_json_default)
        file.write("\n")


def _write_trades_csv(path: Path, result: BacktestRunResult) -> None:
    fieldnames = [
        "symbol",
        "direction",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "quantity",
        "notional",
        "gross_pnl",
        "fees",
        "slippage_cost",
        "spread_cost",
        "funding_cost",
        "net_pnl",
        "return_pct",
        "exit_reason",
        "strategy_reason_entry",
        "strategy_reason_exit",
        "dynamic_exit_used",
        "exit_source",
        "recommended_stop_price",
        "recommended_take_profit_price",
        "actual_stop_price_used",
        "actual_take_profit_price_used",
        "reward_risk_liquido",
        "raw_reward_risk",
        "cost_to_reward_ratio",
        "risk_pct",
        "reward_pct",
        "planned_exit_rejection_reason",
        "smart_sizing_mode",
        "smart_sizing_used",
        "smart_sizing_affected_result",
        "base_notional_usdt",
        "final_notional_usdt",
        "sizing_multiplier",
        "sizing_reason",
        "sizing_diagnostics",
        "smart_execution_mode",
        "smart_execution_used",
        "smart_execution_affected_result",
        "entry_order_type",
        "limit_price",
        "limit_fill_status",
        "limit_wait_candles",
        "fallback_to_market_used",
        "smart_execution_reason",
        "smart_execution_diagnostics",
        "entry_maker",
        "exit_maker",
        "requested_quantity",
        "filled_quantity",
        "entry_volume_participation",
        "exit_volume_participation",
        "market_impact_bps",
        "liquidation_price",
        "liquidation_fee",
        "was_liquidated",
        "effective_entry_price",
        "effective_exit_price",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for trade in result.trades:
            row = asdict(trade)
            row["direction"] = trade.direction.value
            writer.writerow({key: _csv_value(row[key]) for key in fieldnames})


def _write_equity_csv(path: Path, result: BacktestRunResult) -> None:
    fieldnames = [
        "timestamp",
        "balance",
        "realized_pnl",
        "unrealized_pnl",
        "estimated_open_costs",
        "equity",
        "gross_exposure",
        "reserved_margin",
        "open_positions",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for point in result.equity_curve:
            writer.writerow(point.as_dict())


def _write_jsonl(path: Path, items: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for item in items:
            json.dump(item, file, ensure_ascii=False, default=_json_default)
            file.write("\n")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__") and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _csv_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _config_hash(result: BacktestRunResult) -> str:
    config_payload = asdict(result.config)
    config_payload.pop("run_id", None)
    payload = json.dumps(
        _json_safe(config_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=_project_root(),
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_dirty() -> bool:
    try:
        output = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            cwd=_project_root(),
        ).stdout
        return bool(output.strip())
    except (OSError, subprocess.CalledProcessError):
        return True


def _source_tree_sha256() -> str | None:
    root = _project_root()
    try:
        output = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
            cwd=root,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    paths = sorted(
        path
        for path in output.splitlines()
        if path == "pyproject.toml"
        or (path.startswith("src/") and Path(path).suffix in {".py", ".qml", ".toml"})
    )
    digest = hashlib.sha256()
    for relative_path in paths:
        path = root / relative_path
        if not path.is_file():
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _file_record(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _input_file_manifest(config: BacktestConfig) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for symbol in config.symbols:
        primary = Path(symbol.csv_path)
        files[f"{symbol.symbol}:{symbol.timeframe}"] = _file_record(primary)
        for timeframe, raw_path in (symbol.csv_paths_by_timeframe or {}).items():
            files[f"{symbol.symbol}:{timeframe}"] = _file_record(Path(raw_path))
    for funding_symbol, raw_path in config.funding_source_paths_by_symbol.items():
        files[f"{funding_symbol}:FUNDING"] = _file_record(Path(raw_path))
    return files


def _model_file_manifest(config: BacktestConfig) -> dict[str, Any]:
    files: dict[str, Any] = {}
    candidates = {"GLOBAL": config.strategy.global_model_path}
    candidates.update(
        {f"SYMBOL:{symbol}": path for symbol, path in config.strategy.symbol_model_paths.items()}
    )
    for name, raw_path in candidates.items():
        if raw_path and Path(raw_path).exists():
            files[name] = _file_record(Path(raw_path))
    return files


def _output_file_manifest(paths: BacktestArtifactPaths) -> dict[str, Any]:
    candidates = {
        "summary_json": paths.summary_json,
        "trades_csv": paths.trades_csv,
        "decisions_jsonl": paths.decisions_jsonl,
        "rejected_jsonl": paths.rejected_jsonl,
        "equity_csv": paths.equity_csv,
    }
    return {
        name: _file_record(path)
        for name, path in candidates.items()
        if path is not None and path.exists()
    }


def _dependency_versions() -> dict[str, str]:
    packages = (
        "alphora",
        "requests",
        "python-dotenv",
        "websocket-client",
        "logprivacy",
        "pytest",
    )
    result = {}
    for package in packages:
        try:
            result[package] = importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:
            result[package] = "NOT_INSTALLED"
    return result


def build_reproducibility_metadata(config: BacktestConfig) -> dict[str, Any]:
    return {
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
        "source_tree_sha256": _source_tree_sha256(),
        "python_version": sys.version,
        "platform": platform.platform(),
        "dependencies": _dependency_versions(),
        "input_files": _input_file_manifest(config),
        "model_files": _model_file_manifest(config),
        "random_seed": config.validation_settings.random_seed,
    }
