import argparse
import json
from decimal import Decimal

from config import ProtectionPolicy, load_config
from domain.models import FuturesPosition, PositionDirection
from execution.engine import FuturesTradingEngine
from futures.exchange_rules import extract_symbol_rules
from futures.operational import OperationalState, StopMode
from futures.risk_manager import FuturesRiskManager
from ops.cancel_robot_orders import _all_symbol_targets, _config_for_symbol
from ops.common import (
    extract_algo_client_id,
    extract_normal_client_order_id,
    is_robot_client_id,
    json_default,
    make_client,
    request_governor_stop,
    request_stop,
)
from ops.position_legs import (
    extract_open_legs,
    is_hedge_mode,
    position_summary,
    unsupported_legs_reason,
)

CONFIRM_SAFE_STOP = "SAFE_STOP"


def operational_stop(
    *,
    mode: str,
    confirm: str = "",
    symbol: str | None = None,
    all_symbols: bool = False,
    governor: bool = False,
    preview: bool = False,
) -> dict:
    config = load_config()
    stop_mode = StopMode(str(mode).upper())
    client = make_client(config, mutation_enabled=config.mutations_enabled)
    targets, scan_errors = _targets(config, client, symbol=symbol, all_symbols=all_symbols)
    # Position mode is account-wide: read it once, before judging any leg.
    hedge_mode = is_hedge_mode(client.get_position_mode())
    results = list(scan_errors)
    for target_config in targets:
        try:
            results.append(
                _evaluate_target(
                    target_config,
                    client,
                    mode=stop_mode,
                    confirm=confirm,
                    preview=preview,
                    hedge_mode=hedge_mode,
                )
            )
        except Exception as error:
            results.append(
                {
                    "symbol": target_config.symbol,
                    "blocked": True,
                    "state": OperationalState.MANUAL_RECONCILE_REQUIRED.value,
                    "error": f"{type(error).__name__}: {error}",
                    "reason": "Estado do simbolo nao confirmado; verifique a conta manualmente.",
                }
            )
    blocked = [item for item in results if item.get("blocked")]

    stop_request = None
    if not preview and not blocked:
        if governor or all_symbols or config.multi_symbol_enabled:
            stop_request = str(request_governor_stop(config.runtime_dir))
        else:
            stop_request = str(request_stop(config.runtime_dir))

    return {
        "ok": not blocked,
        "mode": stop_mode.value,
        "preview": preview,
        "stop_request": stop_request,
        "results": results,
        "summary": _summary(stop_mode, results, stop_request),
    }


def _evaluate_target(
    config,
    client,
    *,
    mode: StopMode,
    confirm: str,
    preview: bool,
    hedge_mode: bool,
) -> dict:
    if getattr(client, "spot_mode", False):
        client.reconcile_spot_history(config.symbol)
    normal_orders = client.open_orders(config.symbol)
    algo_orders = client.open_algo_orders(config.symbol)
    legs = extract_open_legs(client.position_risk(config.symbol))
    position = {"symbol": config.symbol, **position_summary(legs)}
    unsupported_reason = unsupported_legs_reason(legs, hedge_mode=hedge_mode)
    robot_normal = [
        order
        for order in normal_orders
        if is_robot_client_id(config, extract_normal_client_order_id(order))
    ]
    robot_algo = [
        order for order in algo_orders if is_robot_client_id(config, extract_algo_client_id(order))
    ]
    protections = [order for order in robot_algo if _is_protection_order(order)]
    protected = not position["is_open"]
    if position["is_open"] and unsupported_reason is None:
        protected = _audit_protections(config, client, position, robot_algo)
    non_robot_count = len(normal_orders) + len(algo_orders) - len(robot_normal) - len(robot_algo)

    result = {
        "symbol": config.symbol,
        "state": (
            OperationalState.POSITION_OPEN_PROTECTED.value
            if protected and position["is_open"]
            else OperationalState.POSITION_OPEN_UNPROTECTED.value
            if position["is_open"]
            else OperationalState.FLAT_CONFIRMED.value
        ),
        "position": position,
        "open_legs": legs,
        "position_mode": "HEDGE" if hedge_mode else "ONE_WAY",
        "protections_found": protections,
        "robot_normal_orders": robot_normal,
        "robot_algo_orders": robot_algo,
        "non_robot_orders_count": non_robot_count,
        "action": _action_text(mode, position, protected),
        "blocked": False,
        "reason": "",
    }

    if unsupported_reason is not None:
        # Protection checks and repairs assume one One-way leg; with other legs
        # they could report an unprotected leg as protected.
        result.update(
            blocked=True,
            state=OperationalState.MANUAL_RECONCILE_REQUIRED.value,
            refused_reason=unsupported_reason,
            action="Bloquear stop ate reconciliar manualmente todas as pernas.",
            reason=(
                f"Stop bloqueado ({unsupported_reason}): o Alphora so avalia posicao "
                "One-way (positionSide=BOTH) com uma unica perna. Verifique na Binance "
                "a protecao de cada perna listada em open_legs antes de parar o robo."
            ),
        )
        return result

    if robot_normal or (not position["is_open"] and robot_algo):
        result.update(
            blocked=True,
            reason=(
                "Ordens do robo ainda abertas; cancele-as com ops.cancel_robot_orders "
                "ou use ops.emergency_flatten antes de parar."
            ),
            action="Confirmar cancelamento das ordens pendentes antes do stop.",
        )
        return result

    if mode is StopMode.STOP_KEEP_PROTECTIONS:
        if position["is_open"] and not protected:
            result.update(
                blocked=True,
                state=OperationalState.STOP_BLOCKED_UNPROTECTED.value,
                reason="Posicao aberta sem stop/take validos; stop sem reparo bloqueado.",
            )
        return result

    if mode is StopMode.SAFE_STOP and position["is_open"] and not protected:
        if (
            config.protection_policy is ProtectionPolicy.AUTO_REPAIR
            and config.mutations_enabled
            and confirm == CONFIRM_SAFE_STOP
            and not preview
        ):
            repaired = _attempt_protection_repair(config, client, position)
            result["repair_attempted"] = True
            result["repair_ok"] = repaired
            if repaired:
                result["state"] = OperationalState.STOPPED_WITH_PROTECTION.value
                return result

        result.update(
            blocked=True,
            state=OperationalState.STOP_BLOCKED_UNPROTECTED.value,
            reason="Parada segura bloqueada: posicao aberta sem protecoes validas.",
        )
        return result

    if mode is StopMode.SAFE_STOP and not position["is_open"]:
        result["state"] = OperationalState.STOPPED_FLAT.value

    return result


def _attempt_protection_repair(config, client, position_payload: dict) -> bool:
    engine, position = _protection_engine(config, client, position_payload)
    robot_algo = [
        order
        for order in client.open_algo_orders(config.symbol)
        if is_robot_client_id(config, extract_algo_client_id(order))
    ]
    audit = engine._audit_protection_orders(
        position=position,
        robot_algo_orders=robot_algo,
        reason="safe_stop",
    )
    if audit.ok:
        return True
    engine._handle_unprotected_position(
        position=position,
        audit_result=audit,
        reason="safe_stop",
        robot_algo_orders=robot_algo,
    )
    robot_algo = [
        order
        for order in client.open_algo_orders(config.symbol)
        if is_robot_client_id(config, extract_algo_client_id(order))
    ]
    audit = engine._audit_protection_orders(
        position=position,
        robot_algo_orders=robot_algo,
        reason="safe_stop_after_repair",
    )
    return audit.ok


def _audit_protections(config, client, position_payload: dict, robot_algo: list[dict]) -> bool:
    engine, position = _protection_engine(config, client, position_payload)
    return engine._audit_protection_orders(
        position=position,
        robot_algo_orders=robot_algo,
        reason="operational_stop",
    ).ok


def _protection_engine(config, client, position_payload: dict):
    engine = FuturesTradingEngine(
        config=config,
        client=client,
        risk_manager=FuturesRiskManager(
            position_notional_usdt=config.position_notional_usdt,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        ),
    )
    engine.rules = extract_symbol_rules(client.exchange_info(), config.symbol)
    position = FuturesPosition(
        symbol=config.symbol,
        direction=PositionDirection(position_payload["direction"]),
        quantity=Decimal(str(position_payload["quantity"])),
        entry_price=Decimal(str(position_payload["entry_price"])),
        unrealized_pnl=Decimal("0"),
    )
    return engine, position


def _targets(config, client, *, symbol: str | None, all_symbols: bool) -> tuple[list, list[dict]]:
    if all_symbols:
        targets, errors = _all_symbol_targets(config, client, include_robot_orders=True)
        return targets, [dict(error, blocked=True) for error in errors]
    if symbol:
        return [_config_for_symbol(config, symbol)], []
    return [config], []


def _is_protection_order(order: dict) -> bool:
    order_type = str(order.get("orderType") or order.get("type") or order.get("origType") or "")
    return order_type in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


def _action_text(mode: StopMode, position: dict, protected: bool) -> str:
    if mode is StopMode.STOP_KEEP_PROTECTIONS:
        return (
            "Solicitar stop sem fechar posicao nem cancelar protecoes."
            if protected
            else "Bloquear stop ate reconciliar protecoes."
        )
    if mode is StopMode.SAFE_STOP:
        if not position["is_open"]:
            return "Solicitar stop seguro; cancelar orfas apenas se configurado."
        return "Auditar/reparar protecoes antes de solicitar stop."
    return "Emergency flatten deve usar ops.emergency_flatten."


def _summary(mode: StopMode, results: list[dict], stop_request: str | None) -> str:
    blocked = [item["symbol"] for item in results if item.get("blocked")]
    if blocked:
        return f"{mode.value} bloqueado para: {', '.join(blocked)}"
    if stop_request:
        return f"{mode.value} aprovado; stop solicitado em {stop_request}"
    return f"{mode.value} preview OK"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=[item.value for item in StopMode])
    parser.add_argument("--confirm", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--governor", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            operational_stop(
                mode=args.mode,
                confirm=args.confirm,
                symbol=args.symbol or None,
                all_symbols=args.all,
                governor=args.governor,
                preview=args.preview,
            ),
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )
    )


if __name__ == "__main__":
    main()
