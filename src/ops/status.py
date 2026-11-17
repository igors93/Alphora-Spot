import argparse
import json
import sys
from decimal import Decimal
from typing import Any

from config import load_config
from config.symbol_context import resolve_governor_symbol_configs
from domain.symbol_rules import format_decimal
from futures.exchange_rules import extract_symbol_rules
from ops.common import (
    extract_algo_client_id,
    extract_normal_client_order_id,
    extract_usdt_asset,
    is_robot_client_id,
    json_default,
    make_client,
    mask_api_key,
)


def get_binance_status(*, include_raw_orders: bool = False) -> dict[str, Any]:
    config = load_config()
    client = make_client(config, mutation_enabled=False)
    symbols = _effective_symbols(config)
    status: dict[str, Any] = {
        "connection_ok": False,
        "execution_mode": config.execution_mode.value,
        "environment": config.environment.value,
        "operation_mode": _operation_mode(config, symbols),
        "symbol": config.symbol,
        "api_key": mask_api_key(config.api_key),
        "base_url": config.base_url,
        "checks": {},
        "balance": {},
        "position_mode": {},
        "symbols": {},
        "errors": [],
    }

    exchange_info = None
    account = None

    status["checks"]["ping"] = _check(lambda: client.ping())
    status["checks"]["server_time"] = _check(lambda: client.server_time())

    if status["checks"]["server_time"]["ok"]:
        client.sync_time()

    status["checks"]["auth_futures"] = _check(lambda: client.account_info())
    if status["checks"]["auth_futures"]["ok"]:
        account = status["checks"]["auth_futures"]["data"]
        status["balance"] = _balance_summary(account)

    status["checks"]["exchange_info"] = _check(lambda: client.exchange_info())
    if status["checks"]["exchange_info"]["ok"]:
        exchange_info = status["checks"]["exchange_info"]["data"]

    status["checks"]["position_mode"] = _check(lambda: client.get_position_mode())
    if status["checks"]["position_mode"]["ok"]:
        status["position_mode"] = status["checks"]["position_mode"]["data"]

    status["connection_ok"] = bool(
        status["checks"]["ping"]["ok"]
        and status["checks"]["server_time"]["ok"]
        and status["checks"]["auth_futures"]["ok"]
        and status["checks"]["exchange_info"]["ok"]
    )

    for symbol in symbols:
        status["symbols"][symbol] = _symbol_status(
            client=client,
            config=config,
            symbol=symbol,
            exchange_info=exchange_info,
            include_raw_orders=include_raw_orders,
        )

    return status


def format_status_summary(status: dict[str, Any]) -> str:
    lines = [
        "Status Binance Alphora",
        f"Conexao geral: {'OK' if status.get('connection_ok') else 'FALHA'}",
        (
            f"Modo: {status.get('execution_mode')} | "
            f"Ambiente: {status.get('environment')} | "
            f"Operacao: {status.get('operation_mode')}"
        ),
        f"Base URL: {status.get('base_url')}",
        _format_check_line("Ping publico", status["checks"].get("ping", {})),
        _format_check_line("Server time", status["checks"].get("server_time", {})),
        _format_check_line(
            "Autenticacao Futures",
            status["checks"].get("auth_futures", {}),
        ),
        _format_check_line("ExchangeInfo", status["checks"].get("exchange_info", {})),
        f"Position mode: {status.get('position_mode')}",
        (
            "Saldo USDT: "
            f"wallet={status.get('balance', {}).get('wallet_balance', '0')} | "
            f"available={status.get('balance', {}).get('available_balance', '0')} | "
            f"margin={status.get('balance', {}).get('margin_balance', '0')} | "
            f"unrealized_pnl={status.get('balance', {}).get('unrealized_pnl', '0')}"
        ),
        "",
        "Simbolos:",
    ]

    for symbol, data in status.get("symbols", {}).items():
        if data.get("error"):
            lines.append(f"- {symbol}: ERRO | {data['error']}")
            continue

        position = data.get("position", {})
        lines.append(
            f"- {symbol}: mark={data.get('mark_price')} "
            f"last={data.get('last_price')} | "
            f"min_notional={data.get('min_notional')} "
            f"min_qty={data.get('min_quantity')} "
            f"step={data.get('quantity_step')} "
            f"tick={data.get('price_tick')} "
            f"min_exec_qty={data.get('minimum_executable_quantity')}"
        )
        lines.append(
            f"  posicao={position.get('direction')} "
            f"qty={position.get('quantity')} "
            f"entry={position.get('entry_price')} "
            f"upnl={position.get('unrealized_pnl')} | "
            f"orders normal={data.get('normal_orders')} "
            f"algo={data.get('algo_orders')} "
            f"robot_normal={data.get('robot_normal_orders')} "
            f"robot_algo={data.get('robot_algo_orders')}"
        )

    return "\n".join(lines)


def main() -> None:
    _configure_stdout_encoding()

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Inclui ordens cruas no JSON. Por padrao o status retorna resumo.",
    )
    args = parser.parse_args()

    status = get_binance_status(include_raw_orders=args.raw)

    if args.json:
        _safe_print(_json_dumps(status))
        return

    _safe_print(format_status_summary(status))
    _safe_print("")
    _safe_print(_json_dumps(status))


def _json_dumps(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
        default=json_default,
    )


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoded = text.encode(
            sys.stdout.encoding or "utf-8",
            errors="backslashreplace",
        )
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.flush()


def _configure_stdout_encoding() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
    except Exception:
        pass


def _effective_symbols(config) -> list[str]:
    profiles = resolve_governor_symbol_configs(config)
    symbols = [profile.symbol for profile in profiles if profile.enabled]

    if symbols:
        return symbols

    return [config.symbol]


def _operation_mode(config, symbols: list[str]) -> str:
    if len(symbols) == 1:
        return "GOVERNOR_SINGLE_SYMBOL"

    return "GOVERNOR_MULTI_SYMBOL"


def _symbol_status(
    *,
    client,
    config,
    symbol: str,
    exchange_info: dict | None,
    include_raw_orders: bool,
) -> dict[str, Any]:
    try:
        mark_price = client.mark_price(symbol)
        last_price = client.ticker_price(symbol)
        last = Decimal(str(last_price.get("price", "0")))

        rules = extract_symbol_rules(exchange_info or client.exchange_info(), symbol)
        positions = client.position_risk(symbol)
        normal_orders = client.open_orders(symbol)
        algo_orders = client.open_algo_orders(symbol)

        robot_normal = [
            order
            for order in normal_orders
            if is_robot_client_id(config, extract_normal_client_order_id(order))
        ]
        robot_algo = [
            order
            for order in algo_orders
            if is_robot_client_id(config, extract_algo_client_id(order))
        ]

        payload: dict[str, Any] = {
            "mark_price": mark_price.get("markPrice"),
            "last_price": last_price.get("price"),
            "min_notional": format_decimal(rules.min_notional),
            "min_quantity": format_decimal(rules.min_quantity),
            "quantity_step": format_decimal(rules.quantity_step),
            "price_tick": format_decimal(rules.price_tick),
            "minimum_executable_quantity": (
                format_decimal(rules.minimum_executable_quantity(last)) if last > 0 else "0"
            ),
            "position": _extract_open_position(symbol, positions),
            "normal_orders": len(normal_orders),
            "algo_orders": len(algo_orders),
            "robot_normal_orders": len(robot_normal),
            "robot_algo_orders": len(robot_algo),
            "error": "",
        }

        if include_raw_orders:
            payload.update(
                {
                    "normal_orders_raw": normal_orders,
                    "algo_orders_raw": algo_orders,
                    "robot_normal_orders_raw": robot_normal,
                    "robot_algo_orders_raw": robot_algo,
                }
            )

        return payload

    except Exception as error:
        return {"error": str(error)}


def _check(callable_obj) -> dict[str, Any]:
    try:
        data = callable_obj()
    except Exception as error:
        return {"ok": False, "error": str(error)}

    return {"ok": True, "data": data}


def _format_check_line(label: str, check: dict[str, Any]) -> str:
    if check.get("ok"):
        return f"{label}: OK"

    return f"{label}: FALHA | {check.get('error', '')}"


def _balance_summary(account: dict) -> dict[str, str]:
    usdt = extract_usdt_asset(account) or {}
    return {
        "wallet_balance": str(usdt.get("walletBalance", "0")),
        "available_balance": str(usdt.get("availableBalance", "0")),
        "margin_balance": str(usdt.get("marginBalance", "0")),
        "unrealized_pnl": str(usdt.get("unrealizedProfit", "0")),
    }


def _extract_open_position(symbol: str, positions: list[dict]) -> dict[str, str]:
    for position in positions:
        amount = Decimal(str(position.get("positionAmt", "0")))
        if amount == 0:
            continue

        return {
            "symbol": symbol,
            "direction": "LONG" if amount > 0 else "SHORT",
            "quantity": str(abs(amount)),
            "entry_price": str(position.get("entryPrice", "0")),
            "unrealized_pnl": str(position.get("unRealizedProfit", "0")),
        }

    return {
        "symbol": symbol,
        "direction": "NONE",
        "quantity": "0",
        "entry_price": "0",
        "unrealized_pnl": "0",
    }


if __name__ == "__main__":
    main()
