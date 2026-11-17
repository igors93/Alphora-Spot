import argparse
import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

from config import load_config
from config.symbol_context import (
    make_symbol_order_prefix,
    resolve_governor_symbol_configs,
    symbol_config_from_app_config,
)
from ops.common import (
    extract_algo_client_id,
    extract_normal_client_order_id,
    is_robot_client_id,
    json_default,
    make_client,
)
from ops.position_legs import extract_open_legs

CONFIRM_CANCEL_ROBOT_ORDERS = "CANCEL_ROBOT_ORDERS"
CONFIRM_CANCEL_PROTECTIONS_WHILE_IN_POSITION = "CANCEL_PROTECTIONS_WHILE_IN_POSITION"


def cancel_robot_orders(
    *,
    confirm: str = "",
    symbol: str | None = None,
    all_symbols: bool = False,
) -> dict:
    config = load_config()
    if all_symbols:
        targets, results = _all_symbol_targets(
            config,
            make_client(config, mutation_enabled=False),
            include_robot_orders=True,
        )
        # One failing symbol must not leave the remaining ones untouched.
        for target in targets:
            try:
                results.append(_cancel_for_config(target, confirm=confirm))
            except Exception as error:
                results.append(
                    {
                        "symbol": target.symbol,
                        "error": f"{type(error).__name__}: {error}",
                        "message": "Falha ao cancelar ordens deste simbolo; verifique manualmente.",
                    }
                )
        return {"results": results}
    if symbol:
        config = _config_for_symbol(config, symbol)
    return _cancel_for_config(config, confirm=confirm)


def _cancel_for_config(config, *, confirm: str) -> dict:
    client = make_client(config, mutation_enabled=config.mutations_enabled)
    normal_orders = client.open_orders(config.symbol)
    algo_orders = client.open_algo_orders(config.symbol)
    positions = client.position_risk(config.symbol)
    try:
        position_open = _has_open_position(positions)
    except ValueError as error:
        return {
            "dry_run": True,
            "blocked": True,
            "symbol": config.symbol,
            "reason": str(error),
            "message": "Estado da posicao desconhecido; nenhuma ordem foi cancelada.",
        }
    robot_normal = [
        order
        for order in normal_orders
        if is_robot_client_id(config, extract_normal_client_order_id(order))
    ]
    robot_algo = [
        order for order in algo_orders if is_robot_client_id(config, extract_algo_client_id(order))
    ]
    protection_orders = [order for order in robot_algo if _is_protection_order(order)]

    if (
        position_open
        and protection_orders
        and confirm != CONFIRM_CANCEL_PROTECTIONS_WHILE_IN_POSITION
    ):
        return {
            "dry_run": True,
            "blocked": True,
            "symbol": config.symbol,
            "required_confirmation": CONFIRM_CANCEL_PROTECTIONS_WHILE_IN_POSITION,
            "warning": "Isso pode remover stop/take e deixar posicao desprotegida.",
            "would_cancel_normal": robot_normal,
            "would_cancel_algo": robot_algo,
        }

    valid_confirmation = confirm in {
        CONFIRM_CANCEL_ROBOT_ORDERS,
        CONFIRM_CANCEL_PROTECTIONS_WHILE_IN_POSITION,
    }
    if not config.mutations_enabled or not valid_confirmation:
        if position_open and protection_orders:
            required_confirmation = CONFIRM_CANCEL_PROTECTIONS_WHILE_IN_POSITION
        else:
            required_confirmation = CONFIRM_CANCEL_ROBOT_ORDERS
        return {
            "dry_run": True,
            "symbol": config.symbol,
            "required_confirmation": required_confirmation,
            "would_cancel_normal": robot_normal,
            "would_cancel_algo": robot_algo,
        }

    cancelled = []
    for order in robot_normal:
        cancelled.append(
            client.cancel_order(
                symbol=config.symbol,
                order_id=order.get("orderId"),
                orig_client_order_id=extract_normal_client_order_id(order) or None,
            )
        )
    for order in robot_algo:
        cancelled.append(
            client.cancel_algo_order(
                algo_id=order.get("algoId"),
                client_algo_id=extract_algo_client_id(order) or None,
            )
        )
    return {"dry_run": False, "symbol": config.symbol, "cancelled": cancelled}


def _has_open_position(positions: list[dict]) -> bool:
    return bool(extract_open_legs(positions))


def _is_protection_order(order: dict) -> bool:
    order_type = str(order.get("orderType") or order.get("type") or order.get("origType") or "")
    return order_type in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            cancel_robot_orders(
                confirm=args.confirm,
                symbol=args.symbol or None,
                all_symbols=args.all,
            ),
            indent=2,
            default=json_default,
        )
    )


def _load_profiles_for_ops(config):
    # The same symbols the Governor trades: the profile file whenever it exists
    # (regardless of ALPHORA_MULTI_SYMBOL_ENABLED), otherwise ALPHORA_SYMBOLS.
    return resolve_governor_symbol_configs(config)


def _all_symbol_targets(config, client, *, include_robot_orders: bool) -> tuple[list, list[dict]]:
    """Resolve every symbol an ``--all`` command must cover, plus account-scan errors.

    Targets are the Governor's symbols plus every symbol with an open position
    (and, with ``include_robot_orders``, a robot order) anywhere on the account, so
    a profile change or a missing flag cannot hide live exposure.
    """

    targets: dict[str, Any] = {}
    for profile in _load_profiles_for_ops(config):
        targets.setdefault(profile.symbol, _config_for_profile(config, profile))
    errors: list[dict] = []

    try:
        for row in client.position_risk(None):
            symbol = _row_symbol(row)
            if symbol and symbol not in targets and Decimal(str(row.get("positionAmt", "0"))):
                targets[symbol] = _config_for_symbol(config, symbol)
    except Exception as error:
        errors.append(_scan_error("position_risk", error))

    if include_robot_orders:
        scans = (
            ("open_orders", client.open_orders, extract_normal_client_order_id),
            ("open_algo_orders", client.open_algo_orders, extract_algo_client_id),
        )
        for source, fetch, client_id_of in scans:
            try:
                for order in fetch(None):
                    symbol = _row_symbol(order)
                    if not symbol or symbol in targets:
                        continue
                    target = _robot_order_target(config, symbol, client_id_of(order))
                    if target is not None:
                        targets[symbol] = target
            except Exception as error:
                errors.append(_scan_error(source, error))

    if not targets:
        errors.append(
            {
                "symbol": "*",
                "error": "Nenhum simbolo resolvido para --all",
                "message": "Nada foi verificado; confira perfis, ALPHORA_SYMBOLS e a conta.",
            }
        )
    return list(targets.values()), errors


def _robot_order_target(config, symbol: str, client_id: str):
    target = _config_for_symbol(config, symbol)
    if is_robot_client_id(target, client_id):
        return target
    # Non-first symbols default to a per-symbol prefix (see make_symbol_order_prefix).
    symbol_prefix = make_symbol_order_prefix(symbol)
    if client_id.upper().startswith(f"{symbol_prefix}_"):
        return replace(target, bot_order_prefix=symbol_prefix)
    return None


def _row_symbol(row: dict) -> str:
    return str(row.get("symbol") or "").upper().strip()


def _scan_error(source: str, error: Exception) -> dict:
    return {
        "symbol": "*",
        "error": f"{source}: {type(error).__name__}: {error}",
        "message": (
            "Varredura da conta falhou; simbolos fora dos perfis podem nao ter sido "
            "verificados. Confira a conta manualmente."
        ),
    }


def _config_for_symbol(config, symbol: str):
    normalized = symbol.upper().strip()
    for profile in _load_profiles_for_ops(config):
        if profile.symbol == normalized:
            return _config_for_profile(config, profile)
    return _config_for_profile(config, symbol_config_from_app_config(config, symbol=normalized))


def _config_for_profile(config, profile):
    return replace(
        config,
        symbol=profile.symbol,
        symbols=(profile.symbol,),
        margin_type=profile.margin_type,
        leverage=profile.leverage,
        position_notional_usdt=profile.position_notional_usdt,
        stop_loss_pct=profile.stop_loss_pct,
        take_profit_pct=profile.take_profit_pct,
        strategy_params=profile.strategy_params,
        bot_order_prefix=profile.bot_order_prefix,
        trade_journal_path=profile.journal_path,
        state_path=profile.state_path,
        runtime_dir=profile.runtime_dir,
        recovery_action=profile.recovery_action,
        protection_policy=profile.protection_policy,
        protection_working_type=profile.protection_working_type,
        max_daily_loss_usdt=profile.max_daily_loss_usdt,
        max_daily_trades=profile.max_daily_trades,
        max_consecutive_losses_guard=profile.max_consecutive_losses_guard,
        cooldown_after_loss_seconds=profile.cooldown_after_loss_seconds,
        max_position_hold_seconds=profile.max_position_hold_seconds,
        max_exposure_usdt=profile.max_exposure_usdt,
        exit_model=profile.exit_model,
        atr_period=profile.atr_period,
        atr_stop_multiplier=profile.atr_stop_multiplier,
        atr_take_multiplier=profile.atr_take_multiplier,
    )


if __name__ == "__main__":
    main()
