from dataclasses import dataclass, field
from typing import Any

from config import AppConfig, ExecutionMode
from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition, MarketSnapshot, TradeDecision
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from domain.timeframe import is_signal_timeframe_allowed_for_real
from scanner.signal_score import SignalScore
from strategies.loader import StrategyLoadError, load_strategy
from trading.intent import TradeIntent
from trading_cadence import MIN_REAL_SIGNAL_TIMEFRAME, is_test_strategy
from utils.events import EventChannel, observe_error, observe_event
from utils.logger import get_logger

_OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


@dataclass
class SymbolStrategyState:
    data: dict[str, Any] = field(default_factory=dict)


class StrategyCoordinator:
    """Apply one global strategy to many symbols with isolated memory."""

    def __init__(
        self,
        *,
        config: AppConfig,
        symbol_configs: list[SymbolTradingConfig],
    ):
        self.config = config
        self.symbol_configs = symbol_configs
        self.global_strategy_name = f"{config.strategy_module}.{config.strategy_class}"
        self.logger = get_logger(__name__)
        self._global_strategy = load_strategy(
            config.strategy_module,
            config.strategy_class,
            config.strategy_params,
        )
        self._has_score_symbol = callable(getattr(self._global_strategy, "score_symbol", None))
        self._has_intent_symbol = callable(getattr(self._global_strategy, "intent_symbol", None))
        self._has_decide_symbol = callable(getattr(self._global_strategy, "decide_symbol", None))
        self._uses_symbol_api = self._has_score_symbol and (
            self._has_intent_symbol or self._has_decide_symbol
        )
        if not self._uses_symbol_api:
            raise TypeError(
                "StrategyCoordinator exige score_symbol() e intent_symbol() ou decide_symbol(). "
                f"Estrategia recebida: {self.global_strategy_name}"
            )
        self._real_money = getattr(config, "execution_mode", None) is ExecutionMode.REAL_LIVE
        if self._real_money and is_test_strategy(self._global_strategy):
            raise StrategyLoadError(
                "Estrategias de teste (sinais aleatorios/roteirizados) nunca podem operar "
                f"em REAL_LIVE. Estrategia recebida: {self.global_strategy_name}"
            )
        self._states: dict[str, SymbolStrategyState] = {
            item.symbol: SymbolStrategyState() for item in symbol_configs
        }
        self._rotation_board: dict[str, dict[str, Any]] = {}
        # Tracks last primary candle close_time_ms evaluated per symbol
        # (used for ON_PRIMARY_CANDLE_CLOSE policy — avoids repeating same candle)
        self._last_evaluated_close_ms: dict[str, int] = {}
        self.profile_strategy_warnings = self._profile_strategy_warnings()

        self.runtime_mode = "GLOBAL_SYMBOL_API"
        self.state_mode = "PER_SYMBOL_STATE"
        self.logger.info(
            "StrategyCoordinator ativo | global_strategy=%s | mode=%s | state=%s",
            self.global_strategy_name,
            self.runtime_mode,
            self.state_mode,
        )

        for warning in self.profile_strategy_warnings:
            self.logger.warning(warning)
        if getattr(config, "ai_probabilistic_enabled", False):
            self.runtime_mode = "PROBABILISTIC_AI"
            self.logger.warning(
                "IA probabilistica ativa: estrategias fixas ficam fora do fluxo operacional."
            )

    def begin_cycle(self) -> None:
        self._rotation_board.clear()

    def should_evaluate(
        self,
        symbol: str,
        market: MarketSnapshot,
        symbol_config: SymbolTradingConfig | None = None,
    ) -> tuple[bool, str]:
        """Return (True, "") when the strategy should run, or (False, reason) to skip.

        Called before score/intent. Safety paths (stop, take, protection, reconcile)
        must never depend on this — they bypass strategy evaluation entirely.
        """
        req = self.requirements_for_symbol(symbol, symbol_config=symbol_config)

        if req.evaluation_policy != EVALUATION_ON_PRIMARY_CANDLE_CLOSE:
            return True, ""

        candles = market.candles
        if candles is None:
            return True, ""

        primary_tf = req.primary_timeframe
        series = candles.get(primary_tf)
        if not series:
            return False, "CANDLE_DATA_UNAVAILABLE"

        required_count = max(1, int(req.required_candles.get(primary_tf, 1) or 1))
        if len(series) < required_count:
            return False, "CANDLE_HISTORY_INCOMPLETE"

        last_candle = series[-1]
        if not last_candle.is_closed:
            return False, "WAITING_FOR_PRIMARY_CANDLE_CLOSE"

        last_close_ms = last_candle.close_time_ms
        sym_upper = symbol.upper()
        if self._last_evaluated_close_ms.get(sym_upper) == last_close_ms:
            return False, "ALREADY_EVALUATED_THIS_CANDLE"

        return True, ""

    def mark_evaluated(self, symbol: str, market: MarketSnapshot) -> None:
        """Record that we just evaluated this candle — prevents duplicate signals."""
        req = self.requirements_for_symbol(symbol)
        if req.evaluation_policy != EVALUATION_ON_PRIMARY_CANDLE_CLOSE:
            return
        candles = market.candles
        if not candles:
            return
        series = candles.get(req.primary_timeframe)
        if series and series[-1].is_closed:
            self._last_evaluated_close_ms[symbol.upper()] = series[-1].close_time_ms
            observe_event(
                "strategy_evaluated_on_candle_close",
                component=__name__,
                category="strategy",
                channel=EventChannel.AUDIT,
                symbol=symbol,
                primary_timeframe=req.primary_timeframe,
                close_time_ms=series[-1].close_time_ms,
            )

    def requirements_for_symbol(
        self,
        symbol: str,
        *,
        symbol_config: SymbolTradingConfig | None = None,
    ) -> StrategyDataRequirements:
        strategy = self._global_strategy
        normalized_symbol = str(symbol or "").upper().strip()
        req_method = getattr(strategy, "requirements_for_symbol", None)
        if callable(req_method):
            try:
                return req_method(normalized_symbol, symbol_config)
            except TypeError:
                return req_method(normalized_symbol)

        data_requirements = getattr(strategy, "data_requirements", None)
        if callable(data_requirements):
            return data_requirements()

        metadata = getattr(strategy, "metadata", None)
        if metadata is not None and callable(getattr(metadata, "to_data_requirements", None)):
            return metadata.to_data_requirements()

        return StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 1},
        )

    def requirements_by_symbol(self) -> dict[str, StrategyDataRequirements]:
        return {
            symbol_config.symbol.upper().strip(): self.requirements_for_symbol(
                symbol_config.symbol,
                symbol_config=symbol_config,
            )
            for symbol_config in self.symbol_configs
        }

    def score(
        self,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        position: FuturesPosition,
    ) -> SignalScore:
        state = self._state_data_for_symbol(symbol_config)
        try:
            result = self._global_strategy.score_symbol(
                symbol_config.symbol,
                market,
                position,
                state,
            )
            score = _coerce_score(result, market.symbol, self.global_strategy_name)
        except Exception as error:
            observe_error(
                "strategy_error",
                error=error,
                component=__name__,
                category="strategy",
                channel=EventChannel.AUDIT,
                symbol=symbol_config.symbol,
                strategy=self.global_strategy_name,
                operation="score_symbol",
            )
            raise
        observe_event(
            "strategy_score_evaluated",
            component=__name__,
            category="strategy",
            channel=EventChannel.AUDIT,
            symbol=symbol_config.symbol,
            strategy=self.global_strategy_name,
            score=score.score,
            priority=score.priority.value if score.priority is not None else None,
            action=score.action,
            reason=score.reason,
        )
        return score

    def _decide_symbol(
        self,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        position: FuturesPosition,
    ) -> TradeDecision:
        if not self._has_decide_symbol:
            raise TypeError(
                "Estrategia sem decide_symbol(); use intent_symbol() como contrato canonico."
            )
        state = self._state_data_for_symbol(symbol_config)
        try:
            decision = self._global_strategy.decide_symbol(
                symbol_config.symbol,
                market,
                position,
                state,
            )
        except Exception as error:
            observe_error(
                "strategy_error",
                error=error,
                component=__name__,
                category="strategy",
                channel=EventChannel.AUDIT,
                symbol=symbol_config.symbol,
                strategy=self.global_strategy_name,
                operation="decide_symbol",
            )
            raise
        observe_event(
            "strategy_decision_created",
            component=__name__,
            category="strategy",
            channel=EventChannel.AUDIT,
            symbol=symbol_config.symbol,
            strategy=self.global_strategy_name,
            decision_action=getattr(decision, "action", ""),
            reason=getattr(decision, "reason", ""),
        )
        return decision

    def intent(
        self,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        position: FuturesPosition,
        *,
        score: SignalScore | None = None,
        decision: TradeDecision | None = None,
    ) -> TradeIntent:
        return self._enforce_real_money_cadence(
            symbol_config,
            self._build_intent(
                symbol_config,
                market,
                position,
                score=score,
                decision=decision,
            ),
        )

    def _enforce_real_money_cadence(
        self,
        symbol_config: SymbolTradingConfig,
        intent: TradeIntent,
    ) -> TradeIntent:
        """Refuse real-money entries driven by sub-day-trade timeframes.

        Closing actions are never touched: exits must always remain possible.
        """
        if not self._real_money or intent.action not in _OPEN_ACTIONS:
            return intent
        try:
            primary_timeframe = self.requirements_for_symbol(
                symbol_config.symbol,
                symbol_config=symbol_config,
            ).primary_timeframe
        except Exception as error:
            primary_timeframe = ""
            self.logger.warning(
                "Nao foi possivel resolver o timeframe da estrategia | symbol=%s | error=%s",
                symbol_config.symbol,
                error,
            )
        if is_signal_timeframe_allowed_for_real(primary_timeframe):
            return intent
        reason = (
            f"Entrada bloqueada: timeframe {primary_timeframe or 'desconhecido'} abaixo do "
            f"minimo de day trade ({MIN_REAL_SIGNAL_TIMEFRAME}) para dinheiro real"
        )
        observe_event(
            "strategy_entry_blocked_by_cadence_policy",
            component=__name__,
            category="strategy",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            symbol=symbol_config.symbol,
            strategy=self.global_strategy_name,
            primary_timeframe=primary_timeframe,
            minimum_timeframe=MIN_REAL_SIGNAL_TIMEFRAME,
            blocked_action=intent.action,
        )
        metadata = dict(intent.metadata)
        metadata["cadence_policy_blocked_action"] = intent.action
        return intent.with_updates(action="HOLD", reason=reason, metadata=metadata)

    def _build_intent(
        self,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        position: FuturesPosition,
        *,
        score: SignalScore | None = None,
        decision: TradeDecision | None = None,
    ) -> TradeIntent:
        state = self._state_data_for_symbol(symbol_config)
        intent_symbol = getattr(self._global_strategy, "intent_symbol", None)
        if callable(intent_symbol):
            result = intent_symbol(symbol_config.symbol, market, position, state)
            return _enrich_intent(
                _coerce_intent(result, symbol_config.symbol, self.global_strategy_name),
                symbol_config=symbol_config,
                market=market,
                score=score,
                strategy_name=self.global_strategy_name,
            )

        confidence = score.confidence if score is not None else None
        if confidence is None and score is not None:
            confidence = int(score.score)

        metadata: dict[str, Any] = {
            "symbol_strategy_params": dict(symbol_config.strategy_params),
            "strategy_runtime_mode": self.runtime_mode,
        }
        if score is not None:
            metadata.update(
                {
                    "score": int(score.score),
                    "score_reason": score.reason,
                    "score_action": score.action,
                    "priority": score.priority.value if score.priority is not None else None,
                }
            )
            if score.metadata:
                metadata["score_metadata"] = dict(score.metadata)

        if decision is None:
            decision = self._decide_symbol(symbol_config, market, position)
        confidence_value = int(confidence if confidence is not None else 0)

        return _intent_for_trade_decision(
            symbol=symbol_config.symbol,
            decision=decision,
            confidence=confidence_value,
            strategy_name=self.global_strategy_name,
            metadata=metadata,
        ).with_updates(
            score=int(score.score) if score is not None else confidence_value,
            priority=score.priority if score is not None else None,
            market_price=market.price,
            suggested_next_check_seconds=(
                score.suggested_next_check_seconds if score is not None else 0.0
            ),
        )

    def state_for_symbol(self, symbol: str) -> SymbolStrategyState:
        normalized = symbol.upper().strip()
        if normalized not in self._states:
            self._states[normalized] = SymbolStrategyState()
        return self._states[normalized]

    def _state_data_for_symbol(self, symbol_config: SymbolTradingConfig) -> dict[str, Any]:
        state = self.state_for_symbol(symbol_config.symbol).data
        state["symbol_strategy_params"] = dict(symbol_config.strategy_params)
        state["rotation_board"] = self._rotation_board
        state["monitored_symbols"] = [item.symbol for item in self.symbol_configs]
        state["symbol_operational_profile"] = {
            "position_notional_usdt": symbol_config.position_notional_usdt,
            "stop_loss_pct": symbol_config.stop_loss_pct,
            "take_profit_pct": symbol_config.take_profit_pct,
            "leverage": symbol_config.leverage,
            "max_position_hold_seconds": symbol_config.max_position_hold_seconds,
            "cooldown_after_loss_seconds": symbol_config.cooldown_after_loss_seconds,
        }
        return state

    def _profile_strategy_warnings(self) -> list[str]:
        warnings = []
        for item in self.symbol_configs:
            if item.strategy_params:
                warnings.append(
                    "Perfil de moeda contem strategy_params locais suportados apenas como "
                    f"contexto da estrategia global | symbol={item.symbol} | "
                    f"global_strategy={self.global_strategy_name}"
                )
        return warnings


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


def _intent_for_trade_decision(
    *,
    symbol: str,
    decision: TradeDecision,
    confidence: int,
    strategy_name: str,
    metadata: dict[str, Any] | None = None,
) -> TradeIntent:
    return TradeIntent(
        symbol=symbol,
        action=decision.action.value,
        confidence=confidence,
        score=confidence,
        reason=decision.reason,
        strategy_name=strategy_name,
        metadata=metadata or {},
    )


def _enrich_intent(
    intent: TradeIntent,
    *,
    symbol_config: SymbolTradingConfig,
    market: MarketSnapshot,
    score: SignalScore | None,
    strategy_name: str,
) -> TradeIntent:
    metadata = {
        "symbol_strategy_params": dict(symbol_config.strategy_params),
        "strategy_runtime_mode": "GLOBAL_SYMBOL_API",
        **dict(intent.metadata),
    }
    if score is not None:
        metadata.update(
            {
                "score": int(score.score),
                "score_reason": score.reason,
                "score_action": score.action,
                "priority": score.priority.value if score.priority is not None else None,
            }
        )
        if score.metadata:
            metadata["score_metadata"] = dict(score.metadata)
    return intent.with_updates(
        symbol=symbol_config.symbol,
        strategy_name=intent.strategy_name or strategy_name,
        score=int(score.score) if score is not None else intent.score,
        priority=score.priority if score is not None else intent.priority,
        market_price=market.price if intent.market_price is None else intent.market_price,
        suggested_next_check_seconds=(
            score.suggested_next_check_seconds
            if score is not None
            else intent.suggested_next_check_seconds
        ),
        metadata=metadata,
    )
