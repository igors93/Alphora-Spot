"""Alphora engine entry point (``alphora-engine`` / ``python -m engine``).

The repository's ``main.py`` is a thin shim over this module so that the
engine also runs from an installed wheel, where ``main.py`` does not exist.
"""

import logging
import os
from pathlib import Path

from binance.futures_client import BinanceFuturesClient
from config import (
    ExecutionMode,
    build_effective_config_summary,
    format_effective_config_summary,
    load_config,
)
from governor.alphora_governor import AlphoraGovernor
from observability import initialize_observability, initialize_run_context, shutdown_observability
from utils.audit_logger import finalize_run_audit, initialize_run_audit, update_run_manifest_fields
from utils.events import EventChannel, observe_error, observe_event
from utils.logger import get_logger


def main() -> None:
    logger = logging.getLogger(__name__)
    try:
        config = load_config()
    except Exception as error:
        initialize_run_context()
        initialize_observability(None)
        initialize_run_audit(None)
        observe_error(
            "config_validation_failed",
            error=error,
            component="main",
            category="system",
            channel=EventChannel.AUDIT,
        )
        finalize_run_audit("FAILED")
        shutdown_observability(status="FAILED")
        raise
    initialize_run_context(config=config)
    initialize_observability(config)
    initialize_run_audit(config)
    logger = get_logger(__name__)
    observe_event(
        "config_loaded",
        component="main",
        category="system",
        channel=EventChannel.AUDIT,
        execution_mode=config.execution_mode.value,
        environment=config.environment.value,
        symbols=list(config.symbols),
    )
    try:
        _enforce_live_entrypoint(config)
        client = BinanceFuturesClient(
            base_url=config.base_url,
            api_key=config.api_key,
            api_secret=config.api_secret,
            mutation_enabled=config.mutations_enabled,
            spot_holdings_path=str(
                Path(config.runtime_dir) / f"spot_holdings_{config.environment_key}.json"
            ),
            bot_order_prefix=config.bot_order_prefix,
            market_type=config.market_type,
            allow_custom_host=config.allow_custom_binance_host,
        )
        governor = AlphoraGovernor.from_config(config, client)
        effective_symbols = [item.symbol for item in getattr(governor, "symbol_configs", [])]
        if effective_symbols:
            update_run_manifest_fields(symbols=effective_symbols)
        _log_startup_summary(logger, config, governor=governor)
        _log_governor_boot(logger, config, governor)
        governor.start()
    except KeyboardInterrupt:
        observe_event(
            "shutdown_requested",
            component="main",
            category="system",
            channel=EventChannel.AUDIT,
            reason="KeyboardInterrupt",
        )
        finalize_run_audit("INTERRUPTED")
        shutdown_observability(status="INTERRUPTED")
        raise
    except Exception as error:
        observe_error(
            "unhandled_exception",
            error=error,
            component="main",
            category="system",
            channel=EventChannel.AUDIT,
        )
        finalize_run_audit("FAILED")
        shutdown_observability(status="FAILED")
        raise
    else:
        finalize_run_audit("COMPLETED")
        shutdown_observability(status="COMPLETED")


def _log_startup_summary(logger, config, *, governor=None) -> None:
    summary = build_effective_config_summary(config)
    if governor is not None:
        symbol_configs = getattr(governor, "symbol_configs", [])
        summary["operation_mode"] = getattr(governor, "operational_mode", summary["operation_mode"])
        if symbol_configs:
            summary["symbols"] = [item.symbol for item in symbol_configs]
            coordinator = getattr(governor, "strategy_coordinator", None)
            summary["strategy"] = getattr(
                coordinator,
                "global_strategy_name",
                summary["strategy"],
            )
            summary["state_path"] = "; ".join(
                f"{item.symbol}={item.state_path}" for item in symbol_configs
            )
            summary["trade_journal_path"] = "; ".join(
                f"{item.symbol}={item.journal_path}" for item in symbol_configs
            )

    logger.info("Iniciando Alphora")
    # One record per line: the privacy filter neutralizes "\n" into literal
    # \x0a (log-injection protection), which turned the multiline banner into
    # a single unreadable line in the log file.
    for line in format_effective_config_summary(summary).splitlines():
        if line.strip():
            logger.info("%s", line)
    logger.info(
        "API Key configurada: %s",
        "sim" if config.api_key else "nao",
    )
    logger.info("Exit model ativo: %s", config.exit_model.value)

    if config.execution_mode is ExecutionMode.DRY_RUN:
        logger.warning("DRY_RUN iniciado fora/por ferramenta local: sem mutacoes reais.")

    if config.execution_mode.is_paper:
        logger.warning("PAPER_LIVE ativo: dados ao vivo e estrategia real com execucao simulada.")


def _log_governor_boot(logger, config, governor) -> None:
    symbol_configs = getattr(governor, "symbol_configs", [])
    operational_mode = getattr(governor, "operational_mode", "GOVERNOR")
    logger.warning(
        "Alphora Governor e o caminho operacional oficial. modo=%s",
        operational_mode,
    )
    if symbol_configs:
        coordinator = getattr(governor, "strategy_coordinator", None)
        global_strategy = getattr(
            coordinator,
            "global_strategy_name",
            f"{config.strategy_module}.{config.strategy_class}",
        )
        runtime_mode = getattr(coordinator, "runtime_mode", "UNKNOWN")
        state_mode = getattr(coordinator, "state_mode", "UNKNOWN")
        logger.info(
            "Boot Governor efetivo | mode=%s | execution_mode=%s | environment=%s | "
            "base_url=%s | runtime_dir=%s | mutations_enabled=%s | origem=%s | "
            "global_strategy=%s | strategy_runtime_mode=%s | strategy_state_mode=%s",
            operational_mode,
            config.execution_mode.value,
            config.environment.value,
            config.base_url,
            config.runtime_dir,
            config.mutations_enabled,
            "GUI" if _env_bool("ALPHORA_LAUNCHED_BY_GUI") else "CLI/dev",
            global_strategy,
            runtime_mode,
            state_mode,
        )
        logger.info(
            "Simbolos/paths habilitados: %s",
            "; ".join(
                f"{item.symbol} state={item.state_path} "
                f"journal={item.journal_path} lock={item.lock_path}"
                for item in symbol_configs
            ),
        )


def _enforce_live_entrypoint(config) -> None:
    launched_by_gui = _env_bool("ALPHORA_LAUNCHED_BY_GUI")
    allow_cli = _env_bool("ALPHORA_ALLOW_CLI_RUN")
    if config.execution_mode in {ExecutionMode.TESTNET_LIVE, ExecutionMode.REAL_LIVE}:
        if launched_by_gui or allow_cli:
            return
        raise SystemExit(
            "Execucao live direta via `python main.py` bloqueada por seguranca.\n"
            "Inicie pela interface Desktop (alphora-desktop).\n"
            "Para desenvolvimento consciente, defina ALPHORA_ALLOW_CLI_RUN=true."
        )


def _env_bool(name: str) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        return False

    value = raw_value.strip().lower()

    if value in {"true", "1", "yes", "y", "sim"}:
        return True

    if value in {"false", "0", "no", "n", "nao", "não"}:
        return False

    raise ValueError(f"{name} precisa ser true ou false")


def run() -> None:
    try:
        main()
    except KeyboardInterrupt:
        print("\nExecucao interrompida pelo usuario")


if __name__ == "__main__":
    run()
