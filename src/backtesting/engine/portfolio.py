from dataclasses import asdict
from decimal import Decimal
from typing import Any

from backtesting.domain.config import BacktestStrategyConfig, BacktestSymbolConfig
from config import (
    ExitModelType,
    MarginType,
    ProtectionPolicy,
    ProtectionWorkingType,
    RecoveryAction,
)
from config.symbol_context import PortfolioRiskSettings, SymbolTradingConfig
from governor.models import GovernorDecision
from governor.portfolio_risk_engine import PortfolioRiskEngine
from trading.intent import TradeIntent


class PortfolioSimulator:
    def __init__(
        self,
        *,
        symbols: list[BacktestSymbolConfig],
        strategy: BacktestStrategyConfig,
        settings: PortfolioRiskSettings,
        initial_balance: str,
    ):
        self.settings = settings
        self.initial_balance = initial_balance
        self.new_positions_this_cycle = 0
        self.symbol_contexts = [
            _symbol_context(symbol_config, strategy) for symbol_config in symbols
        ]
        self._context_by_symbol = {context.symbol: context for context in self.symbol_contexts}
        self.engine = PortfolioRiskEngine(settings)

    def start_cycle(self) -> None:
        self.new_positions_this_cycle = 0

    def record_new_position(self) -> None:
        self.new_positions_this_cycle += 1

    def evaluate(
        self,
        intent: TradeIntent,
        *,
        open_positions: list[dict[str, Any]],
        available_balance_usdt: str,
        daily_realized_pnl: str,
        daily_trades: int,
        consecutive_losses: int,
        override_notional: Decimal | None = None,
    ) -> GovernorDecision:
        account_snapshot = {
            "open_positions": open_positions,
            "available_balance_usdt": available_balance_usdt,
            "daily_realized_pnl": daily_realized_pnl,
            "daily_trades": daily_trades,
            "consecutive_losses": consecutive_losses,
            "new_positions_this_cycle": self.new_positions_this_cycle,
        }
        capital_block = self._capital_reserved_guard(
            intent,
            available_balance_usdt=available_balance_usdt,
            account_snapshot=account_snapshot,
            override_notional=override_notional,
        )
        if capital_block is not None:
            return capital_block

        return self.engine.evaluate_trade_intent(
            intent,
            self.symbol_contexts,
            account_snapshot,
            override_notional=override_notional,
        )

    def _capital_reserved_guard(
        self,
        intent: TradeIntent,
        *,
        available_balance_usdt: str,
        account_snapshot: dict[str, Any],
        override_notional: Decimal | None = None,
    ) -> GovernorDecision | None:
        if intent.action not in {"OPEN_LONG", "OPEN_SHORT"}:
            return None

        context = self._context_by_symbol.get(intent.symbol)
        if context is None:
            return None

        available = Decimal(str(available_balance_usdt))
        candidate_notional = (
            Decimal(str(override_notional))
            if override_notional is not None
            else Decimal(str(intent.metadata.get("notional") or context.position_notional_usdt))
        )
        leverage = Decimal(str(max(1, int(context.leverage))))
        required_margin = candidate_notional / leverage
        required_with_safety = required_margin * Decimal("1.10")
        if available >= required_with_safety:
            return None

        checks = {
            "available_balance_usdt": str(available),
            "candidate_notional": str(candidate_notional),
            "leverage": str(leverage),
            "required_margin_usdt": str(required_margin),
            "required_margin_with_safety_usdt": str(required_with_safety),
            "reserved_capital_guard": True,
            "bloqueia_novas_entradas": True,
            "bloqueia_fechamentos": False,
        }
        return GovernorDecision(
            allowed=False,
            symbol=intent.symbol,
            action=intent.action,
            reason="BACKTEST_AVAILABLE_BALANCE_RESERVED",
            checks=checks,
            portfolio_snapshot=asdict(self.engine.snapshot(account_snapshot)),
        )


def _symbol_context(
    symbol_config: BacktestSymbolConfig,
    strategy: BacktestStrategyConfig,
) -> SymbolTradingConfig:
    _, _, params = strategy.load_target()
    return SymbolTradingConfig(
        enabled=True,
        symbol=symbol_config.symbol,
        strategy_params=params,
        leverage=symbol_config.leverage,
        margin_type=MarginType.ISOLATED,
        position_notional_usdt=float(symbol_config.position_notional_usdt),
        stop_loss_pct=float(symbol_config.stop_loss_pct),
        take_profit_pct=float(symbol_config.take_profit_pct),
        exit_model=ExitModelType.FIXED_PCT,
        atr_period=14,
        atr_stop_multiplier=1.5,
        atr_take_multiplier=2.0,
        protection_policy=ProtectionPolicy.AUTO_REPAIR,
        protection_working_type=ProtectionWorkingType.MARK_PRICE,
        recovery_action=RecoveryAction.PROTECT,
        max_daily_loss_usdt=0.0,
        max_daily_trades=0,
        max_consecutive_losses_guard=0,
        cooldown_after_loss_seconds=0.0,
        max_position_hold_seconds=0.0,
        max_exposure_usdt=0.0,
        bot_order_prefix=f"BT_{symbol_config.symbol[:8]}",
        state_path=f"runtime/backtest_state_{symbol_config.symbol.lower()}.json",
        journal_path=f"data/backtest_trades_{symbol_config.symbol.lower()}.jsonl",
        lock_path=f"runtime/backtest_{symbol_config.symbol.lower()}.lock",
        runtime_dir="runtime",
    )
