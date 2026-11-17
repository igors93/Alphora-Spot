from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.jsonl import parse_json_object_line, tail_lines

USER_DATA_CACHE_HEALTH_EVENT_TYPES = {
    "user_data_stream_cache_stale_guard",
    "user_data_stream_cache_usage_shadow",
    "user_data_stream_cache_assisted_plan_shadow",
    "user_data_stream_cache_shadow_audit",
    "user_data_stream_cache_reconciliation",
    "user_data_stream_cache_position_used",
    "user_data_stream_cache_account_used",
    "user_data_stream_cache_open_orders_used",
    "user_data_stream_cache_runtime_metrics",
    "user_data_stream_event_shadow",
    "user_data_stream_cache_shadow_updated",
}


EVENT_TITLES = {
    "user_data_stream_cache_stale_guard": "Stale guard",
    "user_data_stream_cache_usage_shadow": "Elegibilidade do cache",
    "user_data_stream_cache_assisted_plan_shadow": "Plano cache-assisted",
    "user_data_stream_cache_shadow_audit": "Auditoria cache vs REST",
    "user_data_stream_cache_reconciliation": "Reconciliação REST",
    "user_data_stream_cache_position_used": "Cache usado: posição",
    "user_data_stream_cache_account_used": "Cache usado: account_info",
    "user_data_stream_cache_open_orders_used": "Cache usado: open_orders",
    "user_data_stream_cache_runtime_metrics": "Métrica runtime cache/REST",
    "user_data_stream_event_shadow": "Evento privado observado",
    "user_data_stream_cache_shadow_updated": "Cache privado atualizado",
}


@dataclass(frozen=True)
class UserDataCacheHealthEvent:
    timestamp: str
    event_type: str
    title: str
    symbol: str
    status: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UserDataCacheHealthSummary:
    events: list[UserDataCacheHealthEvent]
    latest_by_type: dict[str, UserDataCacheHealthEvent]
    usage_counts: dict[str, int]
    runtime_savings_counts: dict[str, int]
    paths_checked: list[str]


def load_user_data_cache_health_from_paths(
    paths: list[Path],
    *,
    limit: int = 20,
    tail_limit: int = 800,
) -> UserDataCacheHealthSummary:
    events: list[UserDataCacheHealthEvent] = []
    paths_checked = [path.as_posix() for path in paths]

    for path in paths:
        if not path.exists():
            continue

        for line in tail_lines(path, tail_limit):
            payload = parse_json_object_line(line)
            if payload is None:
                continue

            event_type = str(payload.get("event_type", ""))
            if event_type not in USER_DATA_CACHE_HEALTH_EVENT_TYPES:
                continue

            events.append(_summarize_event(payload))

    latest_by_type: dict[str, UserDataCacheHealthEvent] = {}
    for event in events:
        latest_by_type[event.event_type] = event

    recent_events = list(reversed(events[-limit:]))

    return UserDataCacheHealthSummary(
        events=recent_events,
        latest_by_type=latest_by_type,
        usage_counts={
            "position": sum(
                1 for event in events if event.event_type == "user_data_stream_cache_position_used"
            ),
            "account_info": sum(
                1 for event in events if event.event_type == "user_data_stream_cache_account_used"
            ),
            "open_orders": sum(
                1
                for event in events
                if event.event_type == "user_data_stream_cache_open_orders_used"
            ),
        },
        runtime_savings_counts=_runtime_savings_counts(events),
        paths_checked=paths_checked,
    )


def format_user_data_cache_health_for_display(
    summary: UserDataCacheHealthSummary,
) -> str:
    if not summary.events:
        return (
            "Nenhum evento de saúde do User Data Stream/cache encontrado.\n\n"
            "Quando o Governor rodar com as fases 10I, este painel mostrará:\n"
            "- conexão do User Data Stream\n"
            "- stale guard\n"
            "- elegibilidade do cache\n"
            "- auditoria cache vs REST\n"
            "- reconciliação REST\n"
            "- uso de cache para posição, account_info e open_orders\n\n"
            "Caminhos verificados:\n" + "\n".join(f"- {path}" for path in summary.paths_checked)
        )

    lines = [
        "Saúde User Data Stream / Cache privado",
        "",
        "Resumo:",
        _latest_line(
            summary,
            "user_data_stream_cache_stale_guard",
            "Stale guard",
        ),
        _latest_line(
            summary,
            "user_data_stream_cache_usage_shadow",
            "Elegibilidade",
        ),
        _latest_line(
            summary,
            "user_data_stream_cache_assisted_plan_shadow",
            "Plano cache-assisted",
        ),
        _latest_line(
            summary,
            "user_data_stream_cache_shadow_audit",
            "Auditoria cache vs REST",
        ),
        _latest_line(
            summary,
            "user_data_stream_cache_reconciliation",
            "Reconciliação REST",
        ),
        _latest_line(
            summary,
            "user_data_stream_cache_runtime_metrics",
            "Métrica runtime",
        ),
        "",
        "Economia REST runtime no recorte lido:",
        f"- position REST saved: {summary.runtime_savings_counts.get('position_rest_saved', 0)}",
        "- account_info REST saved: "
        f"{summary.runtime_savings_counts.get('account_info_rest_saved', 0)}",
        "- open_orders REST saved: "
        f"{summary.runtime_savings_counts.get('open_orders_rest_saved', 0)}",
        f"- total rest_saved_count: {summary.runtime_savings_counts.get('rest_saved_count', 0)}",
        "- total fallback_rest_count: "
        f"{summary.runtime_savings_counts.get('fallback_rest_count', 0)}",
        "",
        "Uso de cache no recorte lido:",
        f"- position: {summary.usage_counts.get('position', 0)}",
        f"- account_info: {summary.usage_counts.get('account_info', 0)}",
        f"- open_orders: {summary.usage_counts.get('open_orders', 0)}",
        "",
        "Eventos recentes:",
        "",
    ]

    for event in summary.events:
        symbol = event.symbol or "-"
        lines.extend(
            [
                f"[{event.title}] {symbol}",
                f"  tempo: {event.timestamp or '-'}",
                f"  tipo: {event.event_type}",
                f"  status: {event.status or '-'}",
                f"  motivo: {event.reason or '-'}",
            ]
        )

        details = _format_details(event.details)
        if details:
            lines.append(f"  detalhes: {details}")

        lines.append("")

    return "\n".join(lines).rstrip()


def _latest_line(
    summary: UserDataCacheHealthSummary,
    event_type: str,
    label: str,
) -> str:
    event = summary.latest_by_type.get(event_type)
    if event is None:
        return f"- {label}: sem evento"

    parts = [
        f"- {label}: {event.status or '-'}",
        f"motivo={event.reason or '-'}",
    ]

    if event.symbol:
        parts.append(f"symbol={event.symbol}")

    if event.timestamp:
        parts.append(f"tempo={event.timestamp}")

    return " | ".join(parts)


def _summarize_event(payload: dict[str, Any]) -> UserDataCacheHealthEvent:
    event_type = str(payload.get("event_type", ""))

    data = payload.get("data", {})
    if not isinstance(data, dict):
        data = {}

    status = _first_text(data, "status", "state")
    reason = _first_text(data, "reason", "final_reason")
    symbol = _first_text(data, "symbol")

    return UserDataCacheHealthEvent(
        timestamp=str(payload.get("timestamp", "")),
        event_type=event_type,
        title=EVENT_TITLES.get(event_type, event_type),
        symbol=symbol,
        status=status,
        reason=reason,
        details=_details_for_event(event_type=event_type, data=data),
    )


def _details_for_event(*, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    detail_keys = {
        "user_data_stream_cache_stale_guard": (
            "cache_allowed",
            "fallback_required",
            "stream_connected",
            "listen_key_active",
            "stream_last_message_age_seconds",
            "cache_age_seconds",
            "last_audit_status",
        ),
        "user_data_stream_cache_usage_shadow": (
            "would_use_cache",
            "fallback_required",
            "cache_age_seconds",
            "last_audit_status",
            "last_audit_age_seconds",
        ),
        "user_data_stream_cache_assisted_plan_shadow": (
            "would_use_position_cache",
            "would_use_account_cache",
            "would_use_order_cache",
            "fallback_rest_required",
            "last_audit_status",
        ),
        "user_data_stream_cache_shadow_audit": (
            "position_match",
            "orders_match",
            "account_match",
            "cache_age_seconds",
            "dirty_position",
            "account_dirty",
        ),
        "user_data_stream_cache_reconciliation": (
            "position_reconciled",
            "account_reconciled",
            "open_orders_reconciled",
            "dirty_position_cleared",
            "account_dirty_cleared",
            "position_source",
            "account_source",
            "open_orders_source",
            "final_position_source",
        ),
        "user_data_stream_cache_position_used": ("source",),
        "user_data_stream_cache_account_used": ("source",),
        "user_data_stream_cache_open_orders_used": (
            "source",
            "open_orders_count",
        ),
        "user_data_stream_cache_runtime_metrics": (
            "position_source",
            "account_source",
            "open_orders_source",
            "position_rest_saved",
            "account_rest_saved",
            "open_orders_rest_saved",
            "rest_saved_count",
            "fallback_rest_count",
            "cache_components_used",
            "component_rest_reasons",
            "rest_usage_counts",
            "heavy_snapshot_rest_calls",
            "heavy_snapshot_duration_ms",
            "observability_only",
        ),
        "user_data_stream_event_shadow": (
            "user_event_type",
            "operational_type",
            "severity",
            "execution_changed",
            "order_mutation",
        ),
        "user_data_stream_cache_shadow_updated": (
            "user_event_type",
            "category",
            "operational_type",
            "order_cache_updated",
            "position_cache_updated",
            "account_cache_updated",
            "account_dirty",
        ),
    }

    return {
        key: data.get(key) for key in detail_keys.get(event_type, ()) if data.get(key) is not None
    }


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return str(value)

    return ""


def _format_details(details: dict[str, Any]) -> str:
    if not details:
        return ""

    return ", ".join(f"{key}={value}" for key, value in details.items())


def _runtime_savings_counts(
    events: list[UserDataCacheHealthEvent],
) -> dict[str, int]:
    runtime_events = [
        event for event in events if event.event_type == "user_data_stream_cache_runtime_metrics"
    ]
    return {
        "position_rest_saved": sum(
            1 for event in runtime_events if event.details.get("position_rest_saved")
        ),
        "account_info_rest_saved": sum(
            1 for event in runtime_events if event.details.get("account_rest_saved")
        ),
        "open_orders_rest_saved": sum(
            1 for event in runtime_events if event.details.get("open_orders_rest_saved")
        ),
        "rest_saved_count": sum(
            _as_int(event.details.get("rest_saved_count")) for event in runtime_events
        ),
        "fallback_rest_count": sum(
            _as_int(event.details.get("fallback_rest_count")) for event in runtime_events
        ),
    }


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
