from dataclasses import dataclass
from typing import Any

USER_DATA_CACHE_PRESET_DISABLED = "DISABLED"
USER_DATA_CACHE_PRESET_SHADOW_OBSERVE = "SHADOW_OBSERVE"
USER_DATA_CACHE_PRESET_POSITION_CONSERVATIVE = "POSITION_CONSERVATIVE"
USER_DATA_CACHE_PRESET_TESTNET_FULL_CACHE = "TESTNET_FULL_CACHE"

USER_DATA_CACHE_PRESETS = (
    USER_DATA_CACHE_PRESET_DISABLED,
    USER_DATA_CACHE_PRESET_SHADOW_OBSERVE,
    USER_DATA_CACHE_PRESET_POSITION_CONSERVATIVE,
    USER_DATA_CACHE_PRESET_TESTNET_FULL_CACHE,
)
USER_DATA_CACHE_COMPONENTS = ("position", "account_info", "open_orders")


@dataclass(frozen=True)
class UserDataCachePresetResolution:
    selected_preset: str
    effective_preset: str
    degrade_reason: str
    cache_components: tuple[str, ...]
    rest_components: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return self.selected_preset != self.effective_preset


def normalize_user_data_cache_preset(value: Any) -> str:
    normalized = str(value or "").upper().strip()
    if normalized in USER_DATA_CACHE_PRESETS:
        return normalized
    return USER_DATA_CACHE_PRESET_DISABLED


def resolve_user_data_cache_preset(
    settings: dict[str, Any],
    preset: str | None = None,
) -> UserDataCachePresetResolution:
    selected = normalize_user_data_cache_preset(
        preset if preset is not None else settings.get("user_data_cache_operational_preset")
    )
    effective = selected
    reason = ""
    execution_mode = str(settings.get("execution_mode", "")).upper().strip()

    if selected == USER_DATA_CACHE_PRESET_POSITION_CONSERVATIVE and execution_mode == "REAL_LIVE":
        effective = USER_DATA_CACHE_PRESET_SHADOW_OBSERVE
        reason = "POSITION_CONSERVATIVE cai para SHADOW_OBSERVE em REAL_LIVE."

    if selected == USER_DATA_CACHE_PRESET_TESTNET_FULL_CACHE and execution_mode != "TESTNET_LIVE":
        effective = USER_DATA_CACHE_PRESET_SHADOW_OBSERVE
        reason = "TESTNET_FULL_CACHE só é permitido em TESTNET_LIVE."

    cache_components = _cache_components_for_effective_preset(effective)
    rest_components = tuple(
        component for component in USER_DATA_CACHE_COMPONENTS if component not in cache_components
    )
    return UserDataCachePresetResolution(
        selected_preset=selected,
        effective_preset=effective,
        degrade_reason=reason,
        cache_components=cache_components,
        rest_components=rest_components,
    )


def apply_user_data_cache_preset(
    settings: dict[str, Any],
    preset: str | None = None,
) -> dict[str, Any]:
    prepared = dict(settings)
    resolution = resolve_user_data_cache_preset(prepared, preset=preset)
    effective = resolution.effective_preset

    prepared["user_data_cache_operational_preset"] = resolution.selected_preset
    prepared["user_data_cache_effective_preset"] = resolution.effective_preset
    prepared["user_data_cache_preset_degraded"] = resolution.degraded
    prepared["user_data_cache_preset_degrade_reason"] = resolution.degrade_reason

    if effective == USER_DATA_CACHE_PRESET_SHADOW_OBSERVE:
        prepared.update(
            {
                "user_data_stream_enabled": True,
                "user_data_stream_shadow_only": True,
                "user_data_stream_keepalive_seconds": "1800",
                "user_data_stream_reconnect_seconds": "5",
                "user_data_stream_heartbeat_seconds": "60",
                "user_data_stream_max_pending_events": "500",
                "user_data_stream_cache_audit_enabled": True,
                "user_data_stream_cache_audit_max_age_seconds": "30",
                "user_data_stream_cache_usage_shadow_enabled": True,
                "user_data_stream_cache_usage_require_audit_match": True,
                "user_data_stream_cache_usage_max_age_seconds": "10",
                "user_data_stream_cache_usage_max_audit_age_seconds": "60",
                "user_data_stream_cache_assisted_plan_shadow_enabled": True,
                "user_data_stream_cache_position_enabled": False,
                "user_data_stream_cache_account_enabled": False,
                "user_data_stream_cache_open_orders_enabled": False,
                "user_data_stream_cache_stale_guard_enabled": True,
                "user_data_stream_cache_stale_guard_max_message_age_seconds": "30",
                "user_data_stream_cache_stale_guard_require_listen_key": True,
                "user_data_stream_cache_reconciliation_enabled": True,
            }
        )
        return prepared

    if effective == USER_DATA_CACHE_PRESET_POSITION_CONSERVATIVE:
        prepared.update(
            {
                "user_data_stream_enabled": True,
                "user_data_stream_shadow_only": True,
                "user_data_stream_keepalive_seconds": "1800",
                "user_data_stream_reconnect_seconds": "5",
                "user_data_stream_heartbeat_seconds": "60",
                "user_data_stream_max_pending_events": "500",
                "user_data_stream_cache_audit_enabled": True,
                "user_data_stream_cache_audit_max_age_seconds": "30",
                "user_data_stream_cache_usage_shadow_enabled": True,
                "user_data_stream_cache_usage_require_audit_match": True,
                "user_data_stream_cache_usage_max_age_seconds": "10",
                "user_data_stream_cache_usage_max_audit_age_seconds": "60",
                "user_data_stream_cache_assisted_plan_shadow_enabled": True,
                "user_data_stream_cache_position_enabled": True,
                "user_data_stream_cache_account_enabled": False,
                "user_data_stream_cache_open_orders_enabled": False,
                "user_data_stream_cache_stale_guard_enabled": True,
                "user_data_stream_cache_stale_guard_max_message_age_seconds": "30",
                "user_data_stream_cache_stale_guard_require_listen_key": True,
                "user_data_stream_cache_reconciliation_enabled": True,
            }
        )
        return prepared

    if effective == USER_DATA_CACHE_PRESET_TESTNET_FULL_CACHE:
        prepared.update(
            {
                "user_data_stream_enabled": True,
                "user_data_stream_shadow_only": True,
                "user_data_stream_keepalive_seconds": "1800",
                "user_data_stream_reconnect_seconds": "5",
                "user_data_stream_heartbeat_seconds": "60",
                "user_data_stream_max_pending_events": "500",
                "user_data_stream_cache_audit_enabled": True,
                "user_data_stream_cache_audit_max_age_seconds": "30",
                "user_data_stream_cache_usage_shadow_enabled": True,
                "user_data_stream_cache_usage_require_audit_match": True,
                "user_data_stream_cache_usage_max_age_seconds": "10",
                "user_data_stream_cache_usage_max_audit_age_seconds": "60",
                "user_data_stream_cache_assisted_plan_shadow_enabled": True,
                "user_data_stream_cache_position_enabled": True,
                "user_data_stream_cache_account_enabled": True,
                "user_data_stream_cache_open_orders_enabled": True,
                "user_data_stream_cache_stale_guard_enabled": True,
                "user_data_stream_cache_stale_guard_max_message_age_seconds": "30",
                "user_data_stream_cache_stale_guard_require_listen_key": True,
                "user_data_stream_cache_reconciliation_enabled": True,
            }
        )
        return prepared

    prepared.update(
        {
            "user_data_stream_enabled": False,
            "user_data_stream_shadow_only": True,
            "user_data_stream_keepalive_seconds": "1800",
            "user_data_stream_reconnect_seconds": "5",
            "user_data_stream_heartbeat_seconds": "60",
            "user_data_stream_max_pending_events": "500",
            "user_data_stream_cache_audit_enabled": False,
            "user_data_stream_cache_audit_max_age_seconds": "30",
            "user_data_stream_cache_usage_shadow_enabled": False,
            "user_data_stream_cache_usage_require_audit_match": True,
            "user_data_stream_cache_usage_max_age_seconds": "10",
            "user_data_stream_cache_usage_max_audit_age_seconds": "60",
            "user_data_stream_cache_assisted_plan_shadow_enabled": False,
            "user_data_stream_cache_position_enabled": False,
            "user_data_stream_cache_account_enabled": False,
            "user_data_stream_cache_open_orders_enabled": False,
            "user_data_stream_cache_stale_guard_enabled": False,
            "user_data_stream_cache_stale_guard_max_message_age_seconds": "30",
            "user_data_stream_cache_stale_guard_require_listen_key": True,
            "user_data_stream_cache_reconciliation_enabled": False,
        }
    )
    return prepared


def format_user_data_cache_preset_summary(settings: dict[str, Any]) -> str:
    resolution = resolve_user_data_cache_preset(settings)
    lines = [
        f"Selecionado: {resolution.selected_preset}",
        f"Efetivo: {resolution.effective_preset}",
        f"Motivo: {resolution.degrade_reason or 'preset permitido no modo atual.'}",
        f"Cache real: {_format_components(resolution.cache_components)}",
        f"REST: {_format_components(resolution.rest_components)}",
        "",
    ]

    if resolution.effective_preset == USER_DATA_CACHE_PRESET_SHADOW_OBSERVE:
        lines.append(
            "Preset SHADOW_OBSERVE: User Data Stream ligado em observação. "
            "Auditoria, stale guard, plano shadow e reconciliação ficam ligados. "
            "Uso real de cache para posição/account_info/open_orders permanece "
            "desligado."
        )
        return "\n".join(lines)

    if resolution.effective_preset == USER_DATA_CACHE_PRESET_POSITION_CONSERVATIVE:
        lines.append(
            "Preset POSITION_CONSERVATIVE: User Data Stream ligado com "
            "cache-assisted somente para posição. account_info e open_orders "
            "continuam via REST. Auditoria, stale guard e reconciliação ficam "
            "ligados. Em REAL_LIVE este preset cai automaticamente para "
            "SHADOW_OBSERVE."
        )
        return "\n".join(lines)

    if resolution.effective_preset == USER_DATA_CACHE_PRESET_TESTNET_FULL_CACHE:
        lines.append(
            "Preset TESTNET_FULL_CACHE: User Data Stream ligado com cache-assisted "
            "para posição, account_info e open_orders. Auditoria, stale guard e "
            "reconciliação ficam ligados. Este preset só permanece ativo em "
            "TESTNET_LIVE; em outros modos cai automaticamente para SHADOW_OBSERVE."
        )
        return "\n".join(lines)

    lines.append(
        "Preset DISABLED: User Data Stream/cache privado desligado pela "
        "interface. O Governor continuará usando REST normalmente."
    )
    return "\n".join(lines)


def _cache_components_for_effective_preset(preset: str) -> tuple[str, ...]:
    if preset == USER_DATA_CACHE_PRESET_POSITION_CONSERVATIVE:
        return ("position",)

    if preset == USER_DATA_CACHE_PRESET_TESTNET_FULL_CACHE:
        return USER_DATA_CACHE_COMPONENTS

    return ()


def _format_components(components: tuple[str, ...]) -> str:
    return "/".join(components) if components else "nenhum"
