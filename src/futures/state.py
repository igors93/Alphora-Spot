import json
import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from domain.models import PositionDirection
from futures.operational import OperationalState

STATIC_PROTECTION_SOURCE = "STATIC_PROTECTION_MODEL"


@dataclass(frozen=True)
class RuntimeState:
    active_trade_id: str
    symbol: str
    direction: PositionDirection
    opened_at: float
    entry_price: Decimal
    quantity: Decimal
    entry_client_order_id: str | None = None
    stop_client_algo_id: str | None = None
    take_client_algo_id: str | None = None
    operational_state: OperationalState = OperationalState.POSITION_OPEN_PROTECTED
    protection_warning: str = ""
    protection_source: str = STATIC_PROTECTION_SOURCE
    trade_plan_id: str | None = None
    planned_stop_price: Decimal | None = None
    planned_take_profit_price: Decimal | None = None


class RuntimeStateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeState | None:
        if not self.path.exists():
            return None

        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Arquivo de estado corrompido ou inválido: {self.path}") from exc

        operational_state = data.get("operational_state")
        if operational_state is None:
            operational_state = (
                OperationalState.POSITION_OPEN_PROTECTED.value
                if data.get("stop_client_algo_id") and data.get("take_client_algo_id")
                else OperationalState.POSITION_OPEN_UNPROTECTED.value
            )

        try:
            return RuntimeState(
                active_trade_id=str(data["active_trade_id"]),
                symbol=str(data["symbol"]),
                direction=PositionDirection(str(data["direction"])),
                opened_at=float(data["opened_at"]),
                entry_price=Decimal(str(data["entry_price"])),
                quantity=Decimal(str(data["quantity"])),
                entry_client_order_id=data.get("entry_client_order_id"),
                stop_client_algo_id=data.get("stop_client_algo_id"),
                take_client_algo_id=data.get("take_client_algo_id"),
                operational_state=OperationalState(str(operational_state)),
                protection_warning=str(data.get("protection_warning", "")),
                protection_source=str(data.get("protection_source", STATIC_PROTECTION_SOURCE)),
                trade_plan_id=data.get("trade_plan_id"),
                planned_stop_price=(
                    Decimal(str(data["planned_stop_price"]))
                    if data.get("planned_stop_price") is not None
                    else None
                ),
                planned_take_profit_price=(
                    Decimal(str(data["planned_take_profit_price"]))
                    if data.get("planned_take_profit_price") is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise ValueError(
                f"Arquivo de estado com campo obrigatório ausente ({exc}): {self.path}"
            ) from exc

    def save(self, state: RuntimeState) -> None:
        payload = asdict(state)
        payload["direction"] = state.direction.value
        payload["entry_price"] = str(state.entry_price)
        payload["quantity"] = str(state.quantity)
        payload["operational_state"] = state.operational_state.value

        if state.planned_stop_price is not None:
            payload["planned_stop_price"] = str(state.planned_stop_price)
        if state.planned_take_profit_price is not None:
            payload["planned_take_profit_price"] = str(state.planned_take_profit_price)

        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            tmp_path.replace(self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


def state_from_position(
    *,
    active_trade_id: str,
    symbol: str,
    direction: PositionDirection,
    opened_at: float,
    entry_price: Decimal,
    quantity: Decimal,
    entry_client_order_id: str | None,
    stop_client_algo_id: str | None = None,
    take_client_algo_id: str | None = None,
    operational_state: OperationalState = OperationalState.POSITION_OPEN_PROTECTED,
    protection_warning: str = "",
    protection_source: str = STATIC_PROTECTION_SOURCE,
    trade_plan_id: str | None = None,
    planned_stop_price: Decimal | None = None,
    planned_take_profit_price: Decimal | None = None,
) -> RuntimeState:
    return RuntimeState(
        active_trade_id=active_trade_id,
        symbol=symbol,
        direction=direction,
        opened_at=opened_at,
        entry_price=entry_price,
        quantity=quantity,
        entry_client_order_id=entry_client_order_id,
        stop_client_algo_id=stop_client_algo_id,
        take_client_algo_id=take_client_algo_id,
        operational_state=operational_state,
        protection_warning=protection_warning,
        protection_source=protection_source,
        trade_plan_id=trade_plan_id,
        planned_stop_price=planned_stop_price,
        planned_take_profit_price=planned_take_profit_price,
    )


def state_to_journal_data(state: RuntimeState | None) -> dict[str, Any] | None:
    if state is None:
        return None

    return {
        "active_trade_id": state.active_trade_id,
        "symbol": state.symbol,
        "direction": state.direction.value,
        "opened_at": state.opened_at,
        "entry_price": str(state.entry_price),
        "quantity": str(state.quantity),
        "entry_client_order_id": state.entry_client_order_id,
        "stop_client_algo_id": state.stop_client_algo_id,
        "take_client_algo_id": state.take_client_algo_id,
        "operational_state": state.operational_state.value,
        "protection_warning": state.protection_warning,
        "protection_source": state.protection_source,
        "trade_plan_id": state.trade_plan_id,
        "planned_stop_price": (
            str(state.planned_stop_price) if state.planned_stop_price is not None else None
        ),
        "planned_take_profit_price": (
            str(state.planned_take_profit_price)
            if state.planned_take_profit_price is not None
            else None
        ),
    }
