from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

INTELLIGENT_EXECUTION_PLAN_VERSION = "0.1.0"

INTELLIGENT_EXECUTION_STATUS_DISABLED = "DISABLED"
INTELLIGENT_EXECUTION_STATUS_NOT_INTELLIGENT_FLOW = "NOT_INTELLIGENT_FLOW"
INTELLIGENT_EXECUTION_STATUS_NOT_ACTIVE_LEVEL = "NOT_ACTIVE_LEVEL"
INTELLIGENT_EXECUTION_STATUS_NOT_ENTRY_ACTION = "NOT_ENTRY_ACTION"
INTELLIGENT_EXECUTION_STATUS_MARKET_DATA_STALE = "MARKET_DATA_STALE"
INTELLIGENT_EXECUTION_STATUS_PRICE_DRIFT_TOO_HIGH = "PRICE_DRIFT_TOO_HIGH"
INTELLIGENT_EXECUTION_STATUS_READY = "READY"

INTELLIGENT_EXECUTION_ACTION_KEEP_EXECUTION = "KEEP_EXECUTION"
INTELLIGENT_EXECUTION_ACTION_USE_MARKET_NOW = "USE_MARKET_NOW"
INTELLIGENT_EXECUTION_ACTION_WAIT_MARKET_DATA = "WAIT_MARKET_DATA"
INTELLIGENT_EXECUTION_ACTION_WAIT_PRICE_STABILITY = "WAIT_PRICE_STABILITY"

INTELLIGENT_EXECUTION_STYLE_MARKET_NOW = "MARKET_NOW"
INTELLIGENT_EXECUTION_STYLE_WAIT = "WAIT"

EXECUTION_PLANNER_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
EXECUTION_PLANNER_ACTIVE_LEVELS = {"PAPER_ACTIVE", "TESTNET_BLOCK"}

DEFAULT_MAX_PRICE_DRIFT_BPS = Decimal("10")
DEFAULT_MAX_MARKET_DATA_AGE_SECONDS = Decimal("3")


def _new_execution_plan_id() -> str:
    return f"iexec_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentExecutionPlan:
    symbol: str
    action: str
    status: str
    planner_action: str
    reason: str
    execution_style: str
    should_execute: bool
    execution_will_run: bool
    trade_plan_id: str | None = None
    planned_quantity_present: bool = False
    planned_exit_present: bool = False
    market_price: Decimal | None = None
    mark_price: Decimal | None = None
    price_drift_bps: Decimal | None = None
    max_price_drift_bps: Decimal = DEFAULT_MAX_PRICE_DRIFT_BPS
    market_data_age_seconds: Decimal | None = None
    max_market_data_age_seconds: Decimal = DEFAULT_MAX_MARKET_DATA_AGE_SECONDS
    market_data_stale: bool = False
    applied: bool = False
    can_use_for_execution: bool = False
    paper_only: bool = False
    testnet_only: bool = False
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    plan_id: str = field(default_factory=_new_execution_plan_id)
    version: str = INTELLIGENT_EXECUTION_PLAN_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class IntelligentExecutionPlanner:
    """Planeja a decisão final de execução antes do ExecutionEngine.

    Fase 6:
    - avalia freshness do mercado
    - avalia divergência entre intent price e mark price
    - libera MARKET_NOW quando seguro
    - segura execução em paper/testnet quando o mercado está ruim
    - não altera REAL
    """

    def build_plan(
        self,
        *,
        symbol: str,
        action: str,
        architecture_mode: str,
        intelligent_activation_level: str,
        execution_mode: str,
        execution_planner_enabled: bool,
        intent_market_price,
        mark_price,
        market_data_age_seconds,
        market_data_stale: bool,
        planned_quantity,
        planned_exit,
        trade_plan_id: str | None,
        max_price_drift_bps: Decimal = DEFAULT_MAX_PRICE_DRIFT_BPS,
        max_market_data_age_seconds: Decimal = DEFAULT_MAX_MARKET_DATA_AGE_SECONDS,
    ) -> IntelligentExecutionPlan:
        market_price_decimal = _to_decimal(intent_market_price)
        mark_price_decimal = _to_decimal(mark_price)
        data_age_decimal = _to_decimal(market_data_age_seconds)

        base_diagnostics = {
            "architecture_mode": architecture_mode,
            "intelligent_activation_level": intelligent_activation_level,
            "execution_mode": execution_mode,
            "execution_planner_enabled": execution_planner_enabled,
            "planned_quantity_present": planned_quantity is not None,
            "planned_exit_present": planned_exit is not None,
            "safety_note": (
                "Fase 6: execution planner pode segurar execucao apenas em "
                "PAPER_ACTIVE ou TESTNET_BLOCK. REAL continua protegido."
            ),
        }

        if not execution_planner_enabled:
            return self._build(
                symbol=symbol,
                action=action,
                status=INTELLIGENT_EXECUTION_STATUS_DISABLED,
                planner_action=INTELLIGENT_EXECUTION_ACTION_KEEP_EXECUTION,
                reason="Execution planner esta desligado",
                execution_style=INTELLIGENT_EXECUTION_STYLE_MARKET_NOW,
                should_execute=True,
                execution_will_run=True,
                trade_plan_id=trade_plan_id,
                planned_quantity_present=planned_quantity is not None,
                planned_exit_present=planned_exit is not None,
                market_price=market_price_decimal,
                mark_price=mark_price_decimal,
                market_data_age_seconds=data_age_decimal,
                market_data_stale=market_data_stale,
                diagnostics=base_diagnostics,
            )

        if architecture_mode != "INTELLIGENT_FLOW":
            return self._build(
                symbol=symbol,
                action=action,
                status=INTELLIGENT_EXECUTION_STATUS_NOT_INTELLIGENT_FLOW,
                planner_action=INTELLIGENT_EXECUTION_ACTION_KEEP_EXECUTION,
                reason="Execution planner exige arquitetura inteligente",
                execution_style=INTELLIGENT_EXECUTION_STYLE_MARKET_NOW,
                should_execute=True,
                execution_will_run=True,
                trade_plan_id=trade_plan_id,
                planned_quantity_present=planned_quantity is not None,
                planned_exit_present=planned_exit is not None,
                market_price=market_price_decimal,
                mark_price=mark_price_decimal,
                market_data_age_seconds=data_age_decimal,
                market_data_stale=market_data_stale,
                diagnostics=base_diagnostics,
            )

        paper_active = (
            execution_mode == "PAPER_LIVE" and intelligent_activation_level == "PAPER_ACTIVE"
        )
        testnet_block = (
            execution_mode == "TESTNET_LIVE" and intelligent_activation_level == "TESTNET_BLOCK"
        )

        if not paper_active and not testnet_block:
            return self._build(
                symbol=symbol,
                action=action,
                status=INTELLIGENT_EXECUTION_STATUS_NOT_ACTIVE_LEVEL,
                planner_action=INTELLIGENT_EXECUTION_ACTION_KEEP_EXECUTION,
                reason=(
                    "Execution planner ativo nesta fase somente em PAPER_ACTIVE ou TESTNET_BLOCK"
                ),
                execution_style=INTELLIGENT_EXECUTION_STYLE_MARKET_NOW,
                should_execute=True,
                execution_will_run=True,
                trade_plan_id=trade_plan_id,
                planned_quantity_present=planned_quantity is not None,
                planned_exit_present=planned_exit is not None,
                market_price=market_price_decimal,
                mark_price=mark_price_decimal,
                market_data_age_seconds=data_age_decimal,
                market_data_stale=market_data_stale,
                diagnostics={
                    **base_diagnostics,
                    "paper_active": paper_active,
                    "testnet_block": testnet_block,
                },
            )

        if action not in EXECUTION_PLANNER_ENTRY_ACTIONS:
            return self._build(
                symbol=symbol,
                action=action,
                status=INTELLIGENT_EXECUTION_STATUS_NOT_ENTRY_ACTION,
                planner_action=INTELLIGENT_EXECUTION_ACTION_KEEP_EXECUTION,
                reason="Execution planner nao segura saidas nesta fase",
                execution_style=INTELLIGENT_EXECUTION_STYLE_MARKET_NOW,
                should_execute=True,
                execution_will_run=True,
                trade_plan_id=trade_plan_id,
                planned_quantity_present=planned_quantity is not None,
                planned_exit_present=planned_exit is not None,
                market_price=market_price_decimal,
                mark_price=mark_price_decimal,
                market_data_age_seconds=data_age_decimal,
                market_data_stale=market_data_stale,
                diagnostics=base_diagnostics,
            )

        if market_data_stale or (
            data_age_decimal is not None and data_age_decimal > max_market_data_age_seconds
        ):
            return self._build(
                symbol=symbol,
                action=action,
                status=INTELLIGENT_EXECUTION_STATUS_MARKET_DATA_STALE,
                planner_action=INTELLIGENT_EXECUTION_ACTION_WAIT_MARKET_DATA,
                reason="Execution planner aguardando mercado atualizado",
                execution_style=INTELLIGENT_EXECUTION_STYLE_WAIT,
                should_execute=False,
                execution_will_run=False,
                trade_plan_id=trade_plan_id,
                planned_quantity_present=planned_quantity is not None,
                planned_exit_present=planned_exit is not None,
                market_price=market_price_decimal,
                mark_price=mark_price_decimal,
                market_data_age_seconds=data_age_decimal,
                market_data_stale=True,
                applied=True,
                can_use_for_execution=True,
                paper_only=paper_active,
                testnet_only=testnet_block,
                can_activate_testnet=testnet_block,
                diagnostics=base_diagnostics,
            )

        price_drift_bps = _price_drift_bps(
            market_price=market_price_decimal,
            mark_price=mark_price_decimal,
        )

        if price_drift_bps is not None and price_drift_bps > max_price_drift_bps:
            return self._build(
                symbol=symbol,
                action=action,
                status=INTELLIGENT_EXECUTION_STATUS_PRICE_DRIFT_TOO_HIGH,
                planner_action=INTELLIGENT_EXECUTION_ACTION_WAIT_PRICE_STABILITY,
                reason="Execution planner aguardando estabilizacao do preco",
                execution_style=INTELLIGENT_EXECUTION_STYLE_WAIT,
                should_execute=False,
                execution_will_run=False,
                trade_plan_id=trade_plan_id,
                planned_quantity_present=planned_quantity is not None,
                planned_exit_present=planned_exit is not None,
                market_price=market_price_decimal,
                mark_price=mark_price_decimal,
                price_drift_bps=price_drift_bps,
                market_data_age_seconds=data_age_decimal,
                market_data_stale=False,
                applied=True,
                can_use_for_execution=True,
                paper_only=paper_active,
                testnet_only=testnet_block,
                can_activate_testnet=testnet_block,
                diagnostics=base_diagnostics,
            )

        return self._build(
            symbol=symbol,
            action=action,
            status=INTELLIGENT_EXECUTION_STATUS_READY,
            planner_action=INTELLIGENT_EXECUTION_ACTION_USE_MARKET_NOW,
            reason="Execution planner liberou MARKET_NOW",
            execution_style=INTELLIGENT_EXECUTION_STYLE_MARKET_NOW,
            should_execute=True,
            execution_will_run=True,
            trade_plan_id=trade_plan_id,
            planned_quantity_present=planned_quantity is not None,
            planned_exit_present=planned_exit is not None,
            market_price=market_price_decimal,
            mark_price=mark_price_decimal,
            price_drift_bps=price_drift_bps,
            market_data_age_seconds=data_age_decimal,
            market_data_stale=False,
            applied=True,
            can_use_for_execution=True,
            paper_only=paper_active,
            testnet_only=testnet_block,
            can_activate_testnet=testnet_block,
            diagnostics=base_diagnostics,
        )

    def _build(
        self,
        *,
        symbol: str,
        action: str,
        status: str,
        planner_action: str,
        reason: str,
        execution_style: str,
        should_execute: bool,
        execution_will_run: bool,
        trade_plan_id: str | None,
        planned_quantity_present: bool,
        planned_exit_present: bool,
        market_price: Decimal | None,
        mark_price: Decimal | None,
        price_drift_bps: Decimal | None = None,
        market_data_age_seconds: Decimal | None = None,
        market_data_stale: bool = False,
        applied: bool = False,
        can_use_for_execution: bool = False,
        paper_only: bool = False,
        testnet_only: bool = False,
        can_activate_testnet: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> IntelligentExecutionPlan:
        return IntelligentExecutionPlan(
            symbol=symbol,
            action=action,
            status=status,
            planner_action=planner_action,
            reason=reason,
            execution_style=execution_style,
            should_execute=should_execute,
            execution_will_run=execution_will_run,
            trade_plan_id=trade_plan_id,
            planned_quantity_present=planned_quantity_present,
            planned_exit_present=planned_exit_present,
            market_price=market_price,
            mark_price=mark_price,
            price_drift_bps=price_drift_bps,
            market_data_age_seconds=market_data_age_seconds,
            market_data_stale=market_data_stale,
            applied=applied,
            can_use_for_execution=can_use_for_execution,
            paper_only=paper_only,
            testnet_only=testnet_only,
            can_activate_testnet=can_activate_testnet,
            can_activate_real=False,
            diagnostics=diagnostics or {},
        )


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _price_drift_bps(
    *,
    market_price: Decimal | None,
    mark_price: Decimal | None,
) -> Decimal | None:
    if market_price is None or mark_price is None:
        return None

    if market_price <= 0 or mark_price <= 0:
        return None

    return abs((market_price - mark_price) / mark_price) * Decimal("10000")
