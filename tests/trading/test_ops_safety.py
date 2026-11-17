import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from domain.models import PositionDirection
from futures.operational import OperationalState
from ops.cancel_robot_orders import _config_for_profile, cancel_robot_orders
from ops.emergency_flatten import _wait_until_flat, emergency_flatten
from ops.operational_stop import operational_stop
from resilience.budgets import EMERGENCY_FLATTEN_BUDGET
from resilience.catalog import (
    EMERGENCY_FLATTEN_CONFIRMATION_MAX_TIME_SECONDS,
    EMERGENCY_FLATTEN_CONFIRMATION_REQUEST_TIMEOUT,
)
from tests.fakes import FakeFuturesClient


def test_cancel_robot_orders_does_not_mutate_without_text_confirmation(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.open_normal_orders = [{"clientOrderId": "ALPHORA_ENTRY_1", "orderId": 1}]

    monkeypatch.setattr("ops.cancel_robot_orders.load_config", lambda: config)
    monkeypatch.setattr("ops.cancel_robot_orders.make_client", lambda *_args, **_kwargs: client)

    result = cancel_robot_orders(confirm="")

    assert result["dry_run"] is True
    assert result["required_confirmation"] == "CANCEL_ROBOT_ORDERS"
    assert client.cancel_order_calls == []


@pytest.mark.parametrize("amount", ["NaN", "invalid", None])
def test_cancel_robot_orders_blocks_on_unknown_position_amount(monkeypatch, config_factory, amount):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class InvalidPositionClient(FakeFuturesClient):
        def position_risk(self, symbol, *, timeout=None):
            return [{"symbol": symbol, "positionAmt": amount}]

    client = InvalidPositionClient()
    client.open_algo_order_items = _stop_and_take_orders()
    monkeypatch.setattr("ops.cancel_robot_orders.load_config", lambda: config)
    monkeypatch.setattr("ops.cancel_robot_orders.make_client", lambda *_args, **_kwargs: client)

    result = cancel_robot_orders(confirm="CANCEL_PROTECTIONS_WHILE_IN_POSITION")

    assert result["blocked"] is True
    assert client.cancel_algo_order_calls == []


def test_ops_config_for_profile_preserves_symbol_risk_and_protection(config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    profile = replace(
        symbol_config_from_app_config(config),
        stop_loss_pct=0.011,
        take_profit_pct=0.022,
        leverage=3,
        strategy_params={"min_history": 2},
    )

    derived = _config_for_profile(config, profile)

    assert derived.leverage == 3
    assert derived.stop_loss_pct == 0.011
    assert derived.take_profit_pct == 0.022
    assert derived.strategy_params == {"min_history": 2}


def test_emergency_flatten_does_not_mutate_without_confirmation(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.REAL_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")

    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr("ops.emergency_flatten.make_client", lambda *_args, **_kwargs: client)

    result = emergency_flatten(confirm="")

    assert result["dry_run"] is True
    assert result["required_confirmation"] == "EMERGENCY_FLATTEN_REAL"
    assert client.new_order_calls == []


def test_emergency_flatten_refuses_invalid_position_snapshot(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class InvalidPositionClient(FakeFuturesClient):
        def position_risk(self, symbol, *, timeout=None):
            return [{"symbol": symbol, "positionAmt": "NaN"}]

    client = InvalidPositionClient()
    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr("ops.emergency_flatten.make_client", lambda *_args, **_kwargs: client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert result["blocked"] is True
    assert client.new_order_calls == []
    assert client.cancel_order_calls == []


def test_emergency_flatten_wait_until_flat_uses_budget_and_timeout(monkeypatch):
    class TimeoutRecordingClient(FakeFuturesClient):
        def __init__(self):
            super().__init__()
            self.position_risk_timeouts = []

        def position_risk(self, symbol, *, timeout=None):
            self.position_risk_timeouts.append(timeout)
            return super().position_risk(symbol, timeout=timeout)

    client = TimeoutRecordingClient()
    captured = {}

    def fake_run_polling(operation, **kwargs):
        captured.update(kwargs)
        return operation()

    monkeypatch.setattr("ops.emergency_flatten.run_polling", fake_run_polling)

    assert _wait_until_flat(client, "BTCUSDT") is True
    assert captured["budget"] is EMERGENCY_FLATTEN_BUDGET
    assert captured["budget_key"] == "emergency-flatten:BTCUSDT"
    assert captured["max_time_seconds"] == EMERGENCY_FLATTEN_CONFIRMATION_MAX_TIME_SECONDS
    assert client.position_risk_timeouts == [EMERGENCY_FLATTEN_CONFIRMATION_REQUEST_TIMEOUT]


class LegsClient(FakeFuturesClient):
    """Fake exchange with explicit position legs and reduce-only fill semantics."""

    def __init__(self, legs, *, hedge=False, fill_ratio=Decimal("1")):
        super().__init__()
        self.legs = {side: Decimal(amount) for side, amount in legs.items()}
        self.hedge = hedge
        self.fill_ratio = fill_ratio

    def get_position_mode(self):
        return {"dualSidePosition": self.hedge}

    def position_risk(self, symbol, *, timeout=None):
        return [
            {
                "symbol": symbol,
                "positionSide": side,
                "positionAmt": str(amount),
                "entryPrice": "100",
            }
            for side, amount in self.legs.items()
        ]

    def new_order(self, **params):
        response = super().new_order(**params)
        side = params.get("positionSide", "BOTH")
        amount = self.legs[side]
        # reduceOnly never flips the position: it fills at most the open size.
        filled = min(Decimal(params["quantity"]) * self.fill_ratio, abs(amount))
        self.legs[side] = amount - filled if amount > 0 else amount + filled
        return response


def _flatten_with(monkeypatch, config, client):
    cancels = []

    def fake_cancel_robot_orders(**kwargs):
        cancels.append(kwargs)
        return {"dry_run": False}

    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr("ops.emergency_flatten.make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("ops.emergency_flatten.cancel_robot_orders", fake_cancel_robot_orders)
    monkeypatch.setattr(
        "ops.emergency_flatten.run_polling",
        lambda operation, **_kwargs: operation(),
    )
    return cancels


def test_emergency_flatten_refuses_hedge_mode_legs_without_sending_orders(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"LONG": "0.5", "SHORT": "-0.3"}, hedge=True)
    cancels = _flatten_with(monkeypatch, config, client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert client.new_order_calls == []
    assert cancels == []
    assert result["blocked"] is True
    assert result["dry_run"] is True
    assert result["refused_reason"] == "HEDGE_MODE"
    assert result["position_mode"] == "HEDGE"
    assert [(leg["position_side"], leg["quantity"]) for leg in result["open_legs"]] == [
        ("LONG", "0.5"),
        ("SHORT", "0.3"),
    ]
    assert result["position"]["direction"] == "MULTIPLE"
    reconcile = json.loads(Path(result["manual_reconcile_path"]).read_text(encoding="utf-8"))
    assert reconcile["reason"] == "emergency_flatten_refused_hedge_mode"
    assert len(reconcile["context"]["open_legs"]) == 2


def test_emergency_flatten_preview_reports_every_leg_and_the_refusal(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"LONG": "0.5", "SHORT": "-0.3"}, hedge=True)
    _flatten_with(monkeypatch, config, client)

    result = emergency_flatten(confirm="")

    assert result["dry_run"] is True
    assert result["refused_reason"] == "HEDGE_MODE"
    assert len(result["open_legs"]) == 2
    assert "Procedimento manual" in result["message"]
    assert "manual_reconcile_path" not in result
    assert client.new_order_calls == []


@pytest.mark.parametrize(
    "legs,reason",
    [
        ({"BOTH": "0.5", "LONG": "0.2"}, "MULTIPLE_OPEN_LEGS"),
        ({"LONG": "0.5"}, "UNEXPECTED_POSITION_SIDE"),
    ],
)
def test_emergency_flatten_refuses_ambiguous_one_way_snapshots(
    monkeypatch,
    config_factory,
    legs,
    reason,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient(legs, hedge=False)
    _flatten_with(monkeypatch, config, client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert client.new_order_calls == []
    assert result["blocked"] is True
    assert result["refused_reason"] == reason


def test_emergency_flatten_closes_one_way_leg_and_confirms_flat(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"BOTH": "-0.4"})
    cancels = _flatten_with(monkeypatch, config, client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert client.new_order_calls == [
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.4",
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
        }
    ]
    assert result["dry_run"] is False
    assert "blocked" not in result
    assert len(cancels) == 1


def test_emergency_flatten_partial_fill_requires_manual_reconcile(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"BOTH": "1.0"}, fill_ratio=Decimal("0.6"))
    cancels = _flatten_with(monkeypatch, config, client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert len(client.new_order_calls) == 1
    assert cancels == []
    assert result["blocked"] is True
    assert [leg["quantity"] for leg in result["open_legs"]] == ["0.4"]
    reconcile = json.loads(Path(result["manual_reconcile_path"]).read_text(encoding="utf-8"))
    assert reconcile["reason"] == "position_close_not_confirmed"
    assert reconcile["last_known_position"]["quantity"] == "0.4"


def test_emergency_flatten_stale_smaller_read_leaves_remainder_for_manual_reconcile(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"BOTH": "0.5"})
    original_position_risk = client.position_risk
    reads = {"count": 0}

    def stale_then_live(symbol, *, timeout=None):
        reads["count"] += 1
        rows = original_position_risk(symbol, timeout=timeout)
        if reads["count"] == 1:
            # The pre-mutation read lags the exchange: it misses 0.3 that opened since.
            client.legs["BOTH"] = Decimal("0.8")
        return rows

    client.position_risk = stale_then_live
    cancels = _flatten_with(monkeypatch, config, client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert client.new_order_calls[0]["quantity"] == "0.5"
    assert cancels == []
    assert result["blocked"] is True
    assert [leg["quantity"] for leg in result["open_legs"]] == ["0.3"]


def test_emergency_flatten_wait_until_flat_checks_every_leg(monkeypatch):
    client = LegsClient({"BOTH": "0", "SHORT": "-0.1"})
    monkeypatch.setattr(
        "ops.emergency_flatten.run_polling",
        lambda operation, **_kwargs: operation(),
    )

    assert _wait_until_flat(client, "BTCUSDT") is False


def test_emergency_flatten_hedge_mode_without_open_legs_reports_no_position(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"LONG": "0", "SHORT": "0"}, hedge=True)
    _flatten_with(monkeypatch, config, client)

    preview = emergency_flatten(confirm="")
    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert "refused_reason" not in preview
    assert result["message"] == "Sem posicao aberta; ordens do robo canceladas"
    assert "manual_reconcile_path" not in result
    assert client.new_order_calls == []


def _stop_with(monkeypatch, config, client):
    monkeypatch.setattr("ops.operational_stop.load_config", lambda: config)
    monkeypatch.setattr("ops.operational_stop.make_client", lambda *_args, **_kwargs: client)


def _stop_and_take_orders():
    return [
        {
            "clientAlgoId": "ALPHORA_STOP_1",
            "orderType": "STOP_MARKET",
            "algoId": 1,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "triggerPrice": "99.0",
            "algoStatus": "NEW",
        },
        {
            "clientAlgoId": "ALPHORA_TAKE_1",
            "orderType": "TAKE_PROFIT_MARKET",
            "algoId": 2,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "triggerPrice": "102.0",
            "algoStatus": "NEW",
        },
    ]


@pytest.mark.parametrize("mode", ["STOP_KEEP_PROTECTIONS", "SAFE_STOP"])
def test_operational_stop_refuses_hedge_legs_even_when_one_leg_looks_protected(
    monkeypatch,
    config_factory,
    tmp_path,
    mode,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"LONG": "0.5", "SHORT": "-0.3"}, hedge=True)
    client.open_algo_order_items = _stop_and_take_orders()
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode=mode, confirm="SAFE_STOP")

    target = result["results"][0]
    assert result["ok"] is False
    assert result["stop_request"] is None
    assert target["blocked"] is True
    assert target["state"] == OperationalState.MANUAL_RECONCILE_REQUIRED.value
    assert target["refused_reason"] == "HEDGE_MODE"
    assert len(target["open_legs"]) == 2
    assert not (tmp_path / "runtime" / "stop.request").exists()
    assert client.new_algo_order_calls == []
    assert client.new_order_calls == []


def test_operational_stop_refuses_multiple_one_way_rows(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"BOTH": "0.5", "LONG": "0.2"})
    client.open_algo_order_items = _stop_and_take_orders()
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="STOP_KEEP_PROTECTIONS")

    assert result["ok"] is False
    assert result["results"][0]["refused_reason"] == "MULTIPLE_OPEN_LEGS"


def test_operational_stop_still_accepts_a_protected_one_way_leg(
    monkeypatch,
    config_factory,
    tmp_path,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"BOTH": "0.5"})
    client.open_algo_order_items = _stop_and_take_orders()
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="STOP_KEEP_PROTECTIONS")

    target = result["results"][0]
    assert result["ok"] is True
    assert target["state"] == OperationalState.POSITION_OPEN_PROTECTED.value
    assert target["position"] == {
        "symbol": "BTCUSDT",
        "is_open": True,
        "direction": "LONG",
        "quantity": "0.5",
        "entry_price": "100",
    }
    assert (tmp_path / "runtime" / "stop.request").exists()


def test_operational_stop_blocks_wrong_side_protection_order(monkeypatch, config_factory, tmp_path):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"BOTH": "0.5"})
    orders = _stop_and_take_orders()
    orders[0]["side"] = "BUY"
    client.open_algo_order_items = orders
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="STOP_KEEP_PROTECTIONS")

    assert result["ok"] is False
    assert result["results"][0]["state"] == OperationalState.STOP_BLOCKED_UNPROTECTED.value
    assert result["stop_request"] is None
    assert not (tmp_path / "runtime" / "stop.request").exists()


def test_operational_stop_hedge_account_without_legs_is_flat(
    monkeypatch,
    config_factory,
    tmp_path,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = LegsClient({"LONG": "0", "SHORT": "0"}, hedge=True)
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="SAFE_STOP", confirm="SAFE_STOP")

    assert result["ok"] is True
    assert result["results"][0]["state"] == OperationalState.STOPPED_FLAT.value


@pytest.mark.parametrize("mode", ["SAFE_STOP", "STOP_KEEP_PROTECTIONS"])
def test_operational_stop_blocks_flat_symbol_with_pending_robot_entry(
    monkeypatch, config_factory, tmp_path, mode
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.open_normal_orders = [{"clientOrderId": "ALPHORA_ENTRY_1", "orderId": 3}]
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode=mode)

    assert result["ok"] is False
    assert result["results"][0]["blocked"] is True
    assert result["stop_request"] is None
    assert not (tmp_path / "runtime" / "stop.request").exists()


class AccountClient(LegsClient):
    """Fake account with One-way positions and open orders on several symbols."""

    def __init__(self, positions, *, orders=(), algo_orders=(), scan_error=None):
        super().__init__({"BOTH": "0"})
        self.positions = dict(positions)
        self.orders = list(orders)
        self.algo_orders = list(algo_orders)
        self.scan_error = scan_error

    def position_risk(self, symbol, *, timeout=None):
        if symbol is None:
            if self.scan_error is not None:
                raise self.scan_error
            symbols = self.positions
        else:
            symbols = {symbol: self.positions.get(symbol, "0")}
        return [
            {"symbol": item, "positionAmt": amount, "positionSide": "BOTH", "entryPrice": "100"}
            for item, amount in symbols.items()
        ]

    def open_orders(self, symbol, *, timeout=None):
        return [order for order in self.orders if symbol is None or order["symbol"] == symbol]

    def open_algo_orders(self, symbol, *, timeout=None):
        return [order for order in self.algo_orders if symbol is None or order["symbol"] == symbol]


def _profiles_file(tmp_path, *symbols):
    path = tmp_path / "symbols.json"
    path.write_text(
        json.dumps({"symbol_profiles": [{"symbol": item, "enabled": True} for item in symbols]}),
        encoding="utf-8",
    )
    return str(path)


def _flatten_all_preview(monkeypatch, config, client):
    _flatten_with(monkeypatch, config, client)
    return emergency_flatten(all_symbols=True)["results"]


@pytest.mark.parametrize("multi_symbol_enabled", [False, True])
def test_emergency_flatten_all_covers_profiles_regardless_of_multi_symbol_flag(
    monkeypatch,
    config_factory,
    tmp_path,
    multi_symbol_enabled,
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT", "ETHUSDT"),
        multi_symbol_enabled=multi_symbol_enabled,
    )

    results = _flatten_all_preview(monkeypatch, config, AccountClient({}))

    assert [item["symbol"] for item in results] == ["BTCUSDT", "ETHUSDT"]
    assert all(item["dry_run"] is True for item in results)


def test_emergency_flatten_all_falls_back_to_configured_symbols_without_profile_file(
    monkeypatch,
    config_factory,
):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        symbols=("BTCUSDT", "ETHUSDT"),
    )

    results = _flatten_all_preview(monkeypatch, config, AccountClient({}))

    assert [item["symbol"] for item in results] == ["BTCUSDT", "ETHUSDT"]


def test_emergency_flatten_all_includes_open_positions_outside_the_profiles(
    monkeypatch,
    config_factory,
    tmp_path,
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT"),
    )
    client = AccountClient({"BTCUSDT": "0", "SOLUSDT": "-3"})

    results = _flatten_all_preview(monkeypatch, config, client)

    by_symbol = {item["symbol"]: item for item in results}
    assert set(by_symbol) == {"BTCUSDT", "SOLUSDT"}
    assert by_symbol["SOLUSDT"]["position"]["direction"] == "SHORT"


def test_emergency_flatten_all_reports_a_failed_account_scan_and_continues(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = AccountClient({}, scan_error=RuntimeError("timeout"))

    results = _flatten_all_preview(monkeypatch, config, client)

    assert results[0]["symbol"] == "*"
    assert "position_risk" in results[0]["error"]
    assert [item["symbol"] for item in results[1:]] == ["BTCUSDT"]


def test_emergency_flatten_all_includes_flat_symbol_with_robot_order(
    monkeypatch, config_factory, tmp_path
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT"),
    )
    client = AccountClient(
        {}, orders=[{"symbol": "XRPUSDT", "clientOrderId": "ALP_XRP_ENTRY_1", "orderId": 7}]
    )

    results = _flatten_all_preview(monkeypatch, config, client)

    assert [item["symbol"] for item in results] == ["BTCUSDT", "XRPUSDT"]


def test_emergency_flatten_all_cancels_order_on_flat_unconfigured_symbol(
    monkeypatch, config_factory, tmp_path
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT"),
    )
    client = AccountClient(
        {}, orders=[{"symbol": "XRPUSDT", "clientOrderId": "ALP_XRP_ENTRY_1", "orderId": 7}]
    )
    for module in ("ops.emergency_flatten", "ops.cancel_robot_orders"):
        monkeypatch.setattr(f"{module}.load_config", lambda: config)
        monkeypatch.setattr(f"{module}.make_client", lambda *_args, **_kwargs: client)

    results = emergency_flatten(confirm="EMERGENCY_FLATTEN", all_symbols=True)["results"]

    assert [item["symbol"] for item in results] == ["BTCUSDT", "XRPUSDT"]
    assert client.cancel_order_calls == [
        {"symbol": "XRPUSDT", "order_id": 7, "orig_client_order_id": "ALP_XRP_ENTRY_1"}
    ]


def test_emergency_flatten_cancels_normal_and_algo_orders_when_already_flat(
    monkeypatch, config_factory
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.open_normal_orders = [
        {"clientOrderId": "ALPHORA_ENTRY_1", "orderId": 3},
        {"clientOrderId": "manual_entry", "orderId": 5},
    ]
    client.open_algo_order_items = [
        {"clientAlgoId": "ALPHORA_STOP_1", "orderType": "STOP_MARKET", "algoId": 4}
    ]
    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr("ops.emergency_flatten.make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("ops.cancel_robot_orders.load_config", lambda: config)
    monkeypatch.setattr("ops.cancel_robot_orders.make_client", lambda *_args, **_kwargs: client)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert result["dry_run"] is False
    assert "blocked" not in result
    assert client.new_order_calls == []
    assert client.cancel_order_calls == [
        {"symbol": "BTCUSDT", "order_id": 3, "orig_client_order_id": "ALPHORA_ENTRY_1"}
    ]
    assert client.cancel_algo_order_calls == [{"algo_id": 4, "client_algo_id": "ALPHORA_STOP_1"}]


def test_emergency_flatten_flat_order_requires_confirmation(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.open_normal_orders = [{"clientOrderId": "ALPHORA_ENTRY_1", "orderId": 3}]
    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr("ops.emergency_flatten.make_client", lambda *_args, **_kwargs: client)

    result = emergency_flatten(confirm="")

    assert result["dry_run"] is True
    assert client.cancel_order_calls == []


def test_emergency_flatten_reports_failed_cleanup_when_flat(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    _flatten_with(monkeypatch, config, client)
    monkeypatch.setattr(
        "ops.emergency_flatten.cancel_robot_orders",
        lambda **_kwargs: {"dry_run": True, "blocked": True},
    )

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert result["blocked"] is True
    assert result["manual_reconcile_path"]
    assert "nao confirmado" in result["message"]


def test_operational_stop_all_uses_governor_fallback_when_profiles_disabled(
    monkeypatch, config_factory, tmp_path
):
    path = tmp_path / "disabled.json"
    path.write_text(
        json.dumps({"symbol_profiles": [{"symbol": "ETHUSDT", "enabled": False}]}),
        encoding="utf-8",
    )
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE), symbol_profiles_path=str(path)
    )
    client = AccountClient({})
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="SAFE_STOP", all_symbols=True, preview=True)

    assert result["ok"] is True
    assert [item["symbol"] for item in result["results"]] == ["BTCUSDT"]


def test_operational_stop_all_checks_disabled_profile_with_position(
    monkeypatch, config_factory, tmp_path
):
    path = tmp_path / "disabled.json"
    path.write_text(
        json.dumps({"symbol_profiles": [{"symbol": "ETHUSDT", "enabled": False}]}),
        encoding="utf-8",
    )
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE), symbol_profiles_path=str(path)
    )
    client = AccountClient({"ETHUSDT": "1"})
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="SAFE_STOP", all_symbols=True, preview=True)

    by_symbol = {item["symbol"]: item for item in result["results"]}
    assert set(by_symbol) == {"BTCUSDT", "ETHUSDT"}
    assert by_symbol["ETHUSDT"]["blocked"] is True


def test_operational_stop_all_checks_positions_outside_profiles(
    monkeypatch, config_factory, tmp_path
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT"),
    )
    client = AccountClient({"SOLUSDT": "1"})
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="STOP_KEEP_PROTECTIONS", all_symbols=True)

    by_symbol = {item["symbol"]: item for item in result["results"]}
    assert result["ok"] is False
    assert result["stop_request"] is None
    assert by_symbol["SOLUSDT"]["blocked"] is True


def test_operational_stop_all_failed_account_scan_blocks_stop(
    monkeypatch, config_factory, tmp_path
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = AccountClient({}, scan_error=RuntimeError("timeout"))
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="SAFE_STOP", all_symbols=True)

    assert result["ok"] is False
    assert result["stop_request"] is None
    assert result["results"][0]["symbol"] == "*"
    assert "position_risk" in result["results"][0]["error"]


def test_operational_stop_all_keeps_checking_after_one_symbol_fails(
    monkeypatch, config_factory, tmp_path
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT", "ETHUSDT"),
    )

    class FailingClient(AccountClient):
        def position_risk(self, symbol, *, timeout=None):
            if symbol == "BTCUSDT":
                raise RuntimeError("BTC read failed")
            return super().position_risk(symbol, timeout=timeout)

    client = FailingClient({})
    _stop_with(monkeypatch, config, client)

    result = operational_stop(mode="SAFE_STOP", all_symbols=True)

    by_symbol = {item["symbol"]: item for item in result["results"]}
    assert result["ok"] is False
    assert result["stop_request"] is None
    assert by_symbol["BTCUSDT"]["blocked"] is True
    assert by_symbol["ETHUSDT"]["state"] == OperationalState.STOPPED_FLAT.value


def test_cancel_robot_orders_all_includes_robot_orders_outside_the_profiles(
    monkeypatch,
    config_factory,
    tmp_path,
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT"),
    )
    client = AccountClient(
        {},
        orders=[
            {"symbol": "XRPUSDT", "clientOrderId": "ALP_XRP_ENTRY_1", "orderId": 7},
            {"symbol": "DOGEUSDT", "clientOrderId": "manual_order", "orderId": 8},
        ],
    )
    monkeypatch.setattr("ops.cancel_robot_orders.load_config", lambda: config)
    monkeypatch.setattr("ops.cancel_robot_orders.make_client", lambda *_args, **_kwargs: client)

    result = cancel_robot_orders(confirm="CANCEL_ROBOT_ORDERS", all_symbols=True)

    by_symbol = {item["symbol"]: item for item in result["results"]}
    assert set(by_symbol) == {"BTCUSDT", "XRPUSDT"}
    assert client.cancel_order_calls == [
        {"symbol": "XRPUSDT", "order_id": 7, "orig_client_order_id": "ALP_XRP_ENTRY_1"}
    ]


def test_cancel_robot_orders_all_isolates_a_failing_symbol(
    monkeypatch,
    config_factory,
    tmp_path,
):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        symbol_profiles_path=_profiles_file(tmp_path, "BTCUSDT", "ETHUSDT"),
    )

    class FailingBtcClient(AccountClient):
        def open_orders(self, symbol, *, timeout=None):
            if symbol == "BTCUSDT":
                raise RuntimeError("btc down")
            return super().open_orders(symbol, timeout=timeout)

    client = FailingBtcClient({})
    monkeypatch.setattr("ops.cancel_robot_orders.load_config", lambda: config)
    monkeypatch.setattr("ops.cancel_robot_orders.make_client", lambda *_args, **_kwargs: client)

    result = cancel_robot_orders(confirm="", all_symbols=True)

    by_symbol = {item["symbol"]: item for item in result["results"]}
    assert "btc down" in by_symbol["BTCUSDT"]["error"]
    assert by_symbol["ETHUSDT"]["dry_run"] is True
