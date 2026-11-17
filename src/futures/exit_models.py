from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.models import TradeAction
from domain.symbol_rules import SymbolRules
from indicators.atr import CandleLike, average_true_range


@dataclass(frozen=True)
class ExitPrices:
    stop_price: Decimal
    take_profit_price: Decimal
    model: str
    fallback_used: bool = False
    warning: str = ""


class FixedPctExitModel:
    name = "FIXED_PCT"

    def calculate(
        self,
        *,
        action: TradeAction,
        entry_price: Decimal,
        rules: SymbolRules,
        stop_loss_pct: Decimal,
        take_profit_pct: Decimal,
        candles: Sequence[CandleLike] | None = None,
    ) -> ExitPrices:
        del candles
        if action == TradeAction.OPEN_LONG:
            stop_price = rules.round_price_down(entry_price * (Decimal("1") - stop_loss_pct))
            take_profit_price = rules.round_price_up(entry_price * (Decimal("1") + take_profit_pct))
        elif action == TradeAction.OPEN_SHORT:
            stop_price = rules.round_price_up(entry_price * (Decimal("1") + stop_loss_pct))
            take_profit_price = rules.round_price_down(
                entry_price * (Decimal("1") - take_profit_pct)
            )
        else:
            raise ValueError(f"Acao invalida para exit model: {action}")

        return ExitPrices(
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            model=self.name,
        )


class ATRExitModel:
    name = "ATR"

    def __init__(
        self,
        *,
        period: int,
        stop_multiplier: float,
        take_multiplier: float,
        fallback: FixedPctExitModel | None = None,
    ):
        if period < 1:
            raise ValueError("ATR period precisa ser no minimo 1")

        self.period = period
        self.stop_multiplier = Decimal(str(stop_multiplier))
        self.take_multiplier = Decimal(str(take_multiplier))
        self.fallback = fallback or FixedPctExitModel()

    def calculate(
        self,
        *,
        action: TradeAction,
        entry_price: Decimal,
        rules: SymbolRules,
        stop_loss_pct: Decimal,
        take_profit_pct: Decimal,
        candles: Sequence[CandleLike] | None = None,
    ) -> ExitPrices:
        atr = average_true_range(candles or [], self.period)
        if atr is None:
            return self._fallback(
                action=action,
                entry_price=entry_price,
                rules=rules,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                warning=(
                    "ATR sem historico suficiente. Usando FIXED_PCT para preservar "
                    "comportamento atual."
                ),
            )

        if action == TradeAction.OPEN_LONG:
            stop_price = rules.round_price_down(entry_price - atr * self.stop_multiplier)
            take_profit_price = rules.round_price_up(entry_price + atr * self.take_multiplier)
            valid = Decimal("0") < stop_price < entry_price < take_profit_price
        elif action == TradeAction.OPEN_SHORT:
            stop_price = rules.round_price_up(entry_price + atr * self.stop_multiplier)
            take_profit_price = rules.round_price_down(entry_price - atr * self.take_multiplier)
            valid = Decimal("0") < take_profit_price < entry_price < stop_price
        else:
            raise ValueError(f"Acao invalida para exit model: {action}")

        if not valid:
            # A zero ATR (flat candles) puts the stop on the entry; an outsized
            # ATR pushes a price to zero or below. Either order would be
            # rejected and leave the position unprotected.
            return self._fallback(
                action=action,
                entry_price=entry_price,
                rules=rules,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                warning=(
                    f"ATR={atr} gerou stop/take invalidos para a entrada {entry_price}. "
                    "Usando FIXED_PCT."
                ),
            )

        return ExitPrices(
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            model=self.name,
        )

    def _fallback(
        self,
        *,
        action: TradeAction,
        entry_price: Decimal,
        rules: SymbolRules,
        stop_loss_pct: Decimal,
        take_profit_pct: Decimal,
        warning: str,
    ) -> ExitPrices:
        fixed = self.fallback.calculate(
            action=action,
            entry_price=entry_price,
            rules=rules,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        return ExitPrices(
            stop_price=fixed.stop_price,
            take_profit_price=fixed.take_profit_price,
            model=self.name,
            fallback_used=True,
            warning=warning,
        )


def build_exit_model(config) -> FixedPctExitModel | ATRExitModel:
    if config.exit_model.value == "ATR":
        return ATRExitModel(
            period=config.atr_period,
            stop_multiplier=config.atr_stop_multiplier,
            take_multiplier=config.atr_take_multiplier,
        )

    return FixedPctExitModel()
