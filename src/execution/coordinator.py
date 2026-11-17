"""Routes approved commands to the per-symbol execution port.

The port owns the exchange engine and its PositionLedger. The coordinator owns
the final action dispatch so the Governor's stage does not need to select a
mutation path or interpret an order lifecycle.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from config.symbol_context import SymbolTradingConfig
from domain.models import TradeAction


class ExecutionCommandLike(Protocol):
    @property
    def action(self) -> TradeAction: ...


CommandT = TypeVar("CommandT", bound=ExecutionCommandLike)
CommandCoT = TypeVar("CommandCoT", bound=ExecutionCommandLike, covariant=True)


class ExecutableTradeLike(Protocol[CommandCoT]):
    @property
    def execution_command(self) -> CommandCoT: ...


SnapshotT = TypeVar("SnapshotT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class ExecutionCoordinator(Generic[CommandT, SnapshotT, ResultT]):
    open_trade: Callable[[SymbolTradingConfig, SnapshotT, CommandT], ResultT]
    close_trade: Callable[[SymbolTradingConfig, SnapshotT, CommandT], ResultT]
    ignore_trade: Callable[[SymbolTradingConfig, SnapshotT, CommandT], ResultT]

    def execute_trade(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: SnapshotT,
        trade: ExecutableTradeLike[CommandT],
    ) -> ResultT:
        return self.execute(
            symbol_config=symbol_config,
            heavy_snapshot=heavy_snapshot,
            command=trade.execution_command,
        )

    def execute(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: SnapshotT,
        command: CommandT,
    ) -> ResultT:
        action = TradeAction(command.action)
        if action in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT}:
            return self.open_trade(symbol_config, heavy_snapshot, command)
        if action is TradeAction.CLOSE_POSITION:
            return self.close_trade(symbol_config, heavy_snapshot, command)
        return self.ignore_trade(symbol_config, heavy_snapshot, command)
