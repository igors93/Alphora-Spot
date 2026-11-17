import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.ai.model import load_ai_model, save_ai_model
from research.ai.paths import (
    AI_REGISTRY_PATH,
    default_probabilistic_global_model_path,
    default_probabilistic_refined_model_path,
    safe_ai_name,
)
from research.ai.probabilistic import (
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_MODEL_TYPE,
    train_probabilistic_global_model_from_csvs,
    train_probabilistic_refined_model_from_csv,
)
from research.ai.registry import (
    AI_SCOPE_GLOBAL,
    AI_SCOPE_SYMBOL_REFINED,
    list_ai_models,
    register_ai_model,
)
from research.ai.training_defaults import (
    DEFAULT_HORIZON_CANDLES,
    DEFAULT_MIN_TRAINING_ROWS,
    DEFAULT_THRESHOLD_BPS,
)
from research.binance_klines_downloader import download_klines_to_csv
from research.data_loader import load_candles_from_csv
from utils.journals import JournalRegistry

PROBABILISTIC_AI_ALGORITHM_VERSION = "probabilistic_logistic_ai_v3.ohlcv_strict.irls.purged_oos.v5"


class ProbabilisticAITrainingService:
    """Train JSON-only probabilistic AI models for Governor input."""

    def __init__(
        self,
        *,
        registry_path: str | Path = AI_REGISTRY_PATH,
        event_journal_path: str | Path | None = "data/governor_events.jsonl",
        progress_callback: Callable[[str], None] | None = None,
    ):
        self.registry_path = Path(registry_path)
        self.event_journal = (
            JournalRegistry().journal(event_journal_path) if event_journal_path else None
        )
        self.progress_callback = progress_callback

    def train_global(
        self,
        *,
        symbols: list[str],
        interval: str,
        start: str | None = None,
        end: str | None = None,
        csv_by_symbol: dict[str, str | Path] | None = None,
        horizon_candles: int = DEFAULT_HORIZON_CANDLES,
        threshold_bps: float = DEFAULT_THRESHOLD_BPS,
        min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
        output_path: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        normalized_symbols = _normalize_symbols(symbols)
        version = _training_version()
        self._progress(
            "Treinamento IA iniciado | "
            f"scope=GLOBAL | symbols={','.join(normalized_symbols)} | "
            f"interval={interval}"
        )
        self._record(
            "ai_probability_training_started",
            scope="GLOBAL",
            symbols=normalized_symbols,
            interval=interval,
        )
        csvs = self._resolve_csvs(
            symbols=normalized_symbols,
            interval=interval,
            start=start,
            end=end,
            csv_by_symbol=csv_by_symbol,
            step_label="Etapa 1/5",
        )
        dataset_metadata = self._dataset_metadata(
            symbols=normalized_symbols,
            csv_by_symbol=csvs,
            interval=interval,
            requested_start=start,
            requested_end=end,
            horizon_candles=horizon_candles,
            threshold_bps=threshold_bps,
            min_training_rows=min_training_rows,
            training_type="GLOBAL",
        )
        warnings = self._duplicate_training_warnings(
            scope=AI_SCOPE_GLOBAL,
            symbol="GLOBAL",
            dataset_hash=str(dataset_metadata["dataset_hash"]),
        )
        for warning in warnings:
            self._progress(f"Aviso: {warning}")

        self._progress(f"Etapa 2/5: treinando modelo global com {len(normalized_symbols)} moedas")
        if output_path:
            resolved_output_path = Path(output_path)
        elif output_dir:
            safe_interval = safe_ai_name(interval)
            safe_version = safe_ai_name(version)
            resolved_output_path = (
                Path(output_dir) / f"GLOBAL_{safe_interval}_probabilistic_ai_{safe_version}.json"
            )
        else:
            resolved_output_path = default_probabilistic_global_model_path(
                interval=interval,
                version=version,
            )
        model, saved_path = train_probabilistic_global_model_from_csvs(
            csvs,
            interval=interval,
            horizon_candles=horizon_candles,
            threshold_bps=threshold_bps,
            min_training_rows=min_training_rows,
            output_path=resolved_output_path,
        )
        if output_path is None:
            model["model_id"] = f"{model.get('model_id', 'GLOBAL_prob_ai')}_{version}"
        _apply_dataset_metadata(
            model,
            dataset_metadata,
            warnings=warnings,
        )

        self._progress(f"Etapa 3/5: salvando modelo global em {saved_path}")
        saved_path = save_ai_model(model, saved_path)
        self._progress("Etapa 5/5: atualizando registry")
        registry_entry = register_ai_model(
            saved_path,
            scope=AI_SCOPE_GLOBAL,
            model_id=str(model.get("model_id", "")),
            symbol="GLOBAL",
            interval=str(model["interval"]),
            trained_symbols=list(model.get("trained_symbols", [])),
            approved_for=list(model.get("approved_for", [])),
            blocked_for=list(model.get("blocked_for", [])),
            readiness_status=str(model.get("readiness_status", "TRAINED")),
            created_by=str(model.get("created_by", "")),
            event_definition=PROBABILISTIC_EVENT_DEFINITION,
            registry_path=self.registry_path,
        )
        payload = {
            "model_id": registry_entry["model_id"],
            "path": str(saved_path),
            "training_rows": int(model.get("training_rows", 0)),
            "metrics": dict(model.get("metrics", {})),
            "dataset_hash": str(model.get("dataset_hash", "")),
            "warnings": warnings,
        }
        self._record(
            "ai_probability_training_completed",
            scope="GLOBAL",
            model_id=payload["model_id"],
            model_path=payload["path"],
            training_rows=payload["training_rows"],
            metrics=payload["metrics"],
            dataset_hash=payload["dataset_hash"],
            warnings=warnings,
        )
        self._progress("Treinamento concluído com sucesso | modelo global registrado")
        return {"status": "OK", "global_model": payload, "warnings": warnings}

    def train_refined(
        self,
        *,
        symbols: list[str],
        interval: str,
        global_model_path: str | Path,
        start: str | None = None,
        end: str | None = None,
        csv_by_symbol: dict[str, str | Path] | None = None,
        horizon_candles: int = DEFAULT_HORIZON_CANDLES,
        threshold_bps: float = DEFAULT_THRESHOLD_BPS,
        min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        normalized_symbols = _normalize_symbols(symbols)
        global_model = _load_global_probabilistic_model(global_model_path)
        self._progress(
            "Treinamento IA iniciado | "
            f"scope=SYMBOL_REFINED | symbols={','.join(normalized_symbols)} | "
            f"interval={interval}"
        )
        self._record(
            "ai_probability_training_started",
            scope="SYMBOL_REFINED",
            symbols=normalized_symbols,
            interval=interval,
            parent_global_model_path=str(global_model_path),
        )
        csvs = self._resolve_csvs(
            symbols=normalized_symbols,
            interval=interval,
            start=start,
            end=end,
            csv_by_symbol=csv_by_symbol,
            step_label="Etapa 1/4",
        )
        symbol_results: dict[str, dict[str, Any]] = {}
        all_warnings: list[str] = []
        total = len(normalized_symbols)
        for index, symbol in enumerate(normalized_symbols, start=1):
            try:
                version = _training_version()
                self._progress(
                    f"Etapa 2/4: refinando {symbol} ({index}/{total}) a partir do modelo global"
                )
                output_path = _refined_output_path(
                    symbol=symbol,
                    interval=interval,
                    output_dir=output_dir,
                    version=version,
                )
                dataset_metadata = self._dataset_metadata(
                    symbols=[symbol],
                    csv_by_symbol={symbol: csvs[symbol]},
                    interval=interval,
                    requested_start=start,
                    requested_end=end,
                    horizon_candles=horizon_candles,
                    threshold_bps=threshold_bps,
                    min_training_rows=min_training_rows,
                    training_type="SYMBOL_REFINED",
                    parent_global_model=global_model,
                )
                warnings = self._duplicate_training_warnings(
                    scope=AI_SCOPE_SYMBOL_REFINED,
                    symbol=symbol,
                    dataset_hash=str(dataset_metadata["dataset_hash"]),
                )
                warnings.extend(
                    str(flag) for flag in dataset_metadata.get("overfit_risk_flags", [])
                )
                for warning in warnings:
                    self._progress(f"Aviso {symbol}: {warning}")
                all_warnings.extend(warnings)

                model, saved_path = train_probabilistic_refined_model_from_csv(
                    csvs[symbol],
                    symbol=symbol,
                    interval=interval,
                    global_model_path=global_model_path,
                    horizon_candles=horizon_candles,
                    threshold_bps=threshold_bps,
                    min_training_rows=min_training_rows,
                    output_path=output_path,
                )
                model["model_id"] = (
                    f"{model.get('model_id', f'{symbol}_prob_refined_ai')}_{version}"
                )
                _apply_dataset_metadata(
                    model,
                    dataset_metadata,
                    warnings=warnings,
                    parent_global_model=global_model,
                )
                self._progress(f"Etapa 3/4: salvando modelo refinado {symbol}")
                saved_path = save_ai_model(model, saved_path)
                self._progress(f"Etapa 4/4: registrando modelo refinado {symbol}")
                registry_entry = register_ai_model(
                    saved_path,
                    scope=AI_SCOPE_SYMBOL_REFINED,
                    model_id=str(model.get("model_id", "")),
                    symbol=str(model["symbol"]),
                    interval=str(model["interval"]),
                    trained_symbols=[str(model["symbol"])],
                    parent_global_model_id=str(model.get("parent_global_model_id", "")),
                    parent_global_model_path=str(model.get("parent_global_model_path", "")),
                    approved_for=list(model.get("approved_for", [])),
                    blocked_for=list(model.get("blocked_for", [])),
                    readiness_status=str(model.get("readiness_status", "TRAINED")),
                    created_by=str(model.get("created_by", "")),
                    event_definition=PROBABILISTIC_EVENT_DEFINITION,
                    registry_path=self.registry_path,
                )
                symbol_results[symbol] = {
                    "status": "OK",
                    "model_id": registry_entry["model_id"],
                    "path": str(saved_path),
                    "source": "SYMBOL_REFINED",
                    "training_rows": int(model.get("training_rows", 0)),
                    "metrics": dict(model.get("metrics", {})),
                    "dataset_hash": str(model.get("dataset_hash", "")),
                    "warnings": warnings,
                }
            except Exception as error:
                reason = str(error)
                symbol_results[symbol] = {
                    "status": "FAILED",
                    "source": "GLOBAL",
                    "fallback_used": True,
                    "fallback_reason": reason,
                    "training_rows": 0,
                    "metrics": {},
                }
                self._progress(
                    f"Falha no refino {symbol}: {reason}. A moeda usará fallback global."
                )
                self._record(
                    "ai_probability_symbol_refinement_failed",
                    symbol=symbol,
                    interval=interval,
                    parent_global_model_path=str(global_model_path),
                    reason=reason,
                )

        self._record(
            "ai_probability_training_completed",
            scope="SYMBOL_REFINED",
            symbols=normalized_symbols,
            interval=interval,
            parent_global_model_path=str(global_model_path),
            symbol_models=symbol_results,
            warnings=all_warnings,
        )
        self._progress("Treinamento concluído com sucesso | refino por moeda finalizado")
        return {
            "status": "OK",
            "symbol_models": symbol_results,
            "warnings": all_warnings,
        }

    def train_all(
        self,
        *,
        symbols: list[str],
        interval: str,
        start: str | None = None,
        end: str | None = None,
        csv_by_symbol: dict[str, str | Path] | None = None,
        horizon_candles: int = DEFAULT_HORIZON_CANDLES,
        threshold_bps: float = DEFAULT_THRESHOLD_BPS,
        min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
        global_output_path: str | Path | None = None,
        global_output_dir: str | Path | None = None,
        refined_output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        normalized_symbols = _normalize_symbols(symbols)
        self._progress("Etapa 1/5: preparando treino completo da IA")
        resolved_csvs = self._resolve_csvs(
            symbols=normalized_symbols,
            interval=interval,
            start=start,
            end=end,
            csv_by_symbol=csv_by_symbol,
            step_label="Etapa 1/5",
        )
        global_result = self.train_global(
            symbols=normalized_symbols,
            interval=interval,
            start=start,
            end=end,
            csv_by_symbol=resolved_csvs,
            horizon_candles=horizon_candles,
            threshold_bps=threshold_bps,
            min_training_rows=min_training_rows,
            output_path=global_output_path,
            output_dir=global_output_dir,
        )
        self._progress("Etapa 4/5: iniciando refino das moedas habilitadas")
        refined_result = self.train_refined(
            symbols=normalized_symbols,
            interval=interval,
            global_model_path=global_result["global_model"]["path"],
            start=start,
            end=end,
            csv_by_symbol=resolved_csvs,
            horizon_candles=horizon_candles,
            threshold_bps=threshold_bps,
            min_training_rows=min_training_rows,
            output_dir=refined_output_dir,
        )
        warnings = list(global_result.get("warnings", []))
        warnings.extend(refined_result.get("warnings", []))
        self._progress("Etapa 5/5: treinamento completo registrado")
        return {
            "status": "OK",
            "global_model": global_result["global_model"],
            "symbol_models": refined_result["symbol_models"],
            "warnings": warnings,
        }

    def _resolve_csvs(
        self,
        *,
        symbols: list[str],
        interval: str,
        start: str | None,
        end: str | None,
        csv_by_symbol: dict[str, str | Path] | None,
        step_label: str,
    ) -> dict[str, str | Path]:
        normalized_csvs = {
            str(symbol).upper().strip(): path
            for symbol, path in dict(csv_by_symbol or {}).items()
            if str(symbol).strip() and str(path).strip()
        }
        if normalized_csvs:
            missing = [symbol for symbol in symbols if symbol not in normalized_csvs]
            if missing:
                raise ValueError(f"csv_by_symbol sem CSV para simbolos: {missing}")
            for index, symbol in enumerate(symbols, start=1):
                self._progress(
                    f"{step_label}: carregando CSV {symbol} "
                    f"({index}/{len(symbols)}) | {normalized_csvs[symbol]}"
                )
            return {symbol: normalized_csvs[symbol] for symbol in symbols}

        if not start or not end:
            raise ValueError("start e end sao obrigatorios quando csv_by_symbol nao e usado")

        result: dict[str, str | Path] = {}
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            self._progress(
                f"{step_label}: baixando candles {symbol} {interval} "
                f"({index}/{total}) | start={start} | end={end}"
            )
            result[symbol] = download_klines_to_csv(
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
            )
            self._progress(
                f"{step_label}: candles salvos {symbol} ({index}/{total}) | csv={result[symbol]}"
            )
        return result

    def _dataset_metadata(
        self,
        *,
        symbols: list[str],
        csv_by_symbol: dict[str, str | Path],
        interval: str,
        requested_start: str | None,
        requested_end: str | None,
        horizon_candles: int,
        threshold_bps: float,
        min_training_rows: int,
        training_type: str,
        parent_global_model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol_hashes: dict[str, str] = {}
        source_csvs: dict[str, str] = {}
        candle_count_by_symbol: dict[str, int] = {}
        per_symbol_windows: dict[str, dict[str, Any]] = {}
        hash_builder = hashlib.sha256()
        hash_builder.update(
            json.dumps(
                {
                    "symbols": symbols,
                    "interval": interval,
                    "requested_start": requested_start,
                    "requested_end": requested_end,
                    "horizon_candles": int(horizon_candles),
                    "threshold_bps": float(threshold_bps),
                    "min_training_rows": int(min_training_rows),
                    "training_type": training_type,
                    "algorithm_version": PROBABILISTIC_AI_ALGORITHM_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )

        all_starts: list[str] = []
        all_ends: list[str] = []
        for symbol in symbols:
            path = Path(csv_by_symbol[symbol])
            content = path.read_bytes()
            symbol_hash = hashlib.sha256(content).hexdigest()
            symbol_hashes[symbol] = symbol_hash
            source_csvs[symbol] = str(path)
            hash_builder.update(symbol.encode("utf-8"))
            hash_builder.update(symbol_hash.encode("utf-8"))

            candles = load_candles_from_csv(str(path), interval=interval, symbol=symbol)
            candle_count_by_symbol[symbol] = len(candles)
            window = _split_windows(candles, purge_candles=int(horizon_candles))
            per_symbol_windows[symbol] = window
            if candles:
                all_starts.append(str(candles[0].timestamp))
                all_ends.append(str(candles[-1].timestamp))

        data_start = min(all_starts) if all_starts else str(requested_start or "")
        data_end = max(all_ends) if all_ends else str(requested_end or "")
        overlap = _parent_overlap(
            symbols=symbols,
            data_start=data_start,
            data_end=data_end,
            symbol_hashes=symbol_hashes,
            parent_global_model=parent_global_model,
        )
        risk_flags = list(overlap.get("risk_flags", []))

        return {
            "dataset_hash": hash_builder.hexdigest(),
            "dataset_hash_by_symbol": symbol_hashes,
            "symbols_used": symbols,
            "trained_symbols": symbols,
            "interval": interval,
            "data_start": data_start,
            "data_end": data_end,
            "requested_start": str(requested_start or ""),
            "requested_end": str(requested_end or ""),
            "horizon_candles": int(horizon_candles),
            "threshold_bps": float(threshold_bps),
            "min_training_rows": int(min_training_rows),
            "candle_count": sum(candle_count_by_symbol.values()),
            "candle_count_by_symbol": candle_count_by_symbol,
            "source_csvs": source_csvs,
            "algorithm_version": PROBABILISTIC_AI_ALGORITHM_VERSION,
            "training_type": training_type,
            "training_window": _aggregate_window(per_symbol_windows, "training_window"),
            "validation_window": _aggregate_window(per_symbol_windows, "validation_window"),
            "test_window": _aggregate_window(per_symbol_windows, "test_window"),
            "per_symbol_windows": per_symbol_windows,
            "dataset_overlap_with_parent": overlap,
            "overfit_risk_flags": risk_flags,
            "parent_global_dataset_hash": str((parent_global_model or {}).get("dataset_hash", "")),
        }

    def _duplicate_training_warnings(
        self,
        *,
        scope: str,
        symbol: str,
        dataset_hash: str,
    ) -> list[str]:
        if not dataset_hash:
            return []
        warnings = []
        for entry in list_ai_models(scope=scope, symbol=symbol, registry_path=self.registry_path):
            if str(entry.get("dataset_hash", "")) == dataset_hash:
                warnings.append(
                    "Treino duplicado detectado para o mesmo dataset e parâmetros. "
                    f"Modelo anterior: {entry.get('model_id', '')}"
                )
        return warnings

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _record(self, event_type: str, **payload: Any) -> None:
        if self.event_journal is not None:
            self.event_journal.append(event_type, **payload)


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for symbol in symbols:
        value = str(symbol).upper().strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    if not normalized:
        raise ValueError("Informe ao menos um simbolo")
    return normalized


def _refined_output_path(
    *,
    symbol: str,
    interval: str,
    output_dir: str | Path | None,
    version: str,
) -> Path:
    if output_dir is None:
        return default_probabilistic_refined_model_path(
            symbol=symbol,
            interval=interval,
            version=version,
        )
    output_base = Path(output_dir)
    safe_symbol = safe_ai_name(symbol)
    safe_interval = safe_ai_name(interval)
    safe_version = safe_ai_name(version)
    return (
        output_base
        / safe_symbol
        / f"{safe_symbol}_{safe_interval}_probabilistic_refined_ai_{safe_version}.json"
    )


def _training_version() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _apply_dataset_metadata(
    model: dict[str, Any],
    metadata: dict[str, Any],
    *,
    warnings: list[str],
    parent_global_model: dict[str, Any] | None = None,
) -> None:
    model.update(
        {
            "dataset_hash": metadata["dataset_hash"],
            "dataset_hash_by_symbol": metadata["dataset_hash_by_symbol"],
            "symbols_used": metadata["symbols_used"],
            "data_start": metadata["data_start"],
            "data_end": metadata["data_end"],
            "requested_start": metadata["requested_start"],
            "requested_end": metadata["requested_end"],
            "min_training_rows": metadata["min_training_rows"],
            "candle_count": metadata["candle_count"],
            "candle_count_by_symbol": metadata["candle_count_by_symbol"],
            "effective_training_rows": int(model.get("training_rows", 0)),
            "algorithm_version": metadata["algorithm_version"],
            "training_type": metadata["training_type"],
            "source_csvs": metadata["source_csvs"],
            "training_window": metadata["training_window"],
            "validation_window": metadata["validation_window"],
            "test_window": metadata["test_window"],
            "per_symbol_windows": metadata["per_symbol_windows"],
            "dataset_overlap_with_parent": metadata["dataset_overlap_with_parent"],
            "overfit_risk_flags": list(metadata.get("overfit_risk_flags", [])),
            "training_warnings": warnings,
        }
    )
    if parent_global_model:
        model["parent_global_dataset_hash"] = str(parent_global_model.get("dataset_hash", ""))
        if not model.get("parent_global_model_id"):
            model["parent_global_model_id"] = str(parent_global_model.get("model_id", ""))


def _split_windows(
    candles: list[Any],
    *,
    purge_candles: int = 0,
) -> dict[str, dict[str, str]]:
    total = len(candles)
    if total <= 0:
        empty = {"start": "", "end": "", "rows": "0"}
        return {
            "training_window": dict(empty),
            "validation_window": dict(empty),
            "test_window": dict(empty),
        }
    train_boundary = max(1, int(total * 0.70))
    validation_boundary = max(train_boundary + 1, int(total * 0.85))
    validation_boundary = min(validation_boundary, total)
    purge = max(0, int(purge_candles))
    train_end = max(1, train_boundary - purge)
    validation_end = max(train_boundary, validation_boundary - purge)
    train = candles[:train_end]
    validation = (
        candles[train_boundary:validation_end] or candles[train_boundary - 1 : train_boundary]
    )
    test = candles[validation_boundary:] or candles[-1:]
    return {
        "training_window": _window_payload(train),
        "validation_window": _window_payload(validation),
        "test_window": _window_payload(test),
    }


def _window_payload(candles: list[Any]) -> dict[str, str]:
    if not candles:
        return {"start": "", "end": "", "rows": "0"}
    return {
        "start": str(candles[0].timestamp),
        "end": str(candles[-1].timestamp),
        "rows": str(len(candles)),
    }


def _aggregate_window(
    per_symbol_windows: dict[str, dict[str, Any]],
    window_name: str,
) -> dict[str, str]:
    starts = []
    ends = []
    rows = 0
    for payload in per_symbol_windows.values():
        window = payload.get(window_name, {})
        start = str(window.get("start", ""))
        end = str(window.get("end", ""))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
        try:
            rows += int(window.get("rows", "0"))
        except (TypeError, ValueError):
            pass
    return {
        "start": min(starts) if starts else "",
        "end": max(ends) if ends else "",
        "rows": str(rows),
    }


def _parent_overlap(
    *,
    symbols: list[str],
    data_start: str,
    data_end: str,
    symbol_hashes: dict[str, str],
    parent_global_model: dict[str, Any] | None,
) -> dict[str, Any]:
    if not parent_global_model:
        return {"status": "NONE", "risk_flags": []}

    parent_hashes = parent_global_model.get("dataset_hash_by_symbol", {})
    if not isinstance(parent_hashes, dict):
        parent_hashes = {}
    parent_start = str(parent_global_model.get("data_start", ""))
    parent_end = str(parent_global_model.get("data_end", ""))
    statuses: dict[str, str] = {}
    risk_flags: list[str] = []
    for symbol in symbols:
        parent_symbol_hash = str(parent_hashes.get(symbol, ""))
        current_hash = str(symbol_hashes.get(symbol, ""))
        if parent_symbol_hash and parent_symbol_hash == current_hash:
            statuses[symbol] = "SAME_DATASET"
            risk_flags.append("REFINED_DATASET_EQUALS_PARENT_SYMBOL_DATA")
            continue
        if parent_start and parent_end and parent_start <= data_start and data_end <= parent_end:
            statuses[symbol] = "FULL_PERIOD_OVERLAP"
            risk_flags.append("REFINED_PERIOD_CONTAINED_IN_GLOBAL_PERIOD")
            continue
        if parent_start and parent_end and not (data_end < parent_start or data_start > parent_end):
            statuses[symbol] = "PARTIAL_PERIOD_OVERLAP"
            risk_flags.append("REFINED_PERIOD_OVERLAPS_GLOBAL_PERIOD")
            continue
        statuses[symbol] = "NONE"

    unique_flags = sorted(set(risk_flags))
    if any(status == "SAME_DATASET" for status in statuses.values()):
        status = "SAME_DATASET"
    elif any(status == "FULL_PERIOD_OVERLAP" for status in statuses.values()):
        status = "FULL_PERIOD_OVERLAP"
    elif any(status == "PARTIAL_PERIOD_OVERLAP" for status in statuses.values()):
        status = "PARTIAL_PERIOD_OVERLAP"
    else:
        status = "NONE"
    return {
        "status": status,
        "by_symbol": statuses,
        "parent_global_model_id": str(parent_global_model.get("model_id", "")),
        "parent_global_dataset_hash": str(parent_global_model.get("dataset_hash", "")),
        "risk_flags": unique_flags,
    }


def _load_global_probabilistic_model(path: str | Path) -> dict[str, Any]:
    model_path = Path(path)
    if not model_path.exists():
        raise ValueError(f"Modelo global IA nao encontrado: {model_path}")
    model = load_ai_model(model_path)
    if model.get("model_type") != PROBABILISTIC_MODEL_TYPE:
        raise ValueError(
            "Modelo global IA incompativel: "
            f"{model.get('model_type')} != {PROBABILISTIC_MODEL_TYPE}"
        )
    if str(model.get("scope", "")).upper().strip() != "GLOBAL":
        raise ValueError("Modelo base para refino precisa ter scope GLOBAL")
    return model
