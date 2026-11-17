"""File-driven Governor characterization replay with a fake exchange."""

from __future__ import annotations

import csv
import json
import time
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import patch

from binance.futures_client import BinanceFuturesNetworkError
from config import ExecutionMode
from config.symbol_context import PortfolioRiskSettings, symbol_config_from_app_config
from domain.models import PositionDirection, TradeDecision
from execution.coordinator import ExecutionCoordinator
from governor.alphora_governor import AlphoraGovernor
from governor.cycle.telemetry import CycleTelemetry
from governor.evaluation.evaluator import SymbolEvaluator
from market_data.snapshot_provider import wire_names
from scanner.signal_score import SignalScore
from tests.fakes import FakeFuturesClient
from tests.governor.test_alphora_governor import FakeHotWorker, FakeJournal, FakeStrategyCoordinator

_EVENTS = {
    "governor_evaluate_symbol_flow_decision",
    "economic_gate_evaluated_initial",
    "economic_gate_evaluated_final",
    "trade_blocked_by_economic_gate",
    "trade_blocked_by_governor",
    "portfolio_trade_approved",
    "trade_approved",
    "execution_payload_sent",
    "command_dispatched",
    "execution_confirmed",
    "execution_uncertain",
    "execution_failed",
    "symbol_warmup_new_entry_blocked",
    "symbol_quarantine_new_entry_blocked",
    "governor_defensive_mode_blocked",
    "trade_blocked_by_defensive_mode",
    wire_names.PRE_EXECUTION_REVALIDATED,
}
_FIELDS = {
    "symbol",
    "action",
    "stage",
    "classification",
    "reason",
    "status",
    "allowed",
    "terminal",
    "execution_will_run",
    "position_changed",
    "planned_quantity",
    "planned_stop_price",
    "planned_take_profit_price",
    "market_price",
    "expected_net_gain",
    "expected_net_loss",
    "trade_plan_id",
    "intent_id",
    "execution_command_id",
    "cycle_id",
    "symbol_eval_id",
    "order_ids",
}
_ID_FIELDS = {
    "cycle_id",
    "symbol_eval_id",
    "trade_plan_id",
    "intent_id",
    "execution_command_id",
    "newClientOrderId",
    "clientAlgoId",
    "orig_client_order_id",
    "client_algo_id",
    "order_ids",
}


class FixtureFuturesClient(FakeFuturesClient):
    """Serve recorded account and market inputs, then apply simulated fills."""

    def __init__(self, snapshots: dict[str, Any], account: dict[str, Any], candles: list[dict]):
        super().__init__()
        self.price = Decimal(candles[-1]["close"])
        self.mark = Decimal(snapshots["heavy"]["mark_price"])
        assert self.price == Decimal(snapshots["light"]["price"])
        assert self.price == Decimal(snapshots["heavy"]["price"])
        self.balance = str(account["balance"])
        position = account.get("position", {})
        self.direction = PositionDirection(position.get("direction", "NONE"))
        self.quantity = Decimal(str(position.get("quantity", "0")))
        self.entry_price = Decimal(str(position.get("entry_price", "0")))
        self.open_normal_orders = list(account.get("open_orders", []))
        self.open_algo_order_items = list(account.get("open_algo_orders", []))
        self.unknown_entry_result = bool(account.get("unknown_entry_result", False))
        self.query_order_calls: list[dict[str, Any]] = []
        self.query_order_response = None

    def account_info(self, *, timeout=None):
        return {
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": self.balance,
                    "availableBalance": self.balance,
                    "marginBalance": self.balance,
                    "unrealizedProfit": "0",
                }
            ]
        }

    def position_risk(self, symbol, *, timeout=None):
        return super().position_risk(symbol or "BTCUSDT", timeout=timeout)

    def new_order(self, **params):
        response = super().new_order(**params)
        quantity = Decimal(str(params.get("quantity", "0")))
        if params.get("reduceOnly") == "true" or params.get("reduceOnly") is True:
            self.quantity = max(Decimal("0"), self.quantity - quantity)
            if self.quantity == 0:
                self.direction = PositionDirection.NONE
                self.entry_price = Decimal("0")
        else:
            self.direction = (
                PositionDirection.LONG if params["side"] == "BUY" else PositionDirection.SHORT
            )
            self.quantity = quantity
            self.entry_price = self.price
        if self.unknown_entry_result and not params.get("reduceOnly"):
            self.query_order_response = response
            raise BinanceFuturesNetworkError("timeout after exchange acceptance")
        return response

    def query_order(self, **params):
        self.query_order_calls.append(params)
        return super().query_order(**params)

    def cancel_algo_order(self, **params):
        response = super().cancel_algo_order(**params)
        target = params.get("algo_id") or params.get("client_algo_id")
        self.open_algo_order_items = [
            item
            for item in self.open_algo_order_items
            if (item.get("algoId") or item.get("clientAlgoId")) != target
        ]
        return response


class _IdNormalizer:
    def __init__(self):
        self.ids: dict[str, str] = {}

    def value(self, value: Any, key: str = "") -> Any:
        if isinstance(value, Enum):
            return self.value(value.value, key)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (list, tuple)):
            return [self.value(item, key) for item in value]
        if isinstance(value, dict):
            return {name: self.value(item, name) for name, item in value.items()}
        if key in _ID_FIELDS and value is not None and value != "":
            raw = str(value)
            if raw not in self.ids:
                self.ids[raw] = f"<id:{len(self.ids) + 1}>"
            return self.ids[raw]
        return value


def _plan(evaluation) -> dict[str, Any] | None:
    planned = evaluation.planned_trade
    if planned is None:
        return None
    plan = planned.plan
    return {
        "status": plan.status,
        "reason": plan.reason,
        "quantity": str(plan.quantity),
        "notional": str(plan.notional),
        "stop_price": str(plan.stop_price) if plan.stop_price is not None else None,
        "take_profit_price": (
            str(plan.take_profit_price) if plan.take_profit_price is not None else None
        ),
        "expected_net_gain": (
            str(plan.expected_net_gain) if plan.expected_net_gain is not None else None
        ),
        "expected_net_loss": (
            str(plan.expected_net_loss) if plan.expected_net_loss is not None else None
        ),
    }


def run_scenario(directory: Path, config_factory) -> dict[str, Any]:
    scenario = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    snapshots = json.loads((directory / "snapshots.json").read_text(encoding="utf-8"))
    account = json.loads((directory / "account.json").read_text(encoding="utf-8"))
    with (directory / "candles.csv").open(newline="", encoding="utf-8") as source:
        candles = list(csv.DictReader(source))
    assert candles, "A golden scenario needs at least one recorded candle"

    mode = ExecutionMode(scenario["execution_mode"])
    config = replace(
        config_factory(execution_mode=mode, **scenario.get("factory", {})),
        market_ws_enabled=False,
        **scenario.get("config_overrides", {}),
    )
    profile = symbol_config_from_app_config(config)
    client = FixtureFuturesClient(snapshots, account, candles)
    governor = AlphoraGovernor(config=config, client=client, symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    score = SignalScore(
        symbol=profile.symbol,
        score=scenario["score"],
        action=scenario["score_action"],
        reason=scenario["score_reason"],
    )
    decision = TradeDecision.close(scenario["decision_reason"]) if scenario.get("close") else None
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: score}, decision=decision
    )
    governor.hot_symbol_worker = FakeHotWorker()
    patches = ExitStack()
    if "market_health" in snapshots:
        patches.enter_context(
            patch.object(
                CycleTelemetry,
                "market_health",
                lambda _telemetry, _now: dict(snapshots["market_health"]),
            )
        )
    if "portfolio_settings" in scenario:
        governor.portfolio_settings = PortfolioRiskSettings(**scenario["portfolio_settings"])
        from governor.portfolio_risk_engine import PortfolioRiskEngine

        governor.portfolio_risk_engine = PortfolioRiskEngine(governor.portfolio_settings)

    evaluations: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    original_metrics = SymbolEvaluator.record_stage_metrics
    original_execute = ExecutionCoordinator.execute

    def record_metrics(evaluator, evaluation, *, terminal_stage):
        result = evaluation.terminal_result
        evaluations.append(
            {
                "symbol": evaluation.evaluation_input.symbol_config.symbol,
                "terminal_stage": terminal_stage,
                "result": (
                    {
                        "stage": result.stage,
                        "classification": result.classification,
                        "reason": result.reason,
                        "action": result.action,
                        "status": result.status,
                        "terminal": result.terminal,
                    }
                    if result is not None
                    else None
                ),
                "plan": _plan(evaluation),
            }
        )
        return original_metrics(evaluator, evaluation, terminal_stage=terminal_stage)

    def record_execute(self, *, symbol_config, heavy_snapshot, command):
        commands.append(
            {
                "symbol": command.symbol,
                "action": command.action.value,
                "quantity": str(command.quantity) if command.quantity is not None else None,
                "market_price": str(command.market_price),
                "stop_price": str(command.stop_price) if command.stop_price is not None else None,
                "take_profit_price": (
                    str(command.take_profit_price)
                    if command.take_profit_price is not None
                    else None
                ),
                "trade_plan_id": command.trade_plan_id,
                "intent_id": command.intent_id,
                "execution_command_id": command.command_id,
            }
        )
        return original_execute(
            self, symbol_config=symbol_config, heavy_snapshot=heavy_snapshot, command=command
        )

    patches.enter_context(patch.object(SymbolEvaluator, "record_stage_metrics", record_metrics))
    patches.enter_context(patch.object(ExecutionCoordinator, "execute", record_execute))
    try:
        governor.run_cycle(now=time.time())
        stage_events = [
            payload
            for event_type, payload in governor.events_journal.events
            if event_type == "symbol_evaluation_stage_metrics"
        ]
        normalizer = _IdNormalizer()
        return normalizer.value(
            {
                "stages": [
                    {
                        "symbol": event["symbol"],
                        "terminal_stage": event["terminal_stage"],
                        "trace": [
                            {
                                "stage": item["stage"],
                                "status": item["status"],
                                "terminal": item["terminal"],
                            }
                            for item in event["stages"]
                        ],
                    }
                    for event in stage_events
                ],
                "evaluations": evaluations,
                "commands": commands,
                "mutations": {
                    "new_order": client.new_order_calls,
                    "new_algo_order": client.new_algo_order_calls,
                    "cancel_order": client.cancel_order_calls,
                    "cancel_algo_order": client.cancel_algo_order_calls,
                },
                "queries": {"query_order": client.query_order_calls},
                "exchange_state": {
                    "direction": client.direction.value,
                    "quantity": str(client.quantity),
                    "entry_price": str(client.entry_price),
                    "open_algo_orders": [
                        {
                            "type": item.get("orderType"),
                            "trigger_price": item.get("triggerPrice"),
                        }
                        for item in client.open_algo_order_items
                    ],
                },
                "journal": [
                    {
                        "type": event_type,
                        "payload": {key: payload[key] for key in payload if key in _FIELDS},
                    }
                    for event_type, payload in governor.events_journal.events
                    if event_type in _EVENTS
                ],
            }
        )
    finally:
        patches.close()
        if governor.snapshot_provider is not None:
            governor.snapshot_provider.close()
