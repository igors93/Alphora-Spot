import argparse
import json
import uuid
from decimal import Decimal

from binance.futures_client import BinanceFuturesNetworkError
from config import ExecutionMode, load_config
from domain.symbol_rules import format_decimal
from futures.operational import write_manual_reconcile_required
from ops.cancel_robot_orders import (
    CONFIRM_CANCEL_ROBOT_ORDERS,
    _all_symbol_targets,
    _config_for_symbol,
    cancel_robot_orders,
)
from ops.common import json_default, make_client
from ops.position_legs import (
    extract_open_legs,
    is_hedge_mode,
    position_summary,
    unsupported_legs_reason,
)
from resilience.budgets import EMERGENCY_FLATTEN_BUDGET
from resilience.catalog import (
    EMERGENCY_FLATTEN_CONFIRMATION_ATTEMPTS,
    EMERGENCY_FLATTEN_CONFIRMATION_INTERVAL_SECONDS,
    EMERGENCY_FLATTEN_CONFIRMATION_MAX_TIME_SECONDS,
    EMERGENCY_FLATTEN_CONFIRMATION_REQUEST_TIMEOUT,
)
from resilience.polling import run_polling
from utils.journals import JournalRegistry


def emergency_flatten(
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
        # One failing symbol must not leave the remaining ones exposed.
        for target in targets:
            try:
                results.append(emergency_flatten(confirm=confirm, symbol=target.symbol))
            except Exception as error:
                results.append(
                    {
                        "symbol": target.symbol,
                        "error": f"{type(error).__name__}: {error}",
                        "message": "Falha ao zerar este simbolo; verifique manualmente.",
                    }
                )
        return {"results": results}
    if symbol:
        config = _config_for_symbol(config, symbol)
    journal = JournalRegistry().journal(config.trade_journal_path)
    client = make_client(config, mutation_enabled=config.mutations_enabled)
    if getattr(client, "spot_mode", False):
        return _spot_emergency_flatten(config, client, journal, confirm=confirm)
    # Read every leg and the account mode before any decision: a hedge-mode
    # account reports LONG and SHORT legs separately, and closing only the
    # first nonzero row would leave the other leg exposed.
    hedge_mode = is_hedge_mode(client.get_position_mode())
    try:
        legs = extract_open_legs(client.position_risk(config.symbol))
    except ValueError as error:
        result = {
            "dry_run": True,
            "blocked": True,
            "symbol": config.symbol,
            "error": str(error),
            "message": "Estado da posicao desconhecido; nenhuma ordem foi enviada.",
        }
        journal.append("emergency_flatten_invalid_position", **result)
        return result
    position = position_summary(legs)
    unsupported_reason = unsupported_legs_reason(legs, hedge_mode=hedge_mode)
    position_mode = "HEDGE" if hedge_mode else "ONE_WAY"

    required = (
        "EMERGENCY_FLATTEN_REAL"
        if config.execution_mode is ExecutionMode.REAL_LIVE
        else "EMERGENCY_FLATTEN"
    )
    if not config.mutations_enabled or confirm != required:
        result = {
            "dry_run": True,
            "symbol": config.symbol,
            "required_confirmation": required,
            "position": position,
            "open_legs": legs,
            "position_mode": position_mode,
            "message": "Nenhuma mutacao enviada. Confirme explicitamente para executar.",
        }
        if unsupported_reason is not None:
            result["refused_reason"] = unsupported_reason
            result["message"] = (
                f"Nenhuma mutacao enviada. {_manual_flatten_procedure(unsupported_reason)}"
            )
        journal.append("emergency_flatten_preview", **result)
        return result

    if not legs:
        return _finish_flatten(config, client, journal, position=position, order=None)

    if unsupported_reason is not None:
        reconcile_path = write_manual_reconcile_required(
            runtime_dir=config.runtime_dir,
            symbol=config.symbol,
            reason=f"emergency_flatten_refused_{unsupported_reason.lower()}",
            context={"position_mode": position_mode, "open_legs": legs},
            last_known_position=position,
        )
        result = {
            "dry_run": True,
            "blocked": True,
            "symbol": config.symbol,
            "position": position,
            "open_legs": legs,
            "position_mode": position_mode,
            "refused_reason": unsupported_reason,
            "manual_reconcile_path": str(reconcile_path),
            "message": (
                f"Nenhuma ordem foi enviada. {_manual_flatten_procedure(unsupported_reason)}"
            ),
        }
        journal.append("emergency_flatten_refused", **result)
        return result

    leg = legs[0]
    side = "SELL" if leg["direction"] == "LONG" else "BUY"
    order = client.new_order(
        symbol=config.symbol,
        side=side,
        type="MARKET",
        quantity=format_decimal(Decimal(leg["quantity"])),
        reduceOnly="true",
        newOrderRespType="RESULT",
    )
    journal.append("emergency_flatten_order_sent", position=position, order=order)
    if not _wait_until_flat(client, config.symbol):
        try:
            remaining_legs = extract_open_legs(client.position_risk(config.symbol))
            position_check_error = None
        except Exception as error:
            remaining_legs = []
            position_check_error = f"{type(error).__name__}: {error}"
        reconcile_path = write_manual_reconcile_required(
            runtime_dir=config.runtime_dir,
            symbol=config.symbol,
            reason="position_close_not_confirmed",
            context={
                "order": order,
                "open_legs": remaining_legs,
                "position_check_error": position_check_error,
            },
            last_known_position=(
                position_summary(remaining_legs) if position_check_error is None else position
            ),
        )
        result = {
            "dry_run": False,
            "symbol": config.symbol,
            "order": order,
            "cancel_result": None,
            "blocked": True,
            "open_legs": remaining_legs,
            "position_check_error": position_check_error,
            "manual_reconcile_path": str(reconcile_path),
            "message": "Fechamento nao confirmado; ordens restantes nao foram canceladas.",
        }
        journal.append("position_close_not_confirmed", **result)
        return result

    return _finish_flatten(config, client, journal, position=position, order=order)


def _spot_emergency_flatten(config, client, journal, *, confirm: str) -> dict:
    try:
        history = client.reconcile_spot_history(config.symbol)
        legs = extract_open_legs(client.position_risk(config.symbol))
        account = client.account_info()
    except Exception as error:
        result = {
            "symbol": config.symbol, "blocked": True, "dry_run": True,
            "reason": f"SPOT_RECONCILIATION_FAILED: {error}",
        }
        journal.append("emergency_flatten_spot_blocked", **result)
        return result

    position = position_summary(legs)
    base_asset = config.symbol.removesuffix("USDT")
    asset = next(
        (row for row in account.get("assets", []) if row.get("asset") == base_asset),
        {},
    )
    free = Decimal(str(asset.get("free", "0")))
    locked = Decimal(str(asset.get("locked", "0")))
    owned = client.spot_holdings.quantity(config.symbol)
    manual_balance = max(Decimal("0"), free + locked - owned)
    required = (
        "EMERGENCY_FLATTEN_REAL" if config.execution_mode is ExecutionMode.REAL_LIVE
        else "EMERGENCY_FLATTEN"
    )
    if not config.mutations_enabled or confirm != required:
        result = {
            "symbol": config.symbol, "dry_run": True,
            "required_confirmation": required, "position": position,
            "owned_quantity": str(owned), "free_quantity": str(free),
            "locked_quantity": str(locked), "manual_balance_untouched": str(manual_balance),
            "history": history,
        }
        journal.append("emergency_flatten_spot_preview", **result)
        return result

    try:
        client.cancel_all_open_orders(config.symbol)
        client.cancel_all_open_algo_orders(config.symbol)
        remaining = [
            order for order in client.open_orders(config.symbol) + client.open_algo_orders(config.symbol)
            if client._is_spot_bot_order_id(str(
                order.get("clientOrderId") or order.get("clientAlgoId") or ""
            ))
        ]
        if remaining:
            raise ValueError("Ordens do bot continuam abertas apos cancelamento")
        account = client.account_info()
        asset = next(
            (row for row in account.get("assets", []) if row.get("asset") == base_asset),
            {},
        )
        free = Decimal(str(asset.get("free", "0")))
        negotiable = min(client.spot_holdings.quantity(config.symbol), free)
        if negotiable < owned and manual_balance > 0:
            raise ValueError("Saldo livre misto com ativo manual; fechamento automatico recusado")
        if negotiable <= 0 and owned > 0:
            raise ValueError("Saldo do bot bloqueado; fechamento automatico recusado")
        if owned <= 0:
            return _finish_flatten(config, client, journal, position=position, order=None)

        client_order_id = (
            f"{config.bot_order_prefix}_EMERGENCY_{uuid.uuid4().hex[:10].upper()}"[:36]
        )
        try:
            order = client.new_order(
                symbol=config.symbol, side="SELL", type="MARKET",
                quantity=format_decimal(negotiable), reduceOnly="true",
                newClientOrderId=client_order_id,
            )
        except BinanceFuturesNetworkError:
            order = client.query_order(
                symbol=config.symbol, orig_client_order_id=client_order_id,
            )
        if str(order.get("status", "")).upper() != "FILLED":
            raise ValueError(f"Venda Spot nao confirmada: {order.get('status')}")
        if not _wait_until_flat(client, config.symbol):
            raise ValueError("Venda parcial; saldo do bot ainda aberto")
        journal.append("emergency_flatten_spot_sell_confirmed", order=order, position=position)
        return _finish_flatten(config, client, journal, position=position, order=order)
    except Exception as error:
        reconcile_path = write_manual_reconcile_required(
            runtime_dir=config.runtime_dir, symbol=config.symbol,
            reason="spot_emergency_flatten_unconfirmed",
            context={"error": str(error), "position": position},
            last_known_position=position,
        )
        result = {
            "symbol": config.symbol, "blocked": True, "dry_run": False,
            "reason": str(error), "manual_reconcile_path": str(reconcile_path),
            "manual_balance_untouched": str(manual_balance),
        }
        journal.append("emergency_flatten_spot_unconfirmed", **result)
        return result


def _finish_flatten(config, client, journal, *, position: dict, order: dict | None) -> dict:
    """Cancel orphan robot orders and confirm the account is still flat."""
    try:
        cancel_result = cancel_robot_orders(
            confirm=CONFIRM_CANCEL_ROBOT_ORDERS,
            symbol=config.symbol,
        )
    except Exception as error:
        cancel_result = {"error": f"{type(error).__name__}: {error}"}

    try:
        remaining_legs = extract_open_legs(client.position_risk(config.symbol))
        position_check_error = None
    except Exception as error:
        remaining_legs = []
        position_check_error = f"{type(error).__name__}: {error}"

    if (
        cancel_result.get("blocked")
        or cancel_result.get("dry_run") is not False
        or cancel_result.get("error")
        or remaining_legs
        or position_check_error
    ):
        reconcile_path = write_manual_reconcile_required(
            runtime_dir=config.runtime_dir,
            symbol=config.symbol,
            reason="emergency_flatten_cleanup_unconfirmed",
            context={
                "order": order,
                "cancel_result": cancel_result,
                "open_legs": remaining_legs,
                "position_check_error": position_check_error,
            },
            last_known_position=(
                position_summary(remaining_legs) if position_check_error is None else position
            ),
        )
        result = {
            "dry_run": False,
            "blocked": True,
            "symbol": config.symbol,
            "order": order,
            "cancel_result": cancel_result,
            "open_legs": remaining_legs,
            "position_check_error": position_check_error,
            "manual_reconcile_path": str(reconcile_path),
            "message": "Cancelamento ou estado flat nao confirmado; verifique a conta manualmente.",
        }
        journal.append("emergency_flatten_cleanup_unconfirmed", **result)
        return result

    result = {
        "dry_run": False,
        "symbol": config.symbol,
        "order": order,
        "cancel_result": cancel_result,
    }
    if order is None:
        result.update(position=position, message="Sem posicao aberta; ordens do robo canceladas")
        journal.append("emergency_flatten_no_position", **result)
    else:
        journal.append("emergency_flatten_completed", **result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            emergency_flatten(
                confirm=args.confirm,
                symbol=args.symbol or None,
                all_symbols=args.all,
            ),
            indent=2,
            default=json_default,
        )
    )


def _manual_flatten_procedure(reason: str) -> str:
    return (
        f"Flatten automatico recusado ({reason}): o Alphora so zera posicao One-way "
        "(positionSide=BOTH) com uma unica ordem reduce-only. Procedimento manual: na "
        "Binance Futures, feche cada perna listada em open_legs (em Hedge Mode, ordem "
        "MARKET do lado oposto com o positionSide da perna), confirme que todas as "
        "pernas ficaram zeradas, cancele as ordens restantes do simbolo e so entao "
        "resolva o manual_reconcile_required."
    )


def _wait_until_flat(
    client,
    symbol: str,
    *,
    attempts: int = EMERGENCY_FLATTEN_CONFIRMATION_ATTEMPTS,
    delay_seconds: float = EMERGENCY_FLATTEN_CONFIRMATION_INTERVAL_SECONDS,
) -> bool:
    def _check_flat() -> bool:
        try:
            return not extract_open_legs(
                client.position_risk(
                    symbol,
                    timeout=EMERGENCY_FLATTEN_CONFIRMATION_REQUEST_TIMEOUT,
                )
            )
        except ValueError:
            return False

    return run_polling(
        _check_flat,
        attempts=attempts,
        interval_seconds=delay_seconds,
        retry_if_result=lambda flat: not flat,
        fallback=False,
        delay_before_first_attempt=False,
        retry_exceptions=(BinanceFuturesNetworkError,),
        policy_name="emergency_flatten_wait_until_flat",
        budget=EMERGENCY_FLATTEN_BUDGET,
        budget_key=f"emergency-flatten:{symbol}",
        max_time_seconds=EMERGENCY_FLATTEN_CONFIRMATION_MAX_TIME_SECONDS,
    )


if __name__ == "__main__":
    main()
