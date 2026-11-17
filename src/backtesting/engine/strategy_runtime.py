from dataclasses import dataclass, field
from typing import Any

from backtesting.domain.config import BacktestStrategyConfig
from domain.models import FuturesPosition, MarketSnapshot, TradeAction, TradeDecision
from domain.strategy_requirements import StrategyDataRequirements
from scanner.signal_score import SignalScore
from strategies.loader import load_strategy
from trading.intent import TradeIntent


@dataclass(frozen=True)
class BacktestStrategyEvaluation:
    score: SignalScore
    decision: TradeDecision
    metadata: dict[str, Any] = field(default_factory=dict)


class BacktestStrategyRuntime:
    """Executa estrategia global com estado isolado por simbolo.

    O backtesting preserva o contrato do Governor real: score prioriza atenção e
    decide confirma a ação operacional. A chamada fica centralizada em evaluate()
    para que cada candle/símbolo tenha uma avaliação auditável e sem chamadas
    espalhadas pelo simulador.
    """

    def __init__(self, config: BacktestStrategyConfig, symbols: list[str]):
        self.config = config
        self.module_name, self.class_name, self.strategy_params = config.load_target()
        self.strategy_name = f"{self.module_name}.{self.class_name}"
        self.strategy = load_strategy(
            self.module_name,
            self.class_name,
            self.strategy_params,
        )
        self._has_score_symbol = callable(getattr(self.strategy, "score_symbol", None))
        self._has_intent_symbol = callable(getattr(self.strategy, "intent_symbol", None))
        self._has_decide_symbol = callable(getattr(self.strategy, "decide_symbol", None))
        self._uses_symbol_api = self._has_score_symbol and (
            self._has_intent_symbol or self._has_decide_symbol
        )
        if not self._uses_symbol_api:
            raise TypeError(
                "BacktestStrategyRuntime exige score_symbol() e intent_symbol() ou decide_symbol(). "
                "Estratégias sem contrato por símbolo não fazem parte do fluxo atual."
            )
        self._states: dict[str, dict[str, Any]] = {symbol: {} for symbol in symbols}

    @property
    def mode(self) -> str:
        return self.config.normalized_mode

    @property
    def metadata(self):
        return getattr(self.strategy, "metadata", None)

    @property
    def required_timeframes(self) -> list[str]:
        values = {
            timeframe
            for requirement in self.requirements_by_symbol.values()
            for timeframe in requirement.required_candles
        }
        return sorted(values)

    @property
    def required_history(self) -> int:
        return max(
            (
                count
                for requirement in self.requirements_by_symbol.values()
                for count in requirement.required_candles.values()
            ),
            default=0,
        )

    @property
    def requirements_by_symbol(self) -> dict[str, StrategyDataRequirements]:
        return {symbol: self._requirements_for_symbol(symbol) for symbol in self._states}

    def _requirements_for_symbol(self, symbol: str) -> StrategyDataRequirements:
        method = getattr(self.strategy, "requirements_for_symbol", None)
        if callable(method):
            try:
                return method(symbol)
            except TypeError:
                return method(symbol, None)
        method = getattr(self.strategy, "data_requirements", None)
        if callable(method):
            return method()
        metadata = self.metadata
        if metadata is not None and callable(getattr(metadata, "to_data_requirements", None)):
            return metadata.to_data_requirements()
        values = getattr(metadata, "required_timeframes", []) or ["1m"]
        history = max(1, int(getattr(metadata, "required_history", 1) or 1))
        primary = str(values[0] if values else "1m")
        return StrategyDataRequirements(
            primary_timeframe=primary,
            required_candles={str(value): history for value in values},
        )

    def score(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
    ) -> SignalScore:
        symbol = symbol.upper().strip()
        result = self.strategy.score_symbol(
            symbol,
            market,
            position,
            self._state(symbol),
        )
        return _coerce_score(result, symbol, self.strategy_name)

    def decide(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
    ) -> TradeDecision:
        symbol = symbol.upper().strip()
        intent_symbol = getattr(self.strategy, "intent_symbol", None)
        if callable(intent_symbol):
            result = intent_symbol(symbol, market, position, self._state(symbol))
            return _decision_from_intent(_coerce_intent(result, symbol, self.strategy_name))
        if not self._has_decide_symbol:
            raise TypeError("Estratégia sem decide_symbol(); use intent_symbol().")
        return self.strategy.decide_symbol(
            symbol,
            market,
            position,
            self._state(symbol),
        )

    def evaluate(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
    ) -> BacktestStrategyEvaluation:
        score = self.score(symbol, market, position)
        decision = self.decide(symbol, market, position)
        return BacktestStrategyEvaluation(
            score=score,
            decision=decision,
            metadata=_evaluation_metadata(score, decision),
        )

    def _state(self, symbol: str) -> dict[str, Any]:
        return self._states.setdefault(symbol.upper().strip(), {})


def _coerce_score(result: Any, symbol: str, strategy_name: str) -> SignalScore:
    if isinstance(result, SignalScore):
        if result.strategy:
            return result
        return SignalScore(
            symbol=result.symbol,
            score=result.score,
            priority=result.priority,
            reason=result.reason,
            suggested_next_check_seconds=result.suggested_next_check_seconds,
            data_age_seconds=result.data_age_seconds,
            action=result.action,
            confidence=result.confidence,
            strategy=strategy_name,
            timestamp=result.timestamp,
            metadata=result.metadata,
        )

    if isinstance(result, dict):
        payload = dict(result)
        payload.setdefault("symbol", symbol)
        payload.setdefault("strategy", strategy_name)
        return SignalScore(**payload)

    raise TypeError("score_symbol precisa retornar SignalScore ou dict")


def _coerce_intent(result: Any, symbol: str, strategy_name: str) -> TradeIntent:
    if isinstance(result, TradeIntent):
        return result
    if isinstance(result, dict):
        payload = dict(result)
        payload.setdefault("symbol", symbol)
        payload.setdefault("strategy_name", strategy_name)
        return TradeIntent(**payload)
    raise TypeError("intent_symbol precisa retornar TradeIntent ou dict")


def _decision_from_intent(intent: TradeIntent) -> TradeDecision:
    action = TradeAction(intent.action)
    if action is TradeAction.OPEN_LONG:
        return TradeDecision.open_long(intent.reason)
    if action is TradeAction.OPEN_SHORT:
        return TradeDecision.open_short(intent.reason)
    if action is TradeAction.CLOSE_POSITION:
        return TradeDecision.close(intent.reason)
    return TradeDecision.hold(intent.reason)


def _evaluation_metadata(score: SignalScore, decision: TradeDecision) -> dict[str, Any]:
    metadata = {
        "score_decide_contract": "SCORE_THEN_INTENT_SHARED_SYMBOL_STATE",
    }
    score_action = str(score.action or "").upper().strip()
    decision_action = decision.action.value
    if (
        score_action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"}
        and score_action != decision_action
    ):
        metadata["score_decide_warning"] = "SCORE_ACTION_DIVERGED_FROM_DECISION_ACTION"
    return metadata
