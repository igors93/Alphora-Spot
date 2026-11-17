from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from governor.governor_helpers import _enum_value
from operational_module_modes import operational_module_mode_snapshot


def runtime_effective_config_payload(
    config,
    *,
    operational_mode: str,
    symbols: list[str],
    strategy,
    governor_events_path,
) -> dict[str, Any]:
    """The effective runtime settings recorded once when the Governor starts."""

    return {
        "execution_mode": _enum_value(config.execution_mode),
        "environment": _enum_value(config.environment),
        "operational_mode": operational_mode,
        "symbols": symbols,
        "global_strategy": strategy.global_strategy_name,
        "strategy_runtime_mode": strategy.runtime_mode,
        "strategy_state_mode": strategy.state_mode,
        "economic_gate_enabled": config.economic_gate_enabled,
        "min_net_gain_bps": config.min_net_gain_bps,
        "min_net_reward_risk": config.min_net_reward_risk,
        "max_round_trip_cost_bps": config.max_round_trip_cost_bps,
        "economic_exit_adjustment_enabled": config.economic_exit_adjustment_enabled,
        "max_economic_take_bps": config.max_economic_take_bps,
        "max_economic_take_atr_multiplier": config.max_economic_take_atr_multiplier,
        "operational_module_modes": operational_module_mode_snapshot(config),
        "dynamic_exit_enabled": config.dynamic_exit_enabled,
        "dynamic_sizing_enabled": config.dynamic_sizing_enabled,
        "entry_execution_style": config.entry_execution_style,
        "limit_entry_enabled": config.limit_entry_enabled,
        "market_ws_enabled": config.market_ws_enabled,
        "parallel_heavy_prefetch_enabled": getattr(
            config,
            "governor_parallel_heavy_prefetch_enabled",
            True,
        ),
        "max_concurrent_symbol_snapshots": getattr(
            config,
            "governor_max_concurrent_symbol_snapshots",
            3,
        ),
        "prefetched_snapshot_max_age_seconds": getattr(
            config,
            "governor_prefetched_snapshot_max_age_seconds",
            3.0,
        ),
        "shared_private_cycle_ttl_seconds": getattr(
            config,
            "governor_shared_private_cycle_ttl_seconds",
            2.0,
        ),
        "trend_context_enabled": getattr(config, "trend_context_enabled", True),
        "trend_context_mode": getattr(config, "trend_context_mode", "OBSERVE"),
        "user_data_stream_enabled": config.user_data_stream_enabled,
        "learning_store_enabled": config.learning_store_enabled,
        "adaptive_manager_enabled": config.adaptive_manager_enabled,
        "adaptive_execution_enabled": config.adaptive_execution_enabled,
        "trade_audit_path": str(config.trade_audit_path),
        "trade_journal_path": str(config.trade_journal_path),
        "governor_events_path": str(governor_events_path),
        "runtime_dir": str(config.runtime_dir),
    }


def pre_execution_guard_payload(decision) -> dict[str, Any]:
    return _plain_mapping(decision)


def portfolio_trade_approved_payload(
    *,
    trace: dict[str, str],
    symbol: str,
    action: str,
    reason: str,
    decision,
    trade_plan_id: str | None,
    planned_quantity,
    planned_exit,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **trace,
        "symbol": symbol,
        "action": action,
        "reason": reason,
        "decision": _plain_mapping(decision),
        "trade_plan_id": trade_plan_id,
        "planned_quantity": planned_quantity,
        "planned_stop_price": planned_exit_stop(planned_exit),
        "planned_take_profit_price": planned_exit_take(planned_exit),
        "execution_final": False,
        "execution_will_run": False,
        "requires_pre_execution_guard": True,
        "diagnostics": {
            "approval_scope": "PORTFOLIO_RISK_ONLY",
            "safety_note": (
                "PortfolioRisk aprovou a intencao, mas a execucao final ainda "
                "depende do PreExecutionGuard."
            ),
            **(diagnostics or {}),
        },
    }


def planned_exit_stop(planned_exit: Mapping[str, Any] | None) -> Decimal | None:
    if planned_exit is None:
        return None
    value = planned_exit.get("stop_price")
    if value is None:
        return None
    return Decimal(str(value))


def planned_exit_take(planned_exit: Mapping[str, Any] | None) -> Decimal | None:
    if planned_exit is None:
        return None
    value = planned_exit.get("take_profit_price")
    if value is None:
        return None
    return Decimal(str(value))


def _plain_mapping(value) -> dict[str, Any]:
    if value is None:
        return {}
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    fields = {}
    for name in (
        "allowed",
        "reason",
        "symbol",
        "action",
        "checks",
        "risk_snapshot",
        "portfolio_snapshot",
        "timestamp",
    ):
        if hasattr(value, name):
            fields[name] = _plain_value(getattr(value, name))
    return fields


def _plain_value(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if isinstance(value, set):
        return [_plain_value(item) for item in value]
    return value
