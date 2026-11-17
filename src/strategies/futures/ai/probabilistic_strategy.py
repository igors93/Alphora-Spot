from typing import Any

from domain.ai.probabilistic import (
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_MODEL_TYPE,
    no_action_prediction,
    predict_probabilistic_event,
)
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from domain.timeframe import is_supported_timeframe, timeframe_duration_ms
from scanner.signal_score import SignalScore, clamp_score
from strategies.futures.ai.probabilistic_resolver import (
    ProbabilisticAIModelInputContractMissing,
    ProbabilisticAIModelResolver,
    ProbabilisticAIModelSchemaOutdated,
    ProbabilisticAIModelTimeframeMismatch,
    ProbabilisticAIModelUnavailable,
    ProbabilisticAIRefinedGlobalTimeframeMismatch,
    ResolvedProbabilisticAIModel,
)
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata

# Predictions that carry no information: the strategy never acts on them.
UNUSABLE_PREDICTION_REASONS = frozenset(
    {
        "AI_MODEL_INPUT_CONTRACT_MISSING",
        "AI_MODEL_SCHEMA_OUTDATED",
        "AI_MODEL_TIMEFRAME_MISMATCH",
        "AI_MODEL_UNAVAILABLE",
        "AI_INSUFFICIENT_HISTORY",
        "AI_REFINED_GLOBAL_TIMEFRAME_MISMATCH",
        "AI_OHLCV_REQUIRED",
        "AI_OHLCV_UNAVAILABLE",
    }
)


_ENTRY_STATE_KEY = "ai_position_entry"
_PENDING_ENTRY_STATE_KEY = "ai_pending_entry"
_OPEN_ACTIONS = {"OPEN_LONG": "LONG", "OPEN_SHORT": "SHORT"}


class ProbabilisticAIStrategy(FuturesStrategyV2):
    """Generate probabilistic event signals. It never executes orders."""

    strategy_category = "AI_PROBABILISTIC"
    hide_from_fixed_strategy_list = True

    metadata = StrategyMetadata(
        strategy_name="ProbabilisticAIStrategy",
        supports_scoring=True,
        supports_long=True,
        supports_short=True,
        required_history=20,
        required_timeframes=["1m"],
        required_indicators=[PROBABILISTIC_MODEL_TYPE],
    )

    def __init__(
        self,
        *,
        global_model_path: str | None = None,
        symbol_model_paths: dict[str, str] | None = None,
        use_symbol_refined_models: bool = True,
        fallback_to_global_model: bool = True,
        min_long_probability: float = 0.62,
        min_short_probability: float = 0.62,
        min_direction_gap: float = 0.18,
        min_expected_value_bps: float = 3.0,
        close_long_min_short_probability: float = 0.62,
        close_short_min_long_probability: float = 0.62,
        close_long_max_long_probability: float | None = None,
        close_short_max_short_probability: float | None = None,
        min_history: int | None = None,
        min_hold_candles: int | None = None,
        close_on_probability_fade: bool = False,
    ):
        self.global_model_path = str(global_model_path or "")
        self.model_resolver = ProbabilisticAIModelResolver(
            global_model_path=global_model_path,
            symbol_model_paths=symbol_model_paths,
            use_symbol_refined_models=use_symbol_refined_models,
            fallback_to_global_model=fallback_to_global_model,
        )
        self.min_long_probability = _probability(min_long_probability)
        self.min_short_probability = _probability(min_short_probability)
        self.min_direction_gap = _probability(min_direction_gap)
        self.min_expected_value_bps = float(min_expected_value_bps)
        self.close_long_min_short_probability = _probability(close_long_min_short_probability)
        self.close_short_min_long_probability = _probability(close_short_min_long_probability)
        self.close_long_max_long_probability = (
            _probability(close_long_max_long_probability)
            if close_long_max_long_probability is not None
            else max(0.0, self.min_long_probability - self.min_direction_gap)
        )
        self.close_short_max_short_probability = (
            _probability(close_short_max_short_probability)
            if close_short_max_short_probability is not None
            else max(0.0, self.min_short_probability - self.min_direction_gap)
        )
        self.min_history = int(min_history or 0)
        # The model predicts the move over ``horizon_candles``: the position is held that long
        # (a renewed same-side signal restarts the window). ``None`` = the model horizon.
        self.min_hold_candles = None if min_hold_candles is None else max(0, int(min_hold_candles))
        self.close_on_probability_fade = bool(close_on_probability_fade)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "ProbabilisticAIStrategy":
        return cls(
            global_model_path=_optional_str(params.get("global_model_path")),
            symbol_model_paths=_optional_dict(params.get("symbol_model_paths")),
            use_symbol_refined_models=_optional_bool(
                params.get("use_symbol_refined_models"),
                default=True,
            ),
            fallback_to_global_model=_optional_bool(
                params.get("fallback_to_global_model"),
                default=True,
            ),
            min_long_probability=_optional_float(
                params.get("min_long_probability"),
                default=0.62,
            ),
            min_short_probability=_optional_float(
                params.get("min_short_probability"),
                default=0.62,
            ),
            min_direction_gap=_optional_float(
                params.get("min_direction_gap"),
                default=0.18,
            ),
            min_expected_value_bps=_optional_float(
                params.get("min_expected_value_bps"),
                default=3.0,
            ),
            close_long_min_short_probability=_optional_float(
                params.get("close_long_min_short_probability"),
                default=0.62,
            ),
            close_short_min_long_probability=_optional_float(
                params.get("close_short_min_long_probability"),
                default=0.62,
            ),
            close_long_max_long_probability=_optional_float_or_none(
                params.get("close_long_max_long_probability"),
            ),
            close_short_max_short_probability=_optional_float_or_none(
                params.get("close_short_max_short_probability"),
            ),
            min_history=_optional_int(params.get("min_history")),
            min_hold_candles=_optional_int(params.get("min_hold_candles")),
            close_on_probability_fade=_optional_bool(
                params.get("close_on_probability_fade"),
                default=False,
            ),
        )

    def score_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> SignalScore:
        cache_key = _prediction_cache_key(market, position)
        prediction = self._predict(market)
        action = self._action_for_prediction(prediction, position, state, market)
        _remember_prediction(state, cache_key, prediction, action)

        if prediction["reason"] in UNUSABLE_PREDICTION_REASONS:
            return SignalScore(
                symbol=symbol,
                score=10,
                reason=prediction["reason"],
                action="NO_ACTION",
                confidence=0,
                strategy=self.__class__.__name__,
                metadata=_prediction_metadata(prediction, strategy_action="NO_ACTION"),
            )

        score_value = _score_value(prediction, action)
        return SignalScore(
            symbol=symbol,
            score=score_value,
            reason=self._reason(action, prediction),
            action=action,
            confidence=clamp_score(float(prediction.get("confidence", 0.0)) * 100.0),
            strategy=self.__class__.__name__,
            metadata=_prediction_metadata(prediction, strategy_action=action),
        )

    def decide_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeDecision:
        cache_key = _prediction_cache_key(market, position)
        cached = state.get("last_ai_probability_evaluation", {})
        if (
            isinstance(cached, dict)
            and cached.get("key") == cache_key
            and isinstance(cached.get("prediction"), dict)
        ):
            prediction = dict(cached["prediction"])
            action = str(cached.get("strategy_action", "") or "")
        else:
            prediction = self._predict(market)
            action = self._action_for_prediction(prediction, position, state, market)

        if not action:
            action = self._action_for_prediction(prediction, position, state, market)
        _remember_prediction(state, cache_key, prediction, action)

        if prediction["reason"] in UNUSABLE_PREDICTION_REASONS:
            return TradeDecision.hold(prediction["reason"])
        if action == "OPEN_LONG":
            return TradeDecision.open_long(self._reason(action, prediction))
        if action == "OPEN_SHORT":
            return TradeDecision.open_short(self._reason(action, prediction))
        if action == "CLOSE_POSITION":
            return TradeDecision.close(self._reason(action, prediction))
        return TradeDecision.hold(self._reason(action, prediction))

    def data_requirements(self) -> StrategyDataRequirements:
        return StrategyDataRequirements(
            primary_timeframe=self.metadata.required_timeframes[0]
            if self.metadata.required_timeframes
            else "1m",
            required_candles={
                tf: max(1, self.metadata.required_history)
                for tf in (self.metadata.required_timeframes or ["1m"])
            },
            evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
            closed_candles_only=True,
        )

    def requirements_for_symbol(
        self,
        symbol: str,
        symbol_config: Any | None = None,
    ) -> StrategyDataRequirements:
        """Return per-symbol requirements derived from the actual loaded model.

        Resolves the model for *symbol* and builds requirements from its
        ``input_contract``. Falls back to static metadata requirements when
        the model is unavailable or has a timeframe mismatch.
        """
        try:
            resolved = self.model_resolver.resolve(symbol)
            return StrategyDataRequirements.from_ai_model(resolved.model)
        except (
            ProbabilisticAIModelInputContractMissing,
            ProbabilisticAIModelSchemaOutdated,
            ProbabilisticAIModelUnavailable,
            ProbabilisticAIModelTimeframeMismatch,
            ProbabilisticAIRefinedGlobalTimeframeMismatch,
        ):
            return self.data_requirements()

    def _candles_for_prediction(
        self,
        market: MarketSnapshot,
        resolved: "ResolvedProbabilisticAIModel",
    ) -> tuple[list[Any], str]:
        """Return (candles of the model interval, source_label).

        The model was trained on OHLCV candles, so it receives the candles themselves: the
        bare closes in ``market.prices`` would zero five of its twelve features.
        """
        model_interval = str(resolved.model.get("interval", "")).strip() or "1m"
        series = (market.candles or {}).get(model_interval)
        if series:
            return list(series), f"candle_ohlcv_{model_interval}"
        return [], f"candle_ohlcv_{model_interval}_missing"

    def _predict(self, market: MarketSnapshot) -> dict[str, Any]:
        try:
            resolved = self.model_resolver.resolve(market.symbol)
        except (
            ProbabilisticAIModelInputContractMissing,
            ProbabilisticAIModelSchemaOutdated,
            ProbabilisticAIModelTimeframeMismatch,
            ProbabilisticAIRefinedGlobalTimeframeMismatch,
        ) as error:
            reason = _ai_model_contract_error_reason(error)
            return {
                "reason": reason,
                "model_available": False,
                "model_unavailable_reason": str(error),
                "action_candidate": "NO_ACTION",
                "direction_candidate": "NONE",
                "p_long_event": 0.0,
                "p_short_event": 0.0,
                "p_neutral_event": 1.0,
                "expected_move_bps": 0.0,
                "expected_value_bps": 0.0,
                "confidence": 0.0,
                "required_history": self.min_history,
                "available_history": 0,
            }
        except ProbabilisticAIModelUnavailable as error:
            return {
                "reason": "AI_MODEL_UNAVAILABLE",
                "model_available": False,
                "model_unavailable_reason": str(error),
                "action_candidate": "NO_ACTION",
                "direction_candidate": "NONE",
                "p_long_event": 0.0,
                "p_short_event": 0.0,
                "p_neutral_event": 1.0,
                "expected_move_bps": 0.0,
                "expected_value_bps": 0.0,
                "confidence": 0.0,
                "required_history": self.min_history,
                "available_history": len(market.prices),
            }

        candles, data_source = self._candles_for_prediction(market, resolved)
        required_history = max(
            self.min_history,
            int(resolved.model.get("required_history", 20)),
        )
        if not candles:
            prediction = no_action_prediction(
                "AI_OHLCV_UNAVAILABLE",
                required_history=required_history,
                available_history=0,
            )
        elif len(candles) < required_history:
            prediction = no_action_prediction(
                "AI_INSUFFICIENT_HISTORY",
                required_history=required_history,
                available_history=len(candles),
            )
        else:
            prediction = predict_probabilistic_event(
                resolved.model,
                candles,
                min_long_probability=self.min_long_probability,
                min_short_probability=self.min_short_probability,
                min_direction_gap=self.min_direction_gap,
                min_expected_value_bps=self.min_expected_value_bps,
            )

        return {
            **prediction,
            **_resolved_model_metadata(resolved),
            "model_available": True,
            "data_source": data_source,
            "min_long_probability": self.min_long_probability,
            "min_short_probability": self.min_short_probability,
            "min_direction_gap": self.min_direction_gap,
            "min_expected_value_bps": self.min_expected_value_bps,
            "close_long_min_short_probability": self.close_long_min_short_probability,
            "close_short_min_long_probability": self.close_short_min_long_probability,
            "close_long_max_long_probability": self.close_long_max_long_probability,
            "close_short_max_short_probability": self.close_short_max_short_probability,
        }

    def _action_for_prediction(
        self,
        prediction: dict[str, Any],
        position: FuturesPosition,
        state: dict[str, Any],
        market: MarketSnapshot,
    ) -> str:
        # Without a usable model the probabilities are placeholders (0.0); acting
        # on them would close healthy positions for no reason.
        if (
            prediction.get("reason") in UNUSABLE_PREDICTION_REASONS
            or prediction.get("model_available") is False
        ):
            return "NO_ACTION"

        candle_time = _last_close_time_ms(market, str(prediction.get("interval", "") or ""))
        if not position.is_open:
            state.pop(_ENTRY_STATE_KEY, None)
            action = str(prediction.get("action_candidate", "NO_ACTION") or "NO_ACTION")
            if action in _OPEN_ACTIONS and candle_time is not None:
                state[_PENDING_ENTRY_STATE_KEY] = {
                    "direction": _OPEN_ACTIONS[action],
                    "close_time_ms": candle_time,
                }
            return action
        return self._open_position_action(prediction, position, state, candle_time)

    def _open_position_action(
        self,
        prediction: dict[str, Any],
        position: FuturesPosition,
        state: dict[str, Any],
        candle_time: int | None,
    ) -> str:
        """Hold for the model horizon, as the label assumes; exit early only by rule.

        Before ``min_hold_candles`` the model never closes (stop and take still protect the
        position). From then on a reversal closes, and so does a fading probability when
        ``close_on_probability_fade`` is on. At the horizon the position closes unless the
        model renews the same side, which restarts the window.
        """

        long = position.direction is PositionDirection.LONG
        direction = "LONG" if long else "SHORT"
        hold = max(1, int(prediction.get("horizon_candles", 0) or 0))
        minimum = hold if self.min_hold_candles is None else min(self.min_hold_candles, hold)
        interval = str(prediction.get("interval", "") or "")
        held = _candles_held(state, direction, candle_time, interval)
        prediction["hold_candles"] = hold
        prediction["candles_held"] = held
        if held is None or held < minimum:
            return "NO_ACTION"

        p_long = float(prediction.get("p_long_event", 0.0))
        p_short = float(prediction.get("p_short_event", 0.0))
        renewed = prediction.get("action_candidate") == ("OPEN_LONG" if long else "OPEN_SHORT")
        if held >= hold:
            if renewed:
                state[_ENTRY_STATE_KEY] = {"direction": direction, "close_time_ms": candle_time}
                return "NO_ACTION"
            prediction["exit_reason"] = "AI_HORIZON_REACHED"
            return "CLOSE_POSITION"
        opposite = p_short if long else p_long
        if opposite >= (
            self.close_long_min_short_probability if long else self.close_short_min_long_probability
        ):
            prediction["exit_reason"] = "AI_REVERSAL"
            return "CLOSE_POSITION"
        same_side = p_long if long else p_short
        if self.close_on_probability_fade and same_side <= (
            self.close_long_max_long_probability if long else self.close_short_max_short_probability
        ):
            prediction["exit_reason"] = "AI_PROBABILITY_FADE"
            return "CLOSE_POSITION"
        return "NO_ACTION"

    def _reason(self, action: str, prediction: dict[str, Any]) -> str:
        model_source = str(prediction.get("model_source", ""))
        p_long = float(prediction.get("p_long_event", 0.0))
        p_short = float(prediction.get("p_short_event", 0.0))
        expected_value = float(prediction.get("expected_value_bps", 0.0))
        exit_reason = str(prediction.get("exit_reason", "") or "")
        return (
            f"AI_PROBABILITY_{action} "
            f"model_source={model_source} "
            f"p_long={p_long:.4f} p_short={p_short:.4f} "
            f"expected_value_bps={expected_value:.4f}"
            + (f" exit_reason={exit_reason}" if exit_reason else "")
        )


def _resolved_model_metadata(
    resolved: ResolvedProbabilisticAIModel,
) -> dict[str, Any]:
    model = resolved.model
    return {
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "model_id": str(model.get("model_id", "")),
        "model_path": resolved.model_path,
        "model_source": resolved.model_source,
        "fallback_used": bool(resolved.fallback_used),
        "fallback_reason": str(resolved.fallback_reason or ""),
        "event_definition": str(model.get("event_definition", PROBABILISTIC_EVENT_DEFINITION)),
        "horizon_candles": int(model.get("horizon_candles", 0) or 0),
        "interval": str(model.get("interval", "") or ""),
        "threshold_bps": float(model.get("threshold_bps", 0.0) or 0.0),
        "estimated_round_trip_cost_bps": float(
            model.get("estimated_round_trip_cost_bps", 0.0) or 0.0
        ),
        "approved_for": list(model.get("approved_for", []))
        if isinstance(model.get("approved_for", []), list)
        else [],
        "blocked_for": list(model.get("blocked_for", []))
        if isinstance(model.get("blocked_for", []), list)
        else [],
        "readiness_status": str(model.get("readiness_status", "")),
    }


def _prediction_metadata(
    prediction: dict[str, Any],
    *,
    strategy_action: str,
) -> dict[str, Any]:
    return {
        "source": "AI_PROBABILISTIC",
        "model_type": prediction.get("model_type", PROBABILISTIC_MODEL_TYPE),
        "model_id": str(prediction.get("model_id", "")),
        "model_path": str(prediction.get("model_path", "")),
        "model_source": str(prediction.get("model_source", "")),
        "fallback_used": bool(prediction.get("fallback_used", False)),
        "fallback_reason": str(prediction.get("fallback_reason", "")),
        "event_definition": str(prediction.get("event_definition", PROBABILISTIC_EVENT_DEFINITION)),
        "horizon_candles": int(prediction.get("horizon_candles", 0) or 0),
        "threshold_bps": float(prediction.get("threshold_bps", 0.0) or 0.0),
        "p_long_event": float(prediction.get("p_long_event", 0.0)),
        "p_short_event": float(prediction.get("p_short_event", 0.0)),
        "p_neutral_event": float(prediction.get("p_neutral_event", 1.0)),
        "expected_move_bps": float(prediction.get("expected_move_bps", 0.0)),
        "expected_value_bps": float(prediction.get("expected_value_bps", 0.0)),
        "expected_long_gross_bps": float(prediction.get("expected_long_gross_bps", 0.0)),
        "expected_short_gross_bps": float(prediction.get("expected_short_gross_bps", 0.0)),
        "long_expected_value_bps": float(prediction.get("long_expected_value_bps", 0.0)),
        "short_expected_value_bps": float(prediction.get("short_expected_value_bps", 0.0)),
        "estimated_round_trip_cost_bps": float(
            prediction.get("estimated_round_trip_cost_bps", 0.0)
        ),
        "long_score": float(prediction.get("long_score", 0.0)),
        "short_score": float(prediction.get("short_score", 0.0)),
        "confidence": float(prediction.get("confidence", 0.0)),
        "direction_gap": float(prediction.get("direction_gap", 0.0)),
        "action_candidate": str(prediction.get("action_candidate", "NO_ACTION")),
        "direction_candidate": str(prediction.get("direction_candidate", "NONE")),
        "strategy_action": strategy_action,
        "exit_reason": str(prediction.get("exit_reason", "") or ""),
        "hold_candles": prediction.get("hold_candles"),
        "candles_held": prediction.get("candles_held"),
        "model_available": bool(prediction.get("model_available", True)),
        "model_unavailable_reason": str(prediction.get("model_unavailable_reason", "")),
        "required_history": int(prediction.get("required_history", 0) or 0),
        "available_history": int(prediction.get("available_history", 0) or 0),
        "min_long_probability": float(prediction.get("min_long_probability", 0.0)),
        "min_short_probability": float(prediction.get("min_short_probability", 0.0)),
        "min_direction_gap": float(prediction.get("min_direction_gap", 0.0)),
        "min_expected_value_bps": float(prediction.get("min_expected_value_bps", 0.0)),
        "approved_for": list(prediction.get("approved_for", []))
        if isinstance(prediction.get("approved_for", []), list)
        else [],
        "blocked_for": list(prediction.get("blocked_for", []))
        if isinstance(prediction.get("blocked_for", []), list)
        else [],
        "readiness_status": str(prediction.get("readiness_status", "")),
    }


def _prediction_cache_key(
    market: MarketSnapshot,
    position: FuturesPosition,
) -> tuple[Any, ...]:
    # Use candle closes when available for a more stable cache key
    if market.candles:
        for series in market.candles.values():
            if series:
                return (
                    market.symbol,
                    len(series),
                    str(series[-1].close_time_ms),
                    position.direction.value,
                    str(position.quantity),
                )
    last_price = market.prices[-1] if market.prices else market.price
    return (
        market.symbol,
        len(market.prices),
        str(last_price),
        position.direction.value,
        str(position.quantity),
    )


def _last_close_time_ms(market: MarketSnapshot, interval: str) -> int | None:
    series = (market.candles or {}).get(interval) if interval else None
    if not series:
        return None
    return int(series[-1].close_time_ms)


def _candles_held(
    state: dict[str, Any],
    direction: str,
    candle_time: int | None,
    interval: str,
) -> int | None:
    """Closed candles since the entry signal of the open position.

    The entry signal's candle is remembered when the strategy proposes the entry; a position
    found open without one (recovered after a restart) counts from the first candle it is seen.
    """

    if candle_time is None or not interval or not is_supported_timeframe(interval):
        return None
    entry = state.get(_ENTRY_STATE_KEY)
    if not (isinstance(entry, dict) and entry.get("direction") == direction):
        pending = state.pop(_PENDING_ENTRY_STATE_KEY, None)
        start = candle_time
        if (
            isinstance(pending, dict)
            and pending.get("direction") == direction
            and int(pending.get("close_time_ms", candle_time)) <= candle_time
        ):
            start = int(pending["close_time_ms"])
        entry = {"direction": direction, "close_time_ms": start}
        state[_ENTRY_STATE_KEY] = entry
    return max(0, (candle_time - int(entry["close_time_ms"])) // timeframe_duration_ms(interval))


def _remember_prediction(
    state: dict[str, Any],
    key: tuple[Any, ...],
    prediction: dict[str, Any],
    action: str,
) -> None:
    state["last_ai_probability_evaluation"] = {
        "key": key,
        "prediction": dict(prediction),
        "strategy_action": action,
    }


def _score_value(prediction: dict[str, Any], action: str) -> int:
    if action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"}:
        return 95
    probability = max(
        float(prediction.get("p_long_event", 0.0)),
        float(prediction.get("p_short_event", 0.0)),
    )
    return clamp_score(10 + probability * 75.0)


def _ai_model_contract_error_reason(error: Exception) -> str:
    message = str(error)
    for reason in (
        "AI_REFINED_GLOBAL_TIMEFRAME_MISMATCH",
        "AI_MODEL_SCHEMA_OUTDATED",
        "AI_MODEL_INPUT_CONTRACT_MISSING",
        "AI_MODEL_TIMEFRAME_MISMATCH",
    ):
        if message.startswith(reason):
            return reason
    return "AI_MODEL_TIMEFRAME_MISMATCH"


def _probability(value: Any) -> float:
    converted = float(value)
    if converted < 0.0 or converted > 1.0:
        raise ValueError("probabilidade precisa ficar entre 0 e 1")
    return converted


def _optional_float(value: Any, *, default: float) -> float:
    if value is None or str(value).strip() == "":
        return float(default)
    return float(value)


def _optional_float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "sim"}:
        return True
    if normalized in {"false", "0", "no", "n", "nao", "não"}:
        return False
    raise ValueError("valor booleano invalido")


def _optional_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(symbol).upper().strip(): str(path).strip()
        for symbol, path in value.items()
        if str(symbol).strip() and str(path).strip()
    }


def _optional_str(value: Any) -> str:
    return str(value or "").strip()
