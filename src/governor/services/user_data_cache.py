"""Private (user data) stream: shadow cache, audits and the cache-assisted heavy reads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from config.symbol_context import SymbolTradingConfig
from governor.evaluation.outcomes import EventJournal
from governor.governor_helpers import _enum_value
from governor.governor_trade_helpers import _component_fallback_reason_from_plan
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_assisted_plan import (
    UserDataCacheAssistedPlanShadow,
    build_user_data_cache_assisted_plan_shadow,
)
from governor.user_data_cache_audit import audit_user_data_shadow_cache
from governor.user_data_cache_reconciliation import reconcile_user_data_cache_from_rest
from governor.user_data_cache_stale_guard import evaluate_user_data_cache_stale_guard
from governor.user_data_cache_usage import (
    UserDataCacheUsageShadowEvaluation,
    evaluate_user_data_cache_usage_shadow,
)

USER_DATA_CACHE_SOURCE = "USER_DATA_CACHE"

# Reasons a heavy-snapshot component still went to REST, grouped for the metrics event.
_REST_USAGE_BUCKETS = {
    "rest_avoided_by_cache": {"REST_AVOIDED_BY_CACHE"},
    "rest_used_cache_disabled": {"REST_USED_CACHE_DISABLED"},
    "rest_used_fallback_guard_audit_stale": {"REST_USED_FALLBACK_GUARD_AUDIT_STALE"},
    "rest_used_component_unavailable": {"REST_USED_COMPONENT_UNAVAILABLE"},
    "rest_used_other": {"REST_REQUIRED", "REST_USED_CACHE_PLAN_UNAVAILABLE"},
}


class UserDataStreamHubPort(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health_snapshot(self, now: float | None = None) -> dict[str, Any]: ...


def _ignore_cache(_cache: UserDataShadowCache) -> None:
    return None


def _flag(config: Any, name: str, default: bool) -> bool:
    return bool(getattr(config, name, default))


def _seconds(config: Any, name: str, default: float) -> float:
    return max(0.0, float(getattr(config, name, default) or 0.0))


class UserDataCacheService:
    """Shadow cache of the private stream and every audit around it.

    The cache only replaces a REST read when its component is enabled, the assisted
    plan allows it and the stale guard is healthy; everything else is recorded as
    shadow evidence. ``audits`` is the last cache-vs-REST audit per symbol and is
    shared with the Governor, so it survives across on-demand service instances.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal,
        logger: Any = None,
        hub: UserDataStreamHubPort | None = None,
        cache: UserDataShadowCache | None = None,
        audits: dict[str, dict[str, Any]] | None = None,
        provider: Any = None,
        on_cache_created: Callable[[UserDataShadowCache], None] = _ignore_cache,
    ) -> None:
        self.config = config
        self.journal = journal
        self.logger = logger
        self.hub = hub
        self.cache = cache
        self.audits: dict[str, dict[str, Any]] = {} if audits is None else audits
        self.provider = provider
        self.on_cache_created = on_cache_created

    # --- stream lifecycle ------------------------------------------------------------

    def start_stream(self) -> None:
        if self.hub is None:
            return

        self.hub.start()
        self.journal.append(
            "user_data_stream_shadow_started",
            status="STARTED",
            shadow_only=self.config.user_data_stream_shadow_only,
            execution_changed=False,
            order_mutation=False,
        )

    def stop_stream(self) -> None:
        if self.hub is None:
            return

        try:
            self.hub.stop()
            self.journal.append(
                "user_data_stream_shadow_stopped",
                status="STOPPED",
                shadow_only=self.config.user_data_stream_shadow_only,
                execution_changed=False,
                order_mutation=False,
            )
        except Exception as error:
            self.logger.warning("Falha ao parar User Data Stream: %s", error)

    def stream_health(self, now: float) -> dict[str, Any]:
        if self.hub is None:
            return {"enabled": False, "connected": False}

        try:
            return self.hub.health_snapshot(now=now)
        except Exception as error:
            self.logger.warning(
                "Falha ao coletar health do User Data Stream: %s",
                error,
            )
            return {
                "enabled": True,
                "connected": False,
                "last_error": str(error),
            }

    # --- stream events -----------------------------------------------------------------

    def record_stream_event(self, event: dict[str, Any]) -> None:
        classification = event.get("classification", {})
        if not isinstance(classification, dict):
            classification = {}

        self.journal.append(
            "user_data_stream_event_shadow",
            status="OBSERVED",
            user_event_type=event.get("event_type", ""),
            symbol=event.get("symbol", ""),
            event_time=event.get("event_time"),
            transaction_time=event.get("transaction_time"),
            received_at=event.get("received_at"),
            shadow_only=event.get("shadow_only", True),
            source=event.get("source", "user_data_stream"),
            operational_category=event.get(
                "operational_category",
                classification.get("category", "UNKNOWN"),
            ),
            operational_type=event.get(
                "operational_type",
                classification.get("operational_type", "UNKNOWN"),
            ),
            severity=event.get("severity", classification.get("severity", "INFO")),
            classification=classification,
            should_update_order_cache=classification.get("should_update_order_cache", False),
            should_update_position_cache=classification.get("should_update_position_cache", False),
            should_update_account_cache=classification.get("should_update_account_cache", False),
            raw=event.get("raw", {}),
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-2: evento privado classificado em shadow. "
                    "Nenhum cache, estado, decisao ou ordem foi alterado."
                ),
            },
        )
        self.apply_stream_event(event)

    def apply_stream_event(self, event: dict[str, Any]) -> None:
        cache = self.cache
        if cache is None:
            cache = UserDataShadowCache()
            self.cache = cache
            self.on_cache_created(cache)

        update = cache.apply_event(event)
        if update is None:
            return

        payload = update.to_dict()
        user_event_type = payload.pop("event_type", "")

        self.journal.append(
            "user_data_stream_cache_shadow_updated",
            status="UPDATED",
            user_event_type=user_event_type,
            **payload,
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Cache privado atualizado e publicado no snapshot engine. "
                    "A sessao do ciclo fixa os valores usados pela decisao."
                ),
            },
        )
        self._publish_update(cache=cache, update=update, event=event)

    def _publish_update(
        self,
        *,
        cache: UserDataShadowCache,
        update: Any,
        event: dict[str, Any],
    ) -> None:
        if self.provider is None:
            return
        try:
            self.provider.publish_user_data_cache_update(cache=cache, update=update, event=event)
        except Exception as error:
            self.logger.warning(
                "Falha ao publicar User Data Stream no snapshot engine: %s",
                error,
            )

    # --- audits ------------------------------------------------------------------------

    def remember_audit(
        self,
        *,
        symbol: str,
        status: str,
        payload: dict[str, Any],
        now: float,
    ) -> None:
        normalized_symbol = str(symbol or "").upper().strip()
        if not normalized_symbol:
            return

        self.audits[normalized_symbol] = {
            "status": status,
            "recorded_at": now,
            **payload,
        }

    def record_shadow_audit(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy: Any,
        now: float,
    ) -> None:
        if not _flag(self.config, "user_data_stream_cache_audit_enabled", True):
            return

        cache = self.cache
        if cache is None:
            return

        if getattr(cache, "last_event_at", None) is None:
            return

        result = audit_user_data_shadow_cache(
            cache=cache,
            symbol=symbol_config.symbol,
            heavy_snapshot=heavy,
            now=now,
            max_cache_age_seconds=_seconds(
                self.config, "user_data_stream_cache_audit_max_age_seconds", 30.0
            ),
        )

        payload = result.to_dict()
        audit_status = payload.pop("status", "UNKNOWN")

        self.remember_audit(
            symbol=symbol_config.symbol,
            status=audit_status,
            payload=payload,
            now=now,
        )
        self.journal.append(
            "user_data_stream_cache_shadow_audit",
            status=audit_status,
            **payload,
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-4: auditoria cache shadow vs REST. "
                    "Nenhum dado do cache foi usado para decisao ou execucao."
                ),
            },
        )

    def record_reconciliation(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy: Any,
        now: float,
        rest_backed_position: bool,
        rest_backed_account: bool,
        rest_backed_open_orders: bool,
        position_source: str | None = None,
        account_source: str | None = None,
        open_orders_source: str | None = None,
        final_position_source: str | None = None,
    ) -> None:
        if not _flag(self.config, "user_data_stream_cache_reconciliation_enabled", True):
            return

        result = reconcile_user_data_cache_from_rest(
            cache=self.cache,
            symbol=symbol_config.symbol,
            heavy_snapshot=heavy,
            now=now,
            rest_backed_position=rest_backed_position,
            rest_backed_account=rest_backed_account,
            rest_backed_open_orders=rest_backed_open_orders,
            position_source=position_source,
            account_source=account_source,
            open_orders_source=open_orders_source,
            final_position_source=final_position_source,
        )

        payload = result.to_dict()
        reconciliation_status = payload.pop("status", "UNKNOWN")

        if reconciliation_status == "SKIPPED":
            return

        self.journal.append(
            "user_data_stream_cache_reconciliation",
            status=reconciliation_status,
            **payload,
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-7B: reconciliacao REST do cache privado. "
                    "Somente cache shadow foi atualizado; decisao e ordens nao mudaram."
                ),
            },
        )

    def _guard_inputs(self, symbol_config: SymbolTradingConfig, now: float) -> dict[str, Any]:
        """Arguments shared by the stale guard and the usage shadow evaluation."""

        symbol = str(symbol_config.symbol or "").upper().strip()
        config = self.config
        return {
            "cache": self.cache,
            "symbol": symbol,
            "now": now,
            "stream_health": self.stream_health(now),
            "last_audit": self.audits.get(symbol),
            "max_cache_age_seconds": _seconds(
                config, "user_data_stream_cache_usage_max_age_seconds", 10.0
            ),
            "max_stream_message_age_seconds": _seconds(
                config, "user_data_stream_cache_stale_guard_max_message_age_seconds", 30.0
            ),
            "max_audit_age_seconds": _seconds(
                config, "user_data_stream_cache_usage_max_audit_age_seconds", 60.0
            ),
            "require_audit_match": _flag(
                config, "user_data_stream_cache_usage_require_audit_match", True
            ),
            "require_active_listen_key": _flag(
                config, "user_data_stream_cache_stale_guard_require_listen_key", True
            ),
        }

    def record_stale_guard(self, *, symbol_config: SymbolTradingConfig, now: float) -> None:
        if not _flag(self.config, "user_data_stream_cache_stale_guard_enabled", True):
            return

        evaluation = evaluate_user_data_cache_stale_guard(**self._guard_inputs(symbol_config, now))

        payload = evaluation.to_dict()
        guard_status = payload.pop("status", "UNKNOWN")

        self.journal.append(
            "user_data_stream_cache_stale_guard",
            status=guard_status,
            **payload,
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-7A: stale guard do cache privado. "
                    "Se status nao for HEALTHY, cache-assisted deve cair para REST."
                ),
            },
        )

    def record_usage_shadow(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        now: float,
    ) -> UserDataCacheUsageShadowEvaluation | None:
        if not _flag(self.config, "user_data_stream_cache_usage_shadow_enabled", True):
            return None

        evaluation = evaluate_user_data_cache_usage_shadow(**self._guard_inputs(symbol_config, now))

        payload = evaluation.to_dict()
        usage_status = payload.pop("status", "UNKNOWN")

        self.journal.append(
            "user_data_stream_cache_usage_shadow",
            status=usage_status,
            **payload,
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-5: elegibilidade shadow do cache privado. "
                    "REST ainda nao foi substituido e nenhuma decisao usa o cache."
                ),
            },
        )
        return evaluation

    def record_assisted_plan_shadow(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        usage_evaluation: UserDataCacheUsageShadowEvaluation | None,
    ) -> UserDataCacheAssistedPlanShadow | None:
        if not _flag(self.config, "user_data_stream_cache_assisted_plan_shadow_enabled", True):
            return None

        plan = build_user_data_cache_assisted_plan_shadow(
            cache=self.cache,
            symbol=symbol_config.symbol,
            usage_evaluation=usage_evaluation,
        )

        payload = plan.to_dict()
        plan_status = payload.pop("status", "UNKNOWN")

        self.journal.append(
            "user_data_stream_cache_assisted_plan_shadow",
            status=plan_status,
            **payload,
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-6A: plano shadow de cache-assisted heavy snapshot. "
                    "REST continua sendo usado normalmente."
                ),
            },
        )
        return plan

    # --- runtime metrics ---------------------------------------------------------------

    def _component_enabled(self) -> dict[str, bool]:
        return {
            "position": _flag(self.config, "user_data_stream_cache_position_enabled", False),
            "account_info": _flag(self.config, "user_data_stream_cache_account_enabled", False),
            "open_orders": _flag(self.config, "user_data_stream_cache_open_orders_enabled", False),
        }

    @staticmethod
    def component_rest_reason(
        *,
        source: str,
        component_enabled: bool,
        component: str,
        assisted_plan: Any,
    ) -> str:
        if str(source or "").upper().strip() == USER_DATA_CACHE_SOURCE:
            return "REST_AVOIDED_BY_CACHE"

        if not component_enabled:
            return "REST_USED_CACHE_DISABLED"

        component_fallback_reason = _component_fallback_reason_from_plan(assisted_plan, component)
        if component_fallback_reason:
            if component_fallback_reason.endswith("_MISSING"):
                return "REST_USED_COMPONENT_UNAVAILABLE"
            return "REST_USED_FALLBACK_GUARD_AUDIT_STALE"

        if not assisted_plan:
            return "REST_USED_CACHE_PLAN_UNAVAILABLE"

        plan_reason = str(getattr(assisted_plan, "reason", "") or "").upper()
        if plan_reason in {
            "CACHE_NOT_AVAILABLE",
            "USAGE_EVALUATION_MISSING",
            "CACHE_ASSISTED_PLAN_PARTIAL",
        }:
            return "REST_USED_COMPONENT_UNAVAILABLE"

        if (
            plan_reason.startswith("CACHE_")
            or plan_reason.startswith("USER_DATA_STREAM_")
            or plan_reason.startswith("AUDIT_")
        ):
            return "REST_USED_FALLBACK_GUARD_AUDIT_STALE"

        return "REST_REQUIRED"

    def record_runtime_metrics(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        assisted_plan: Any,
        position_source: str,
        account_source: str,
        open_orders_source: str,
        heavy_snapshot_duration_ms: float | None,
    ) -> None:
        component_sources = {
            "position": position_source,
            "account_info": account_source,
            "open_orders": open_orders_source,
        }
        cache_components_used = {
            component: source == USER_DATA_CACHE_SOURCE
            for component, source in component_sources.items()
        }
        rest_saved_count = sum(1 for used in cache_components_used.values() if used)
        cache_components_enabled = self._component_enabled()
        component_rest_reasons = {
            component: self.component_rest_reason(
                source=source,
                component_enabled=cache_components_enabled[component],
                component=component,
                assisted_plan=assisted_plan,
            )
            for component, source in component_sources.items()
        }
        rest_usage_counts: dict[str, int] = {
            bucket: sum(1 for reason in component_rest_reasons.values() if reason in reasons)
            for bucket, reasons in _REST_USAGE_BUCKETS.items()
        }
        rest_usage_counts["rest_mandatory"] = 2

        config = self.config
        self.journal.append(
            "user_data_stream_cache_runtime_metrics",
            status="OBSERVED",
            reason="OBSERVABILITY_ONLY",
            symbol=symbol_config.symbol,
            execution_mode=_enum_value(getattr(config, "execution_mode", "")),
            environment=_enum_value(getattr(config, "environment", "")),
            user_data_cache_operational_preset=str(
                getattr(config, "user_data_cache_operational_preset", "") or ""
            ),
            user_data_cache_effective_preset=str(
                getattr(config, "user_data_cache_effective_preset", "") or ""
            ),
            position_source=position_source,
            account_source=account_source,
            open_orders_source=open_orders_source,
            position_rest_saved=cache_components_used["position"],
            account_rest_saved=cache_components_used["account_info"],
            open_orders_rest_saved=cache_components_used["open_orders"],
            rest_saved_count=rest_saved_count,
            fallback_rest_count=len(cache_components_used) - rest_saved_count,
            cache_components_used=cache_components_used,
            cache_components_enabled=cache_components_enabled,
            component_rest_reasons=component_rest_reasons,
            rest_usage_counts=rest_usage_counts,
            heavy_snapshot_rest_calls={
                "ticker_price": "LIGHT_SNAPSHOT_CACHE_OR_REST",
                "mark_price": "REST_MANDATORY",
                "exchange_info": "ROUTER_CACHE_OR_REST",
                "open_algo_orders": "REST_MANDATORY",
                "position_risk": component_rest_reasons["position"],
                "account_info": component_rest_reasons["account_info"],
                "open_orders": component_rest_reasons["open_orders"],
            },
            heavy_snapshot_duration_ms=heavy_snapshot_duration_ms,
            execution_changed=False,
            order_mutation=False,
            observability_only=True,
            diagnostics={
                "safety_note": (
                    "Fase 10I-10A: metrica runtime de economia REST e latencia. "
                    "Nao altera decisao, execucao, ordem ou cache."
                ),
            },
        )

    # --- cache-assisted heavy reads ----------------------------------------------------

    def _usable_cache(self, assisted_plan: Any, *, enabled: str, plan_flag: str) -> bool:
        if not _flag(self.config, enabled, False):
            return False
        if assisted_plan is None:
            return False
        if not getattr(assisted_plan, plan_flag, False):
            return False
        return self.cache is not None

    def cached_position(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        assisted_plan: Any,
    ) -> Any:
        if not self._usable_cache(
            assisted_plan,
            enabled="user_data_stream_cache_position_enabled",
            plan_flag="would_use_position_cache",
        ):
            return None
        assert self.cache is not None

        position = self.cache.futures_position_for_symbol(symbol_config.symbol)
        if position is None:
            return None

        self.journal.append(
            "user_data_stream_cache_position_used",
            status="USED",
            symbol=symbol_config.symbol,
            source="user_data_shadow_cache",
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-6B: posicao veio do cache privado auditado. "
                    "Fallback REST permanece disponivel para os demais dados."
                ),
            },
        )
        return position

    def cached_account_info(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        assisted_plan: Any,
    ) -> dict[str, Any] | None:
        if not self._usable_cache(
            assisted_plan,
            enabled="user_data_stream_cache_account_enabled",
            plan_flag="would_use_account_cache",
        ):
            return None
        assert self.cache is not None

        account_info = self.cache.account_info_snapshot()
        if account_info is None:
            return None

        if account_info.get("runtime_balance_fields_trusted") is not True:
            self.journal.append(
                "user_data_stream_cache_account_fallback",
                status="REST_FALLBACK",
                symbol=symbol_config.symbol,
                reason="ACCOUNT_RUNTIME_BALANCE_FIELDS_UNTRUSTED",
                source="user_data_shadow_cache",
                execution_changed=False,
                order_mutation=False,
                diagnostics={
                    "safety_note": (
                        "Account_info do User Data Stream nao contem campos "
                        "runtime de saldo confiaveis; heavy snapshot deve usar REST."
                    ),
                },
            )
            return None

        self.journal.append(
            "user_data_stream_cache_account_used",
            status="USED",
            symbol=symbol_config.symbol,
            source="user_data_shadow_cache",
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-6C: account_info veio do cache privado auditado. "
                    "Fallback REST permanece disponivel."
                ),
            },
        )
        return account_info

    def cached_open_orders(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        assisted_plan: Any,
    ) -> list[dict[str, Any]] | None:
        if not self._usable_cache(
            assisted_plan,
            enabled="user_data_stream_cache_open_orders_enabled",
            plan_flag="would_use_order_cache",
        ):
            return None
        assert self.cache is not None

        open_orders = self.cache.open_orders_snapshot(symbol_config.symbol)

        self.journal.append(
            "user_data_stream_cache_open_orders_used",
            status="USED",
            symbol=symbol_config.symbol,
            source="user_data_shadow_cache",
            open_orders_count=len(open_orders),
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10I-6D: open_orders veio do cache privado auditado. "
                    "Fallback REST permanece disponivel."
                ),
            },
        )
        return open_orders
