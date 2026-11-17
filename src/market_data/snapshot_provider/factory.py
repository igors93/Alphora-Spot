"""Start the snapshot provider under the startup policy of the execution mode."""

from __future__ import annotations

from typing import Any

from binance.futures_client import BinanceFuturesClient
from market_data import snapshot_engine
from market_data.snapshot_engine import CapabilityRequirementError
from market_data.snapshot_provider import wire_names
from market_data.snapshot_provider.health import COMPONENT
from market_data.snapshot_provider.provider import (
    REQUIRED_FEATURES,
    REQUIRED_SCHEMAS,
    SnapshotProvider,
)
from market_data.snapshot_provider.settings import (
    SnapshotProviderSettings,
    SnapshotStartupPolicy,
    resolve_startup_policy,
)
from utils.events import EventChannel, observe_error, observe_event

_STARTUP_SETTINGS = (
    "light_ttl_seconds",
    "heavy_ttl_seconds",
    "max_concurrency",
    "max_pending_tasks",
    "max_pending_submissions",
    "deadline_seconds",
    "cycle_deadline_seconds",
    "rest_timeout_seconds",
    "rest_connect_timeout_seconds",
    "rest_read_timeout_seconds",
    "rest_transport_timeout_seconds",
    "rest_max_attempts",
    "rest_retry_delay_seconds",
    "execution_max_age_seconds",
    "execution_max_skew_seconds",
    "rest_concurrency",
    "stream_concurrency",
    "max_data_age_seconds",
)


def build_snapshot_provider(
    *,
    client: BinanceFuturesClient,
    market_data_hub: Any = None,
    candle_data_hub: Any = None,
    user_data_cache: Any = None,
    settings: SnapshotProviderSettings | None = None,
    logger: Any = None,
    execution_mode: Any = None,
    environment: Any = None,
    fallback_allowed: bool | None = None,
) -> SnapshotProvider | None:
    """Start the provider; return ``None`` only where the policy allows running without it."""

    startup_policy = resolve_startup_policy(
        execution_mode=execution_mode,
        environment=environment,
        fallback_allowed=fallback_allowed,
    )
    try:
        provider = SnapshotProvider(
            client=client,
            market_data_hub=market_data_hub,
            candle_data_hub=candle_data_hub,
            user_data_cache=user_data_cache,
            settings=settings,
        )
    except CapabilityRequirementError:
        # A missing capability in the embedded snapshot engine is a source regression.
        # Keep failing closed to preserve the required safety guarantees.
        raise
    except Exception as error:
        _warn_integration_error(error, logger=logger, startup_policy=startup_policy)
        required: dict[str, Any] = {wire_names.REQUIRED_KEY: startup_policy.required}
        observe_error(
            "snapshot_provider_start_failed",
            error=error,
            component=COMPONENT,
            category="market_data",
            resource=wire_names.ERROR_RESOURCE_PROVIDER,
            source=wire_names.ERROR_SOURCE_PROVIDER,
            error_type=type(error).__name__,
            message=str(error),
            execution_mode=startup_policy.execution_mode,
            environment=startup_policy.environment,
            fallback_allowed=startup_policy.fallback_allowed,
            **required,
        )
        if startup_policy.fallback_allowed and not startup_policy.required:
            return None
        raise

    engine_identity: dict[str, Any] = {
        wire_names.ENABLED_KEY: True,
        wire_names.VERSION_KEY: getattr(snapshot_engine, "__version__", "unknown"),
    }
    observe_event(
        wire_names.PROVIDER_STARTED,
        component=__name__,
        category="market_data",
        channel=EventChannel.AUDIT,
        **engine_identity,
        required_features=list(REQUIRED_FEATURES),
        required_schemas=dict(REQUIRED_SCHEMAS),
        capabilities_validated=True,
        startup_policy=startup_policy.to_dict(),
        settings={name: getattr(provider.settings, name) for name in _STARTUP_SETTINGS},
    )
    return provider


def _warn_integration_error(
    error: Exception,
    *,
    logger: Any = None,
    startup_policy: SnapshotStartupPolicy | None = None,
) -> None:
    message = (
        "AVISO: falha ao inicializar o mecanismo de snapshots integrado ao Alphora. "
        "Verifique as capacidades de src/market_data/snapshot_engine. Erro: %s"
    )
    if logger is not None:
        try:
            logger.warning(message, error)
        except Exception as logger_error:
            message = f"{message} Falha adicional ao registrar warning: {logger_error}"
    observe_event(
        wire_names.PROVIDER_UNAVAILABLE,
        component=__name__,
        category="market_data",
        channel=EventChannel.AUDIT,
        severity="WARNING",
        aviso=message % error,
        reason=str(error),
        error_type=type(error).__name__,
        startup_policy={} if startup_policy is None else startup_policy.to_dict(),
        **{wire_names.ENABLED_KEY: False},
    )


__all__ = ["build_snapshot_provider"]
