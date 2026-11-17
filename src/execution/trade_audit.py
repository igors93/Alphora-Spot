from __future__ import annotations

import copy
import json
from collections import deque
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from utils.events import EventChannel, observe_event
from utils.journals import JournalRegistry
from utils.logger import get_logger

TRADE_AUDIT_OPEN_CANDIDATE = "trade_audit_open_candidate"
TRADE_AUDIT_OPENED = "trade_audit_opened"
TRADE_AUDIT_PROTECTED = "trade_audit_protected"
TRADE_AUDIT_CLOSE_REQUESTED = "trade_audit_close_requested"
TRADE_AUDIT_COMPLETED = "trade_audit_completed"

EXIT_SOURCE_STOP = "STOP"
EXIT_SOURCE_TAKE = "TAKE"
EXIT_SOURCE_STRATEGY = "STRATEGY"
EXIT_SOURCE_ADAPTIVE = "ADAPTIVE"
EXIT_SOURCE_LEARNING = "LEARNING"
EXIT_SOURCE_MANUAL = "MANUAL"
EXIT_SOURCE_GOVERNOR_RISK = "GOVERNOR_RISK"
EXIT_SOURCE_RECOVERY = "RECOVERY"
EXIT_SOURCE_UNKNOWN = "UNKNOWN"

KNOWN_EXIT_SOURCES = {
    EXIT_SOURCE_STOP,
    EXIT_SOURCE_TAKE,
    EXIT_SOURCE_STRATEGY,
    EXIT_SOURCE_ADAPTIVE,
    EXIT_SOURCE_LEARNING,
    EXIT_SOURCE_MANUAL,
    EXIT_SOURCE_GOVERNOR_RISK,
    EXIT_SOURCE_RECOVERY,
    EXIT_SOURCE_UNKNOWN,
}


@dataclass(frozen=True)
class TradeAuditEntry:
    symbol: str
    trade_id: str | None = None
    trade_plan_id: str | None = None
    environment: str | None = None
    execution_mode: str | None = None
    operational_mode: str | None = None
    strategy_name: str | None = None
    strategy_module: str | None = None
    strategy_class: str | None = None
    entry_time: str | None = None
    entry_reason: str | None = None
    strategy_score: int | Decimal | None = None
    strategy_priority: str | None = None
    strategy_confidence: int | Decimal | None = None
    entry_price: Decimal | str | None = None
    quantity: Decimal | str | None = None
    direction: str | None = None
    stop_price: Decimal | str | None = None
    take_profit_price: Decimal | str | None = None
    planned_stop_price: Decimal | str | None = None
    planned_take_profit_price: Decimal | str | None = None
    reward_risk_liquido: Decimal | str | None = None
    expected_net_gain: Decimal | str | None = None
    expected_net_gain_bps: Decimal | str | None = None
    expected_net_loss: Decimal | str | None = None
    round_trip_cost: Decimal | str | None = None
    round_trip_cost_bps: Decimal | str | None = None
    estimated_entry_fee: Decimal | str | None = None
    estimated_exit_fee: Decimal | str | None = None
    estimated_spread_cost: Decimal | str | None = None
    estimated_slippage_cost: Decimal | str | None = None
    estimated_funding_cost: Decimal | str | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    exit_source: str | None = None
    seconds_in_position: Decimal | float | str | None = None
    gross_pnl: Decimal | str | None = None
    net_pnl: Decimal | str | None = None
    pnl_status: str | None = None
    pnl_source: str | None = None
    close_price: Decimal | str | None = None
    exited_by_stop: bool = False
    exited_by_take: bool = False
    exited_by_strategy: bool = False
    exited_by_adaptive: bool = False
    exited_by_learning: bool = False
    exited_by_manual: bool = False
    exited_by_governor: bool = False
    exited_by_recovery: bool = False
    protection_source: str | None = None
    protection_model: str | None = None
    stop_client_algo_id: str | None = None
    take_client_algo_id: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TradeAuditRecorder:
    """Append-only consolidated audit journal for all trade lifecycle events."""

    def __init__(
        self,
        path: str | Path = "data/trade_audit.jsonl",
        *,
        environment: str | None = None,
        execution_mode: str | None = None,
        operational_mode: str | None = None,
        strategy_name: str | None = None,
        strategy_module: str | None = None,
        strategy_class: str | None = None,
        logger=None,
        journals: JournalRegistry | None = None,
    ):
        self.path = Path(path)
        self.journal = (journals or JournalRegistry()).journal(self.path)
        self.environment = environment
        self.execution_mode = execution_mode
        self.operational_mode = operational_mode
        self.strategy_name = strategy_name
        self.strategy_module = strategy_module
        self.strategy_class = strategy_class
        self.logger = logger or get_logger(__name__)
        self._lifecycle_cache: dict[tuple[str, str], dict[str, Any]] = {}

    @classmethod
    def from_config(
        cls,
        config,
        *,
        operational_mode: str | None = None,
        journals: JournalRegistry | None = None,
    ) -> TradeAuditRecorder:
        strategy_module = getattr(config, "strategy_module", None)
        strategy_class = getattr(config, "strategy_class", None)
        strategy_name = (
            f"{strategy_module}.{strategy_class}"
            if strategy_module and strategy_class
            else strategy_class or strategy_module
        )
        return cls(
            getattr(config, "trade_audit_path", "data/trade_audit.jsonl"),
            environment=_enum_value(getattr(config, "environment", None)),
            execution_mode=_enum_value(getattr(config, "execution_mode", None)),
            operational_mode=operational_mode or operational_mode_from_config(config),
            strategy_name=strategy_name,
            strategy_module=strategy_module,
            strategy_class=strategy_class,
            journals=journals,
        )

    def record_open_candidate(self, **fields_: Any) -> None:
        if fields_.get("entry_reason") is None and fields_.get("reason") is not None:
            fields_["entry_reason"] = fields_.get("reason")
        self._record(TRADE_AUDIT_OPEN_CANDIDATE, **fields_)

    def record_open(self, **fields_: Any) -> None:
        if fields_.get("entry_reason") is None and fields_.get("reason") is not None:
            fields_["entry_reason"] = fields_.get("reason")
        self._record(TRADE_AUDIT_OPENED, **fields_)

    def record_protected(self, **fields_: Any) -> None:
        self._record(TRADE_AUDIT_PROTECTED, **fields_)

    def record_close(self, **fields_: Any) -> None:
        fields_ = self._merge_lifecycle_context(fields_)

        if fields_.get("exit_reason") is None and fields_.get("reason") is not None:
            fields_["exit_reason"] = fields_.get("reason")

        fields_.setdefault(
            "exit_source",
            self.infer_exit_source(
                reason=fields_.get("exit_reason") or fields_.get("reason"),
                source=fields_.get("exit_source"),
                event_type=TRADE_AUDIT_CLOSE_REQUESTED,
                diagnostics=fields_.get("diagnostics"),
                strategy_module=fields_.get("strategy_module") or self.strategy_module,
                strategy_class=fields_.get("strategy_class") or self.strategy_class,
            ),
        )
        self._record(TRADE_AUDIT_CLOSE_REQUESTED, **fields_)

    def record_completed(self, **fields_: Any) -> None:
        fields_ = self._merge_lifecycle_context(fields_)

        if fields_.get("exit_reason") is None and fields_.get("reason") is not None:
            fields_["exit_reason"] = fields_.get("reason")

        if fields_.get("close_price") in {None, ""}:
            close_price = _close_price_from_diagnostics(fields_.get("diagnostics"))
            if close_price is not None:
                fields_["close_price"] = close_price

        if fields_.get("gross_pnl") in {None, ""}:
            gross_pnl = _gross_pnl_from_diagnostics(fields_.get("diagnostics"))
            if gross_pnl is not None:
                fields_["gross_pnl"] = gross_pnl

        fields_.setdefault(
            "exit_source",
            self.infer_exit_source(
                reason=fields_.get("exit_reason") or fields_.get("reason"),
                source=fields_.get("exit_source"),
                event_type=TRADE_AUDIT_COMPLETED,
                order_type=fields_.get("order_type"),
                diagnostics=fields_.get("diagnostics"),
                strategy_module=fields_.get("strategy_module") or self.strategy_module,
                strategy_class=fields_.get("strategy_class") or self.strategy_class,
            ),
        )

        if fields_.get("seconds_in_position") is None:
            fields_["seconds_in_position"] = seconds_between(
                fields_.get("entry_time"),
                fields_.get("exit_time"),
            )
        if fields_.get("net_pnl") in {None, ""}:
            observe_event(
                "pnl_unknown_in_journal",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=fields_.get("symbol"),
                trade_id=fields_.get("trade_id"),
                trade_plan_id=fields_.get("trade_plan_id"),
                pnl_status=fields_.get("pnl_status"),
                pnl_source=fields_.get("pnl_source"),
            )

        self._record(TRADE_AUDIT_COMPLETED, **fields_)

    @staticmethod
    def infer_exit_source(
        *,
        reason: Any = None,
        source: Any = None,
        event_type: Any = None,
        order_type: Any = None,
        diagnostics: Any = None,
        strategy_module: Any = None,
        strategy_class: Any = None,
    ) -> str:
        explicit = normalize_exit_source(source)
        if explicit != EXIT_SOURCE_UNKNOWN:
            return explicit

        reason_text = str(reason or "").upper()
        order_text = str(order_type or "").upper()
        event_text = str(event_type or "").upper()

        diagnostics_text = ""
        if isinstance(diagnostics, str):
            diagnostics_text = diagnostics.upper()
        elif isinstance(diagnostics, dict):
            diagnostics_text = _flatten_text(diagnostics)

        # When the order type comes explicitly from Binance/protection,
        # it is the strongest evidence for STOP/TAKE classification.
        if "TAKE_PROFIT_MARKET" in order_text or "TAKE_PROFIT" in order_text:
            return EXIT_SOURCE_TAKE

        if "STOP_MARKET" in order_text or "STOP_LOSS" in order_text or "STOP_TRIGGER" in order_text:
            return EXIT_SOURCE_STOP

        # Strong operational reasons must take priority over strategy reasons.
        # Example: protection_creation_failed must not become STRATEGY
        # just because strategy_class exists in the recorder.
        if _looks_like_recovery_reason(reason_text):
            return EXIT_SOURCE_RECOVERY

        if "LEARNING" in reason_text:
            return EXIT_SOURCE_LEARNING

        if "ADAPTIVE" in reason_text:
            return EXIT_SOURCE_ADAPTIVE

        if (
            "MANUAL" in reason_text
            or "CONTROL_REQUEST" in reason_text
            or "USER_REQUEST" in reason_text
        ):
            return EXIT_SOURCE_MANUAL

        if "RISK" in reason_text or "GOVERNOR" in reason_text or "DAILY_LOSS" in reason_text:
            return EXIT_SOURCE_GOVERNOR_RISK

        # Only now consider strategy-driven close.
        # This prevents planned fields like take_profit_price inside diagnostics
        # from being mistaken for a TAKE exit.
        if _looks_like_strategy_exit_reason(
            reason_text,
            strategy_module=strategy_module,
            strategy_class=strategy_class,
        ):
            return EXIT_SOURCE_STRATEGY

        # Last resort: inspect the full diagnostics text.
        # Real stop/take order responses may appear here.
        full_text = f"{event_text} {diagnostics_text}"

        if "TAKE_PROFIT_MARKET" in full_text:
            return EXIT_SOURCE_TAKE

        if "STOP_MARKET" in full_text or "STOP_LOSS" in full_text or "STOP_TRIGGER" in full_text:
            return EXIT_SOURCE_STOP

        if _looks_like_recovery_reason(full_text):
            return EXIT_SOURCE_RECOVERY

        if "ADAPTIVE" in full_text:
            return EXIT_SOURCE_ADAPTIVE

        if "LEARNING" in full_text:
            return EXIT_SOURCE_LEARNING

        if "MANUAL" in full_text or "CONTROL_REQUEST" in full_text or "USER_REQUEST" in full_text:
            return EXIT_SOURCE_MANUAL

        if "RISK" in full_text or "GOVERNOR" in full_text or "DAILY_LOSS" in full_text:
            return EXIT_SOURCE_GOVERNOR_RISK

        if "STRATEGY" in full_text:
            return EXIT_SOURCE_STRATEGY

        return EXIT_SOURCE_UNKNOWN

    def _record(self, event_type: str, **fields_: Any) -> None:
        try:
            payload = self._entry_payload(**fields_)
            self.journal.append(event_type, **payload)
            self._remember_lifecycle_event(event_type, payload)
        except Exception as error:
            symbol = str(fields_.get("symbol") or "UNKNOWN")
            trade_id = str(fields_.get("trade_id") or fields_.get("trade_plan_id") or "?")
            self.logger.warning(
                "Falha ao gravar trade audit: %s | event_type=%s | symbol=%s"
                " | trade_id=%s | error_type=%s",
                error,
                event_type,
                symbol,
                trade_id,
                type(error).__name__,
            )

    def _remember_lifecycle_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type not in {
            TRADE_AUDIT_OPEN_CANDIDATE,
            TRADE_AUDIT_OPENED,
            TRADE_AUDIT_PROTECTED,
            TRADE_AUDIT_CLOSE_REQUESTED,
            TRADE_AUDIT_COMPLETED,
        }:
            return

        for key in self._cache_keys(payload):
            previous = self._lifecycle_cache.get(key, {})
            self._lifecycle_cache[key] = _merge_audit_context(previous, payload)

    def _merge_lifecycle_context(self, fields_: dict[str, Any]) -> dict[str, Any]:
        context = self._cached_lifecycle_context(fields_)
        if not context:
            context = self._load_recent_lifecycle_context(fields_)

        if not context:
            return dict(fields_)

        merged = dict(context)
        for key, value in fields_.items():
            if _has_value(value):
                merged[key] = value

        current_diagnostics = fields_.get("diagnostics")
        if not isinstance(current_diagnostics, dict):
            current_diagnostics = (
                {"raw_diagnostics": current_diagnostics} if current_diagnostics else {}
            )

        merged["diagnostics"] = {
            **current_diagnostics,
            "audit_lifecycle_context": {
                key: value for key, value in context.items() if key not in {"diagnostics"}
            },
        }
        return merged

    def _cached_lifecycle_context(self, fields_: dict[str, Any]) -> dict[str, Any]:
        for key in self._cache_keys(fields_):
            cached = self._lifecycle_cache.get(key)
            if cached:
                return dict(cached)
        return {}

    def _load_recent_lifecycle_context(
        self,
        fields_: dict[str, Any],
        *,
        max_lines: int = 5000,
    ) -> dict[str, Any]:
        if not self.path.exists():
            return {}

        symbol = str(fields_.get("symbol") or "").upper().strip()
        trade_id = str(fields_.get("trade_id") or "").strip()
        trade_plan_id = str(fields_.get("trade_plan_id") or "").strip()

        if not symbol and not trade_id and not trade_plan_id:
            return {}

        lines: deque[str] = deque(maxlen=max_lines)
        try:
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    lines.append(line)
        except Exception as error:
            self.logger.warning("Falha ao ler contexto recente do trade audit: %s", error)
            return {}

        context: dict[str, Any] = {}
        for line in lines:
            try:
                event = json.loads(line)
            except Exception:
                continue

            event_type = str(event.get("event_type") or "")
            if event_type not in {
                TRADE_AUDIT_OPEN_CANDIDATE,
                TRADE_AUDIT_OPENED,
                TRADE_AUDIT_PROTECTED,
                TRADE_AUDIT_CLOSE_REQUESTED,
            }:
                continue

            data = event.get("data") or {}
            if not isinstance(data, dict):
                continue

            if symbol and str(data.get("symbol") or "").upper().strip() != symbol:
                continue

            same_trade_id = trade_id and str(data.get("trade_id") or "").strip() == trade_id
            same_plan_id = (
                trade_plan_id and str(data.get("trade_plan_id") or "").strip() == trade_plan_id
            )

            if not same_trade_id and not same_plan_id:
                continue

            context = _merge_audit_context(context, data)

        return context

    def _cache_keys(self, payload: dict[str, Any]) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        for field_name in ("trade_id", "trade_plan_id"):
            value = str(payload.get(field_name) or "").strip()
            if value:
                keys.append((field_name, value))
        return keys

    def _entry_payload(self, **fields_: Any) -> dict[str, Any]:
        data = dict(fields_)
        data.setdefault("environment", self.environment)
        data.setdefault("execution_mode", self.execution_mode)
        data.setdefault("operational_mode", self.operational_mode)
        data.setdefault("strategy_name", self.strategy_name)
        data.setdefault("strategy_module", self.strategy_module)
        data.setdefault("strategy_class", self.strategy_class)

        data["symbol"] = str(data.get("symbol") or "").upper().strip()
        if not data["symbol"]:
            data["symbol"] = "UNKNOWN"

        if data.get("exit_source") is not None:
            data["exit_source"] = normalize_exit_source(data.get("exit_source"))
        _apply_exit_source_flags(data)

        allowed_fields = {item.name for item in fields(TradeAuditEntry)}
        extra = {
            key: value
            for key, value in data.items()
            if key not in allowed_fields and value is not None
        }
        diagnostics = data.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {"raw_diagnostics": diagnostics} if diagnostics else {}
        if extra:
            diagnostics = {**diagnostics, "extra": extra}

        entry_data = {key: data.get(key) for key in allowed_fields if key != "diagnostics"}
        entry_data["diagnostics"] = _deep_to_plain(diagnostics)
        try:
            # The dataclass fields are selected dynamically from its schema above.
            return asdict(TradeAuditEntry(**entry_data))  # type: ignore[arg-type]
        except Exception as error:
            escaped = _scan_non_serializable(entry_data)
            raise type(error)(f"{error} | escaped_non_serializable={escaped}") from error


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _merge_audit_context(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(previous)
    for key, value in current.items():
        if _has_value(value):
            merged[key] = value
    return merged


def _close_price_from_diagnostics(diagnostics: Any) -> Any:
    if not isinstance(diagnostics, dict):
        return None

    for path in (
        ("pnl_result", "close_price_estimate"),
        ("order_response", "avgPrice"),
        ("order_response", "averagePrice"),
        ("order_response", "avg_price"),
    ):
        value = _nested_value(diagnostics, path)
        if _has_value(value):
            return value

    order_response = diagnostics.get("order_response")
    if isinstance(order_response, dict):
        try:
            executed_qty = Decimal(str(order_response.get("executedQty", "0") or "0"))
            cum_quote = Decimal(
                str(
                    order_response.get(
                        "cumQuote",
                        order_response.get("cumQuoteQty", "0"),
                    )
                    or "0"
                )
            )
            if executed_qty > 0 and cum_quote > 0:
                return cum_quote / executed_qty
        except Exception:
            return None

    return None


def _gross_pnl_from_diagnostics(diagnostics: Any) -> Any:
    if not isinstance(diagnostics, dict):
        return None
    return _nested_value(diagnostics, ("pnl_result", "gross_pnl_estimate"))


def _nested_value(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _looks_like_recovery_reason(text: str) -> bool:
    text = str(text or "").upper()
    return (
        "RECOVERY" in text
        or "RECOVER" in text
        or "RECONCILE" in text
        or "UNPROTECTED" in text
        or "PROTECTION_CREATION_FAILED" in text
        or "PROTECTION_FAILURE" in text
    )


def _looks_like_strategy_exit_reason(
    reason_text: str,
    *,
    strategy_module: Any = None,
    strategy_class: Any = None,
) -> bool:
    text = str(reason_text or "").upper()

    blocked_markers = (
        "TAKE_PROFIT_MARKET",
        "STOP_MARKET",
        "RECOVERY",
        "RECOVER",
        "RECONCILE",
        "UNPROTECTED",
        "PROTECTION",
        "GOVERNOR",
        "RISK",
        "DAILY_LOSS",
        "MANUAL",
        "CONTROL_REQUEST",
        "USER_REQUEST",
        "LEARNING",
        "ADAPTIVE",
    )
    if any(marker in text for marker in blocked_markers):
        return False

    strategy_markers = (
        "STRATEGY",
        "ESTRATEGIA",
        "ESTRATÉGIA",
        "TRENDPULLBACK",
        "TREND_PULLBACK",
        "PULLBACK",
    )

    close_markers = (
        "STRATEGY CLOSE",
        "CLOSE POR",
        "CLOSE DE",
        "FECHAMENTO POR ESTRATÉGIA",
        "FECHAMENTO POR ESTRATEGIA",
        "SAÍDA POR ESTRATÉGIA",
        "SAIDA POR ESTRATEGIA",
        "FALHA DE TENDÊNCIA",
        "FALHA DE TENDENCIA",
        "FALHA FORTE DE TENDÊNCIA",
        "FALHA FORTE DE TENDENCIA",
        "TEMPO MÁXIMO",
        "TEMPO MAXIMO",
    )

    if any(marker in text for marker in strategy_markers):
        return True

    if any(marker in text for marker in close_markers):
        return True

    if strategy_module or strategy_class:
        return (
            "CLOSE" in text
            or "FECHAMENTO" in text
            or "FECH" in text
            or "SAÍDA" in text
            or "SAIDA" in text
        )

    return False


def operational_mode_from_config(config) -> str:
    symbols = tuple(getattr(config, "symbols", ()) or ())
    if getattr(config, "multi_symbol_enabled", False) or len(symbols) > 1:
        return "GOVERNOR_MULTI_SYMBOL"
    return "GOVERNOR_SINGLE_SYMBOL"


def normalize_exit_source(value: Any) -> str:
    normalized = str(value or "").upper().strip()
    if not normalized:
        return EXIT_SOURCE_UNKNOWN
    aliases = {
        "STOP_MARKET": EXIT_SOURCE_STOP,
        "STOP_LOSS": EXIT_SOURCE_STOP,
        "TAKE_PROFIT": EXIT_SOURCE_TAKE,
        "TAKE_PROFIT_MARKET": EXIT_SOURCE_TAKE,
        "GOVERNOR": EXIT_SOURCE_GOVERNOR_RISK,
        "GOVERNOR_SAFETY": EXIT_SOURCE_GOVERNOR_RISK,
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in KNOWN_EXIT_SOURCES else EXIT_SOURCE_UNKNOWN


def seconds_between(entry_time: Any, exit_time: Any) -> Decimal | None:
    entry = _parse_time(entry_time)
    exit_ = _parse_time(exit_time)
    if entry is None or exit_ is None or exit_ < entry:
        return None
    return Decimal(str((exit_ - entry).total_seconds()))


def _parse_time(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError):
        numeric = None
    if numeric is not None:
        if numeric > Decimal("100000000000"):
            numeric = numeric / Decimal("1000")
        return datetime.fromtimestamp(float(numeric), tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _apply_exit_source_flags(data: dict[str, Any]) -> None:
    source = normalize_exit_source(data.get("exit_source"))
    flags = {
        "exited_by_stop": source == EXIT_SOURCE_STOP,
        "exited_by_take": source == EXIT_SOURCE_TAKE,
        "exited_by_strategy": source == EXIT_SOURCE_STRATEGY,
        "exited_by_adaptive": source == EXIT_SOURCE_ADAPTIVE,
        "exited_by_learning": source == EXIT_SOURCE_LEARNING,
        "exited_by_manual": source == EXIT_SOURCE_MANUAL,
        "exited_by_governor": source == EXIT_SOURCE_GOVERNOR_RISK,
        "exited_by_recovery": source == EXIT_SOURCE_RECOVERY,
    }

    if source != EXIT_SOURCE_UNKNOWN:
        # Lifecycle context is captured while a trade is open, when every exit
        # flag is necessarily false. Once the close source is known it is the
        # authority; retaining those cached defaults makes the audit contradict
        # its own exit_source.
        data.update(flags)
        return

    for name, value in flags.items():
        data.setdefault(name, value)


def _flatten_text(value: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, item in value.items():
        parts.append(str(key))
        if isinstance(item, dict):
            parts.append(_flatten_text(item))
        elif isinstance(item, (list, tuple, set)):
            parts.extend(str(child) for child in item)
        else:
            parts.append(str(item))
    return " ".join(parts).upper()


def _deep_to_plain(value: Any) -> Any:
    """Recursively convert any Mapping or dataclass to plain dict.

    dataclasses.asdict uses copy.deepcopy for unknown types, which fails on
    MappingProxyType and frozen dataclasses that contain it. This
    pre-sanitization ensures diagnostics are always plain dicts before asdict
    is called.

    Dataclasses are expanded field-by-field via getattr (no deepcopy) so that
    frozen dataclasses whose internal fields are MappingProxyType — such as
    PositionExecutionResult.diagnostics — are handled safely.
    """
    from collections.abc import Mapping

    if isinstance(value, dict):
        return {k: _deep_to_plain(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {k: _deep_to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_to_plain(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _deep_to_plain(getattr(value, f.name)) for f in fields(value)}
    return value


def _scan_non_serializable(value: Any, path: str = "") -> list[str]:
    """Return paths of values that copy.deepcopy cannot handle.

    Only called inside exception handlers, so performance is not a concern.
    Used to produce a diagnostic message when _deep_to_plain misses a type.
    """
    results: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            results.extend(_scan_non_serializable(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            results.extend(_scan_non_serializable(v, f"{path}[{i}]"))
    else:
        try:
            copy.deepcopy(value)
        except Exception:
            results.append(f"{path}:{type(value).__name__}")
    return results


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))
