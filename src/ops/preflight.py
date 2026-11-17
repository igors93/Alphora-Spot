import argparse
import json
from pathlib import Path

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesNetworkError
from config import ExecutionMode, ProtectionPolicy, RecoveryAction, load_config
from config.symbol_context import (
    load_portfolio_risk_settings,
    load_symbol_profiles,
    resolve_governor_symbol_configs,
)
from futures.exchange_rules import extract_symbol_rules
from futures.operational import manual_reconcile_path
from futures.process_lock import coerce_pid, is_pid_running
from ops.common import (
    CheckResult,
    CheckSeverity,
    extract_algo_client_id,
    extract_normal_client_order_id,
    extract_usdt_asset,
    is_robot_client_id,
    make_client,
    print_result,
)
from strategies.loader import StrategyLoadError, load_strategy


def run_preflight() -> dict:
    checks: list[CheckResult] = []

    try:
        config = load_config()
        checks.append(CheckResult("config", True, "Configuracao carregada"))
    except Exception as error:
        checks.append(CheckResult("config", False, str(error), CheckSeverity.ERROR.value))
        return _payload(checks)

    if config.multi_symbol_enabled:
        return run_multi_symbol_preflight(config, checks)

    _check_mode_environment(config, checks)
    _check_credentials(config, checks)
    _check_strategy(config, checks)
    _check_symbol(config, checks)

    symbol_config = _check_symbol_profiles(config, checks)
    if symbol_config is None:
        if any(not check.ok and check.severity == CheckSeverity.ERROR.value for check in checks):
            return _payload(checks)
        _check_lock(config, checks)
        _check_manual_reconcile(config.runtime_dir, config.symbol, checks)
    else:
        _check_symbol_lock(symbol_config.lock_path, checks)
        _check_manual_reconcile(symbol_config.runtime_dir, symbol_config.symbol, checks)

    if any(not check.ok and check.severity == CheckSeverity.ERROR.value for check in checks):
        return _payload(checks)

    effective_symbol = symbol_config.symbol if symbol_config is not None else config.symbol
    client = make_client(config, mutation_enabled=False)
    try:
        client.ping()
        checks.append(CheckResult("public_connection", True, "Conexao publica OK"))

        client.sync_time()

        account = client.account_info()
        checks.append(CheckResult("account_auth", True, "Autenticacao Futures OK"))

        _check_balance(account, checks)
        _check_position_mode(client, checks)

        exchange_info = client.exchange_info()
        extract_symbol_rules(exchange_info, effective_symbol)
        checks.append(CheckResult("exchange_rules", True, "Regras do simbolo carregadas"))

        _check_account_state(client, config, checks)

    except (BinanceFuturesAPIError, BinanceFuturesNetworkError, ValueError) as error:
        checks.append(CheckResult("binance", False, str(error), CheckSeverity.ERROR.value))

    _check_recovery_policy(config, checks)
    return _payload(checks)


def run_multi_symbol_preflight(config, global_checks: list[CheckResult] | None = None) -> dict:
    global_checks = list(global_checks or [])
    symbol_checks: dict[str, list[CheckResult]] = {}

    _check_mode_environment(config, global_checks)
    _check_credentials(config, global_checks)

    profile_path = Path(config.symbol_profiles_path)
    if not profile_path.exists():
        global_checks.append(
            CheckResult(
                "symbol_profiles",
                False,
                f"Arquivo de perfis multi-moedas nao encontrado: {profile_path}",
                CheckSeverity.ERROR.value,
            )
        )
        return _multi_payload(global_checks, symbol_checks)

    try:
        symbol_profiles = [
            profile
            for profile in load_symbol_profiles(profile_path, base_config=config)
            if profile.enabled
        ]
        global_checks.append(
            CheckResult(
                "symbol_profiles",
                True,
                f"{len(symbol_profiles)} perfis habilitados carregados",
            )
        )
    except Exception as error:
        global_checks.append(
            CheckResult("symbol_profiles", False, str(error), CheckSeverity.ERROR.value)
        )
        return _multi_payload(global_checks, symbol_checks)

    try:
        load_portfolio_risk_settings(config.portfolio_risk_path)
        global_checks.append(CheckResult("portfolio_risk", True, "Risco global carregado"))
    except Exception as error:
        global_checks.append(
            CheckResult("portfolio_risk", False, str(error), CheckSeverity.ERROR.value)
        )

    try:
        load_strategy(config.strategy_module, config.strategy_class, config.strategy_params)
        global_checks.append(
            CheckResult(
                "global_strategy",
                True,
                f"Estrategia global carregada: {config.strategy_module}.{config.strategy_class}",
            )
        )
    except (StrategyLoadError, ImportError, ValueError) as error:
        global_checks.append(
            CheckResult("global_strategy", False, str(error), CheckSeverity.ERROR.value)
        )

    for profile in symbol_profiles:
        checks: list[CheckResult] = []
        symbol_checks[profile.symbol] = checks
        global_name = f"{config.strategy_module}.{config.strategy_class}"
        if profile.strategy_params:
            checks.append(
                CheckResult(
                    "profile_strategy_params_context",
                    True,
                    (
                        "strategy_params local sera entregue como contexto da "
                        f"estrategia global ativa: {global_name}"
                    ),
                    CheckSeverity.WARNING.value,
                )
            )
        _check_symbol_lock(profile.lock_path, checks)
        _check_manual_reconcile(profile.runtime_dir, profile.symbol, checks)

    if _has_critical_errors(global_checks) or any(
        _has_critical_errors(checks) for checks in symbol_checks.values()
    ):
        return _multi_payload(global_checks, symbol_checks)

    client = make_client(config, mutation_enabled=False)
    try:
        client.ping()
        global_checks.append(CheckResult("public_connection", True, "Conexao publica OK"))
        client.sync_time()
        account = client.account_info()
        global_checks.append(CheckResult("account_auth", True, "Autenticacao Futures OK"))
        _check_balance(account, global_checks)
        _check_position_mode(client, global_checks)
        exchange_info = client.exchange_info()

        for profile in symbol_profiles:
            checks = symbol_checks[profile.symbol]
            try:
                rules = extract_symbol_rules(exchange_info, profile.symbol)
                checks.append(CheckResult("exchange_rules", True, "Regras do simbolo carregadas"))
                if profile.position_notional_usdt < float(rules.min_notional):
                    checks.append(
                        CheckResult(
                            "min_notional",
                            False,
                            (
                                f"Notional {profile.position_notional_usdt} menor que "
                                f"min_notional {rules.min_notional}"
                            ),
                            CheckSeverity.ERROR.value,
                        )
                    )
                else:
                    checks.append(CheckResult("min_notional", True, "Notional atende min_notional"))
                _check_symbol_account_state(client, config, profile.symbol, checks)
            except (BinanceFuturesAPIError, BinanceFuturesNetworkError, ValueError) as error:
                checks.append(CheckResult("binance", False, str(error), CheckSeverity.ERROR.value))

    except (BinanceFuturesAPIError, BinanceFuturesNetworkError, ValueError) as error:
        global_checks.append(CheckResult("binance", False, str(error), CheckSeverity.ERROR.value))

    _check_recovery_policy(config, global_checks)
    return _multi_payload(global_checks, symbol_checks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    print_result(run_preflight(), json_only=args.json)


def _check_symbol_profiles(config, checks: list[CheckResult]):
    """Resolve symbol profiles using the same path the Governor will use.

    Returns the single SymbolTradingConfig when a profiles file is present and valid,
    or None when the file is absent (Governor falls back to AppConfig).
    Appends check results and returns None on any error.
    """
    from pathlib import Path

    profile_path = Path(config.symbol_profiles_path)
    if not profile_path.exists():
        return None

    try:
        symbol_configs = resolve_governor_symbol_configs(config)
    except Exception as error:
        checks.append(
            CheckResult(
                "symbol_profiles",
                False,
                str(error),
                CheckSeverity.ERROR.value,
            )
        )
        return None

    enabled = [sc for sc in symbol_configs if sc.enabled]
    if len(enabled) != 1:
        checks.append(
            CheckResult(
                "symbol_profiles",
                False,
                (
                    f"Modo single-symbol requer exatamente 1 perfil habilitado; "
                    f"encontrado: {len(enabled)}"
                ),
                CheckSeverity.ERROR.value,
            )
        )
        return None

    profile = enabled[0]
    if profile.symbol != config.symbol:
        checks.append(
            CheckResult(
                "symbol_profiles",
                False,
                (
                    f"Símbolo do perfil ({profile.symbol}) difere do símbolo "
                    f"configurado ({config.symbol})"
                ),
                CheckSeverity.ERROR.value,
            )
        )
        return None

    checks.append(
        CheckResult(
            "symbol_profiles",
            True,
            f"1 perfil canônico carregado: {profile.symbol}",
        )
    )
    return profile


def _check_mode_environment(config, checks: list[CheckResult]) -> None:
    if (
        config.execution_mode is ExecutionMode.TESTNET_LIVE
        and config.environment.value != "TESTNET"
    ):
        checks.append(
            CheckResult(
                "mode_environment",
                False,
                "TESTNET_LIVE precisa usar TESTNET",
                CheckSeverity.ERROR.value,
            )
        )
        return

    if config.execution_mode is ExecutionMode.REAL_LIVE and config.environment.value != "REAL":
        checks.append(
            CheckResult(
                "mode_environment",
                False,
                "REAL_LIVE precisa usar REAL",
                CheckSeverity.ERROR.value,
            )
        )
        return

    checks.append(CheckResult("mode_environment", True, "Modo e ambiente coerentes"))

    if config.execution_mode is ExecutionMode.PAPER_LIVE:
        checks.append(
            CheckResult(
                "paper_live",
                True,
                "PAPER_LIVE usa dados ao vivo com execucao simulada; mutacoes ficam bloqueadas",
                CheckSeverity.INFO.value,
            )
        )

    if config.multi_symbol_experimental:
        checks.append(
            CheckResult(
                "multi_symbol",
                True,
                "Multi-symbol ainda experimental. Operando apenas o primeiro simbolo.",
                CheckSeverity.WARNING.value,
            )
        )


def _check_credentials(config, checks: list[CheckResult]) -> None:
    if not config.api_key or not config.api_secret:
        checks.append(
            CheckResult(
                "api_credentials",
                False,
                "API key/secret ausentes para o ambiente selecionado",
                CheckSeverity.ERROR.value,
            )
        )
        return

    checks.append(CheckResult("api_credentials", True, "Chaves encontradas"))


def _check_strategy(config, checks: list[CheckResult]) -> None:
    try:
        load_strategy(config.strategy_module, config.strategy_class, config.strategy_params)
    except (StrategyLoadError, ImportError, ValueError) as error:
        checks.append(CheckResult("strategy", False, str(error), CheckSeverity.ERROR.value))
        return

    checks.append(CheckResult("strategy", True, "Estrategia importada e valida"))


def _check_symbol(config, checks: list[CheckResult]) -> None:
    checks.append(
        CheckResult(
            "symbol",
            bool(config.symbol),
            "Symbol configurado" if config.symbol else "Symbol vazio",
            CheckSeverity.INFO.value if config.symbol else CheckSeverity.ERROR.value,
        )
    )


def _check_lock(config, checks: list[CheckResult]) -> None:
    lock_path = (
        Path(config.runtime_dir) / f"alphora-{config.symbol.lower()}-{config.environment_key}.lock"
    )

    if not lock_path.exists():
        checks.append(CheckResult("process_lock", True, "Nenhum lock ativo encontrado"))
        return

    lock_data = _read_lock_data(lock_path)
    pid = coerce_pid(lock_data.get("pid"))

    if pid is not None and not is_pid_running(pid):
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as error:
            checks.append(
                CheckResult(
                    "process_lock",
                    False,
                    (
                        "Lock antigo encontrado, mas nao foi possivel remover: "
                        f"{lock_path} | {error}"
                    ),
                    CheckSeverity.ERROR.value,
                )
            )
            return

        checks.append(
            CheckResult(
                "process_lock",
                True,
                f"Lock antigo removido automaticamente: {lock_path}",
                CheckSeverity.WARNING.value,
            )
        )
        return

    if pid is None:
        message = (
            f"Lock existente sem PID valido: {lock_path}. "
            "Remova manualmente apenas se tiver certeza de que nao ha robo rodando."
        )
    else:
        message = f"Lock existente: {lock_path} | pid={pid}"

    checks.append(
        CheckResult(
            "process_lock",
            False,
            message,
            CheckSeverity.ERROR.value,
        )
    )


def _check_symbol_lock(lock_path: str, checks: list[CheckResult]) -> None:
    path = Path(lock_path)
    if not path.exists():
        checks.append(CheckResult("process_lock", True, "Nenhum lock ativo encontrado"))
        return

    lock_data = _read_lock_data(path)
    pid = coerce_pid(lock_data.get("pid"))
    if pid is not None and not is_pid_running(pid):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            checks.append(
                CheckResult(
                    "process_lock",
                    False,
                    f"Lock antigo encontrado, mas nao foi possivel remover: {path} | {error}",
                    CheckSeverity.ERROR.value,
                )
            )
            return
        checks.append(
            CheckResult(
                "process_lock",
                True,
                f"Lock antigo removido automaticamente: {path}",
                CheckSeverity.WARNING.value,
            )
        )
        return

    message = f"Lock existente: {path} | pid={pid}" if pid else f"Lock sem PID valido: {path}"
    checks.append(CheckResult("process_lock", False, message, CheckSeverity.ERROR.value))


def _check_manual_reconcile(runtime_dir: str, symbol: str, checks: list[CheckResult]) -> None:
    path = manual_reconcile_path(runtime_dir, symbol)
    if not path.exists():
        checks.append(CheckResult("manual_reconcile", True, "Nenhum reconcile manual pendente"))
        return
    checks.append(
        CheckResult(
            "manual_reconcile",
            False,
            f"Reconcile manual pendente bloqueia novas entradas: {path}",
            CheckSeverity.ERROR.value,
        )
    )


def _read_lock_data(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def _check_balance(account: dict, checks: list[CheckResult]) -> None:
    usdt = extract_usdt_asset(account)
    if usdt is None:
        checks.append(
            CheckResult("balance", False, "Saldo USDT nao encontrado", CheckSeverity.ERROR.value)
        )
        return

    checks.append(
        CheckResult(
            "balance",
            True,
            f"USDT available={usdt.get('availableBalance', '0')} margin={usdt.get('marginBalance', '0')}",
        )
    )


def _check_position_mode(client, checks: list[CheckResult]) -> None:
    mode = client.get_position_mode()
    if mode.get("dualSidePosition") in {True, "true", "TRUE", "True"}:
        checks.append(
            CheckResult(
                "position_mode",
                False,
                "Conta esta em Hedge Mode. Alphora exige One-way/BOTH.",
                CheckSeverity.ERROR.value,
            )
        )
        return

    checks.append(CheckResult("position_mode", True, "One-way Mode/BOTH"))


def _check_account_state(client, config, checks: list[CheckResult]) -> None:
    positions = client.position_risk(config.symbol)
    open_positions = [item for item in positions if float(item.get("positionAmt", "0")) != 0]

    if open_positions:
        checks.append(
            CheckResult(
                "open_position",
                True,
                f"Existe posicao aberta em {config.symbol}: {open_positions}",
                CheckSeverity.WARNING.value,
            )
        )
    else:
        checks.append(CheckResult("open_position", True, "Nenhuma posicao aberta"))

    normal_orders = client.open_orders(config.symbol)
    algo_orders = client.open_algo_orders(config.symbol)

    robot_normal = [
        order
        for order in normal_orders
        if is_robot_client_id(config, extract_normal_client_order_id(order))
    ]

    robot_algo = [
        order for order in algo_orders if is_robot_client_id(config, extract_algo_client_id(order))
    ]

    non_robot = len(normal_orders) + len(algo_orders) - len(robot_normal) - len(robot_algo)

    checks.append(
        CheckResult(
            "robot_orders",
            True,
            f"Ordens do robo: normal={len(robot_normal)} algo={len(robot_algo)}",
            CheckSeverity.WARNING.value if robot_normal or robot_algo else CheckSeverity.INFO.value,
        )
    )

    if non_robot:
        checks.append(
            CheckResult(
                "non_robot_orders",
                True,
                f"Existem {non_robot} ordens nao pertencentes ao robo no simbolo",
                CheckSeverity.WARNING.value,
            )
        )


def _check_symbol_account_state(
    client,
    config,
    symbol: str,
    checks: list[CheckResult],
) -> None:
    positions = client.position_risk(symbol)
    open_positions = [item for item in positions if float(item.get("positionAmt", "0")) != 0]
    if open_positions:
        checks.append(
            CheckResult(
                "open_position",
                True,
                f"Existe posicao aberta em {symbol}: {open_positions}",
                CheckSeverity.WARNING.value,
            )
        )
    else:
        checks.append(CheckResult("open_position", True, "Nenhuma posicao aberta"))

    normal_orders = client.open_orders(symbol)
    algo_orders = client.open_algo_orders(symbol)
    robot_normal = [
        order
        for order in normal_orders
        if is_robot_client_id(config, extract_normal_client_order_id(order))
    ]
    robot_algo = [
        order for order in algo_orders if is_robot_client_id(config, extract_algo_client_id(order))
    ]
    checks.append(
        CheckResult(
            "robot_orders",
            True,
            f"Ordens do robo: normal={len(robot_normal)} algo={len(robot_algo)}",
            CheckSeverity.WARNING.value if robot_normal or robot_algo else CheckSeverity.INFO.value,
        )
    )


def _check_recovery_policy(config, checks: list[CheckResult]) -> None:
    if config.recovery_action is RecoveryAction.CLOSE:
        checks.append(
            CheckResult(
                "recovery_action",
                True,
                "RecoveryAction=CLOSE pode fechar posicao encontrada no boot",
                CheckSeverity.WARNING.value,
            )
        )

    if config.protection_policy is ProtectionPolicy.CLOSE_ON_UNPROTECTED:
        checks.append(
            CheckResult(
                "protection_policy",
                True,
                "ProtectionPolicy=CLOSE_ON_UNPROTECTED pode fechar posicao sem protecao",
                CheckSeverity.WARNING.value,
            )
        )


def _payload(checks: list[CheckResult]) -> dict:
    critical_errors = [
        check for check in checks if not check.ok and check.severity == CheckSeverity.ERROR.value
    ]

    warnings = [check for check in checks if check.severity == CheckSeverity.WARNING.value]

    ok = not critical_errors

    if critical_errors:
        summary = "Preflight encontrou erro critico. Inicio bloqueado."
    elif warnings:
        summary = "Preflight encontrou avisos. Revise antes de iniciar."
    else:
        summary = "Preflight OK."

    return {
        "ok": ok,
        "checks": [check.__dict__ for check in checks],
        "summary": summary,
    }


def _multi_payload(
    global_checks: list[CheckResult],
    symbol_checks: dict[str, list[CheckResult]],
) -> dict:
    critical_global = _has_critical_errors(global_checks)
    critical_symbols = {
        symbol: checks for symbol, checks in symbol_checks.items() if _has_critical_errors(checks)
    }
    warnings = [check for check in global_checks if check.severity == CheckSeverity.WARNING.value]
    for checks in symbol_checks.values():
        warnings.extend(check for check in checks if check.severity == CheckSeverity.WARNING.value)
    ok = not critical_global and not critical_symbols
    approved_symbols = [
        symbol for symbol in sorted(symbol_checks) if symbol not in critical_symbols
    ]
    if critical_global:
        summary = "Preflight multi-symbol encontrou erro global. Inicio bloqueado."
    elif critical_symbols:
        blocked = ", ".join(sorted(critical_symbols))
        summary = f"Preflight multi-symbol bloqueou simbolos: {blocked}"
    elif warnings:
        summary = "Preflight multi-symbol encontrou avisos. Revise antes de iniciar."
    else:
        summary = "Preflight multi-symbol OK"
    return {
        "ok": ok,
        "global_ok": not critical_global,
        "approved_symbols": approved_symbols,
        "blocked_symbols": sorted(critical_symbols),
        "global_checks": [check.__dict__ for check in global_checks],
        "symbol_checks": {
            symbol: [check.__dict__ for check in checks]
            for symbol, checks in sorted(symbol_checks.items())
        },
        "summary": summary,
    }


def _has_critical_errors(checks: list[CheckResult]) -> bool:
    return any(not check.ok and check.severity == CheckSeverity.ERROR.value for check in checks)


if __name__ == "__main__":
    main()
