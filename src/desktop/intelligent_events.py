from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.settings_store import normalize_environment
from utils.jsonl import parse_json_object_line, tail_lines

INTELLIGENT_EVENT_TYPES = {
    "intelligent_order_activation_result",
    "intelligent_exit_plan_shadow_created",
    "intelligent_exit_activation_evaluated",
    "intelligent_exit_activation_result",
    "intelligent_execution_plan_evaluated",
    "trade_waiting_by_execution_planner",
    "adaptive_management_shadow_evaluated",
    "adaptive_adjustment_plan_shadow_created",
    "adaptive_adjustment_activation_evaluated",
    "adaptive_adjustment_execution_result",
}

EVENT_TITLES = {
    "intelligent_order_activation_result": "Ordem inteligente",
    "intelligent_exit_plan_shadow_created": "Stop/Take inteligente shadow",
    "intelligent_exit_activation_evaluated": "Ativação de stop/take",
    "intelligent_exit_activation_result": "Bridge de stop/take",
    "intelligent_execution_plan_evaluated": "Execution planner",
    "trade_waiting_by_execution_planner": "Execução aguardando",
    "adaptive_management_shadow_evaluated": "Gerenciamento de posição",
    "adaptive_adjustment_plan_shadow_created": "Plano adaptativo",
    "adaptive_adjustment_activation_evaluated": "Ativação adaptativa",
    "adaptive_adjustment_execution_result": "Execução adaptativa",
}


@dataclass(frozen=True)
class IntelligentEventSummary:
    timestamp: str
    event_type: str
    title: str
    symbol: str
    status: str
    action: str
    reason: str
    severity: str
    details: dict[str, Any]


def build_intelligent_event_paths(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
) -> list[Path]:
    environment = normalize_environment(settings).lower()
    paths = [
        Path("data/governor_events.jsonl"),
        Path("data/trades.jsonl"),
    ]

    for profile in symbol_profiles:
        if not bool(profile.get("enabled", True)):
            continue

        symbol = str(profile.get("symbol", "")).lower().strip()
        if not symbol:
            continue

        paths.append(Path("data") / f"trades_{symbol}_{environment}.jsonl")
        paths.append(Path("data") / f"paper_trades_{symbol}_{environment}.jsonl")

    return _unique_paths(paths)


def load_intelligent_events_from_paths(
    paths: list[Path],
    *,
    limit: int = 30,
    tail_limit: int = 500,
) -> list[IntelligentEventSummary]:
    events: list[IntelligentEventSummary] = []

    for path in paths:
        if not path.exists():
            continue

        for line in tail_lines(path, tail_limit):
            event = parse_json_object_line(line)
            if event is None:
                continue

            event_type = str(event.get("event_type", ""))
            if event_type not in INTELLIGENT_EVENT_TYPES:
                continue

            events.append(_summarize_event(event))

    return list(reversed(events[-limit:]))


def format_intelligent_events_for_display(
    events: list[IntelligentEventSummary],
) -> str:
    if not events:
        return (
            "Nenhuma decisão inteligente recente encontrada.\n\n"
            "Quando o Alphora rodar, este painel irá destacar eventos como "
            "execution planner, stop/take inteligente e gerenciamento adaptativo."
        )

    lines = ["Decisões inteligentes recentes:", ""]
    for event in events:
        symbol = event.symbol or "-"
        lines.extend(
            [
                f"[{event.severity}] {event.title} | {symbol}",
                f"  tempo: {event.timestamp or '-'}",
                f"  tipo: {event.event_type}",
                f"  status: {event.status or '-'}",
                f"  ação: {event.action or '-'}",
                f"  motivo: {event.reason or '-'}",
            ]
        )

        detail_text = _format_details(event.details)
        if detail_text:
            lines.append(f"  detalhes: {detail_text}")

        lines.append("")

    return "\n".join(lines).rstrip()


def _summarize_event(event: dict[str, Any]) -> IntelligentEventSummary:
    event_type = str(event.get("event_type", ""))
    data = event.get("data", {})
    if not isinstance(data, dict):
        data = {}

    status = _first_text(
        data,
        "status",
        "final_status",
        "state",
    )
    action = _first_text(
        data,
        "activation_action",
        "planner_action",
        "action",
        "execution_style",
    )
    reason = _first_text(
        data,
        "reason",
        "final_reason",
        "planned_quantity_reason",
        "planned_exit_reason",
    )
    symbol = _first_text(data, "symbol")
    severity = _severity(event_type=event_type, status=status, data=data)

    return IntelligentEventSummary(
        timestamp=str(event.get("timestamp", "")),
        event_type=event_type,
        title=EVENT_TITLES.get(event_type, event_type),
        symbol=symbol,
        status=status,
        action=action,
        reason=reason,
        severity=severity,
        details=_details_for_event(event_type=event_type, data=data),
    )


def _details_for_event(*, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    detail_keys = {
        "intelligent_order_activation_result": (
            "quantity_applied",
            "exit_applied",
            "planned_quantity_passed",
            "paper_only",
            "testnet_only",
        ),
        "intelligent_exit_plan_shadow_created": (
            "recommendation_mode",
            "protection_quality",
            "adaptive_changed",
            "adaptive_risk_reward",
        ),
        "intelligent_exit_activation_evaluated": (
            "exit_source",
            "used_adaptive_exit",
            "final_stop_price",
            "final_take_profit_price",
        ),
        "intelligent_exit_activation_result": (
            "exit_source",
            "planned_stop_price_passed",
            "planned_take_profit_price_passed",
        ),
        "intelligent_execution_plan_evaluated": (
            "execution_style",
            "should_execute",
            "price_drift_bps",
            "market_data_stale",
        ),
        "trade_waiting_by_execution_planner": (
            "execution_style",
            "price_drift_bps",
            "market_data_age_seconds",
            "market_data_stale",
        ),
        "adaptive_management_shadow_evaluated": (
            "management_mode",
            "trailing_stop_price",
            "suggested_reduction_fraction",
            "should_exit_position",
        ),
        "adaptive_adjustment_plan_shadow_created": (
            "management_mode",
            "target_stop_price",
            "target_reduction_fraction",
            "requires_manual_review",
            "execution_enabled",
        ),
        "adaptive_adjustment_activation_evaluated": (
            "plan_action",
            "execution_enabled",
            "applied",
            "paper_only",
        ),
        "adaptive_adjustment_execution_result": (
            "status",
            "plan_action",
            "execution_enabled",
        ),
    }

    return {
        key: data.get(key) for key in detail_keys.get(event_type, ()) if data.get(key) is not None
    }


def _severity(*, event_type: str, status: str, data: dict[str, Any]) -> str:
    normalized = status.upper()

    if event_type == "trade_waiting_by_execution_planner":
        return "WAIT"

    if "FAILED" in normalized or "ERROR" in normalized:
        return "ERRO"

    if "BLOCK" in normalized or "REJECT" in normalized:
        return "BLOQUEADO"

    if "WAIT" in normalized or "STALE" in normalized:
        return "AGUARDANDO"

    if data.get("requires_manual_review") is True:
        return "REVISÃO"

    if data.get("applied") is True or normalized == "APPLIED":
        return "APLICADO"

    if "SHADOW" in normalized:
        return "SHADOW"

    return "INFO"


def _format_details(details: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in details.items())


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return str(value)
    return ""


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique
