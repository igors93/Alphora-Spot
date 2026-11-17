from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.ai.model import AI_FEATURE_SCHEMA_VERSION
from domain.ai.probabilistic import (
    PROBABILISTIC_MODEL_TYPE,
    PROBABILISTIC_SCOPE_SYMBOL_REFINED,
    load_probabilistic_ai_model,
)
from domain.timeframe import is_supported_timeframe, normalize_timeframe


class ProbabilisticAIModelUnavailable(ValueError):
    pass


class ProbabilisticAIModelInputContractMissing(ValueError):
    """Raised when an operational AI model lacks the explicit input contract."""

    def __init__(self, *, model_source: str, model_id: str):
        self.model_source = model_source
        self.model_id = model_id
        super().__init__(
            "AI_MODEL_INPUT_CONTRACT_MISSING: "
            f"modelo '{model_id or '<sem-id>'}' ({model_source}) nao declara input_contract"
        )


class ProbabilisticAIModelSchemaOutdated(ValueError):
    """Raised when an artifact predates the current feature schema (IA0).

    Earlier artifacts were trained with a non-converged fit and evaluated on closes only; their
    metrics do not describe what the model would do now, so they are refused instead of run.
    """

    def __init__(self, *, model_source: str, model_id: str, schema_version: str):
        self.model_source = model_source
        self.model_id = model_id
        self.schema_version = schema_version
        super().__init__(
            "AI_MODEL_SCHEMA_OUTDATED: "
            f"modelo '{model_id or '<sem-id>'}' ({model_source}) usa o schema de features "
            f"'{schema_version or '<ausente>'}'; o atual e '{AI_FEATURE_SCHEMA_VERSION}'. "
            "Treine o modelo novamente."
        )


class ProbabilisticAIModelTimeframeMismatch(ValueError):
    """Raised when the loaded model's interval doesn't match the expected runtime interval."""

    def __init__(self, model_interval: str, runtime_interval: str):
        self.model_interval = model_interval
        self.runtime_interval = runtime_interval
        super().__init__(
            f"AI_MODEL_TIMEFRAME_MISMATCH: "
            f"modelo treinado em '{model_interval}', runtime configurado para '{runtime_interval}'"
        )


class ProbabilisticAIRefinedGlobalTimeframeMismatch(ValueError):
    """Raised when a refined model does not match its global model's timeframe."""

    def __init__(
        self,
        *,
        refined_interval: str,
        global_interval: str,
        symbol: str,
    ):
        self.refined_interval = refined_interval
        self.global_interval = global_interval
        self.symbol = symbol
        super().__init__(
            "AI_REFINED_GLOBAL_TIMEFRAME_MISMATCH: "
            f"modelo refinado {symbol} treinado em '{refined_interval}', "
            f"modelo global em '{global_interval}'"
        )


@dataclass(frozen=True)
class ResolvedProbabilisticAIModel:
    model: dict[str, Any]
    model_source: str
    model_path: str
    fallback_used: bool = False
    fallback_reason: str = ""

    @property
    def model_id(self) -> str:
        return str(self.model.get("model_id", ""))

    @property
    def interval(self) -> str:
        return str(self.model.get("interval", ""))


class ProbabilisticAIModelResolver:
    """Resolve refined symbol models with explicit global fallback."""

    def __init__(
        self,
        *,
        global_model_path: str | None = None,
        symbol_model_paths: dict[str, str] | None = None,
        use_symbol_refined_models: bool = True,
        fallback_to_global_model: bool = True,
        expected_interval: str | None = None,
    ):
        self.global_model_path = _clean_path(global_model_path)
        self.symbol_model_paths = {
            str(symbol).upper().strip(): cleaned_path
            for symbol, path in dict(symbol_model_paths or {}).items()
            if str(symbol).strip()
            for cleaned_path in [_clean_path(path)]
            if cleaned_path
        }
        self.use_symbol_refined_models = bool(use_symbol_refined_models)
        self.fallback_to_global_model = bool(fallback_to_global_model)
        self._models_by_path: dict[str, dict[str, Any]] = {}
        raw_interval = str(expected_interval or "").strip()
        self.expected_interval: str | None = (
            normalize_timeframe(raw_interval)
            if raw_interval and is_supported_timeframe(raw_interval)
            else None
        )

    def resolve(self, symbol: str) -> ResolvedProbabilisticAIModel:
        normalized_symbol = str(symbol or "").upper().strip()
        if not normalized_symbol:
            raise ProbabilisticAIModelUnavailable("AI_MODEL_UNAVAILABLE: SYMBOL_EMPTY")

        if self.use_symbol_refined_models:
            refined_path = self.symbol_model_paths.get(normalized_symbol, "")
            if refined_path:
                refined = self._try_load_refined(
                    symbol=normalized_symbol,
                    path=refined_path,
                )
                if refined is not None:
                    return refined
                return self._fallback(
                    normalized_symbol,
                    "SYMBOL_REFINED_MODEL_INVALID_OR_MISSING",
                )
            return self._fallback(
                normalized_symbol,
                "SYMBOL_REFINED_MODEL_NOT_CONFIGURED",
            )

        return self._global(fallback_used=False, fallback_reason="")

    def _try_load_refined(
        self,
        *,
        symbol: str,
        path: str,
    ) -> ResolvedProbabilisticAIModel | None:
        if not Path(path).exists():
            return None
        try:
            model = self._load_model(path)
        except Exception:
            return None

        if model.get("model_type") != PROBABILISTIC_MODEL_TYPE:
            return None
        if str(model.get("scope", "")).upper().strip() != PROBABILISTIC_SCOPE_SYMBOL_REFINED:
            return None
        model_symbol = str(model.get("symbol", "")).upper().strip()
        if model_symbol and model_symbol != symbol:
            return None

        resolved = ResolvedProbabilisticAIModel(
            model=model,
            model_source="SYMBOL_REFINED",
            model_path=path,
            fallback_used=False,
            fallback_reason="",
        )
        self._validate_input_contract(resolved)
        self._validate_interval(resolved)
        self._validate_refined_against_global(symbol=symbol, resolved=resolved)
        return resolved

    def _fallback(
        self,
        symbol: str,
        reason: str,
    ) -> ResolvedProbabilisticAIModel:
        if not self.fallback_to_global_model:
            raise ProbabilisticAIModelUnavailable(f"AI_MODEL_UNAVAILABLE: {reason}")
        return self._global(fallback_used=True, fallback_reason=reason)

    def _global(
        self,
        *,
        fallback_used: bool,
        fallback_reason: str,
    ) -> ResolvedProbabilisticAIModel:
        if not self.global_model_path:
            raise ProbabilisticAIModelUnavailable("AI_MODEL_UNAVAILABLE: GLOBAL_MODEL_PATH_EMPTY")
        if not Path(self.global_model_path).exists():
            raise ProbabilisticAIModelUnavailable("AI_MODEL_UNAVAILABLE: GLOBAL_MODEL_NOT_FOUND")
        model = self._load_model(self.global_model_path)
        if model.get("model_type") != PROBABILISTIC_MODEL_TYPE:
            raise ProbabilisticAIModelUnavailable("AI_MODEL_UNAVAILABLE: GLOBAL_MODEL_TYPE_INVALID")
        resolved = ResolvedProbabilisticAIModel(
            model=model,
            model_source="GLOBAL",
            model_path=self.global_model_path,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        self._validate_input_contract(resolved)
        self._validate_interval(resolved)
        return resolved

    def _validate_input_contract(self, resolved: ResolvedProbabilisticAIModel) -> None:
        contract = resolved.model.get("input_contract")
        if not isinstance(contract, dict):
            raise ProbabilisticAIModelInputContractMissing(
                model_source=resolved.model_source,
                model_id=resolved.model_id,
            )
        schema_version = str(contract.get("feature_schema_version", "") or "")
        if schema_version != AI_FEATURE_SCHEMA_VERSION:
            raise ProbabilisticAIModelSchemaOutdated(
                model_source=resolved.model_source,
                model_id=resolved.model_id,
                schema_version=schema_version,
            )

    def _validate_interval(self, resolved: ResolvedProbabilisticAIModel) -> None:
        if self.expected_interval is None:
            return
        model_interval = _model_timeframe(resolved.model)
        if not model_interval:
            return
        if model_interval != self.expected_interval:
            raise ProbabilisticAIModelTimeframeMismatch(model_interval, self.expected_interval)

    def _validate_refined_against_global(
        self,
        *,
        symbol: str,
        resolved: ResolvedProbabilisticAIModel,
    ) -> None:
        if not self.global_model_path or not Path(self.global_model_path).exists():
            return

        global_model = self._load_model(self.global_model_path)
        global_resolved = ResolvedProbabilisticAIModel(
            model=global_model,
            model_source="GLOBAL",
            model_path=self.global_model_path,
        )
        self._validate_input_contract(global_resolved)
        refined_interval = _model_timeframe(resolved.model)
        global_interval = _model_timeframe(global_model)
        if refined_interval and global_interval and refined_interval != global_interval:
            raise ProbabilisticAIRefinedGlobalTimeframeMismatch(
                refined_interval=refined_interval,
                global_interval=global_interval,
                symbol=symbol,
            )

    def _load_model(self, path: str) -> dict[str, Any]:
        normalized_path = str(Path(path))
        if normalized_path not in self._models_by_path:
            self._models_by_path[normalized_path] = load_probabilistic_ai_model(normalized_path)
        return self._models_by_path[normalized_path]


def _clean_path(value: Any) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    if cleaned.lower() in {"", "none", "null"}:
        return ""
    return cleaned


def _model_timeframe(model: dict[str, Any]) -> str:
    contract = model.get("input_contract")
    if isinstance(contract, dict):
        timeframe = str(contract.get("timeframe", "")).strip()
        if timeframe:
            return (
                normalize_timeframe(timeframe) if is_supported_timeframe(timeframe) else timeframe
            )

    interval = str(model.get("interval", "")).strip()
    if not interval:
        return ""
    return normalize_timeframe(interval) if is_supported_timeframe(interval) else interval
