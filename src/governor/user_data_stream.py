import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from binance.futures_client import BinanceFuturesClient
from utils.logger import get_logger


@dataclass(frozen=True)
class UserDataStreamClassification:
    category: str
    operational_type: str
    severity: str
    symbol: str
    symbols: list[str]
    summary: str
    order_status: str = ""
    execution_type: str = ""
    account_update_reason: str = ""
    details: dict[str, Any] | None = None
    should_update_order_cache: bool = False
    should_update_position_cache: bool = False
    should_update_account_cache: bool = False
    shadow_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "operational_type": self.operational_type,
            "severity": self.severity,
            "symbol": self.symbol,
            "symbols": self.symbols,
            "summary": self.summary,
            "order_status": self.order_status,
            "execution_type": self.execution_type,
            "account_update_reason": self.account_update_reason,
            "details": self.details or {},
            "should_update_order_cache": self.should_update_order_cache,
            "should_update_position_cache": self.should_update_position_cache,
            "should_update_account_cache": self.should_update_account_cache,
            "shadow_only": self.shadow_only,
        }


@dataclass(frozen=True)
class UserDataStreamEvent:
    event_type: str
    event_time: float | None
    transaction_time: float | None
    symbol: str
    raw: dict[str, Any]
    received_at: float
    classification: UserDataStreamClassification
    source: str = "user_data_stream"
    shadow_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "event_time": self.event_time,
            "transaction_time": self.transaction_time,
            "symbol": self.symbol,
            "raw": self.raw,
            "received_at": self.received_at,
            "source": self.source,
            "shadow_only": self.shadow_only,
            "classification": self.classification.to_dict(),
            "operational_category": self.classification.category,
            "operational_type": self.classification.operational_type,
            "severity": self.classification.severity,
        }


def parse_user_data_stream_payload(
    raw: str | dict[str, Any],
    *,
    received_at: float | None = None,
    shadow_only: bool = True,
) -> UserDataStreamEvent | None:
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None

    if not isinstance(payload, dict):
        return None

    event_type = str(payload.get("e", "")).strip()
    if not event_type:
        return None

    received = time.time() if received_at is None else float(received_at)

    classification = classify_user_data_stream_payload(
        payload,
        shadow_only=shadow_only,
    )

    return UserDataStreamEvent(
        event_type=event_type,
        event_time=_event_time_seconds(payload.get("E")),
        transaction_time=_event_time_seconds(payload.get("T")),
        symbol=_extract_symbol(payload),
        raw=payload,
        received_at=received,
        classification=classification,
        shadow_only=shadow_only,
    )


class UserDataStreamHub:
    def __init__(
        self,
        *,
        client: BinanceFuturesClient,
        base_url: str,
        keepalive_seconds: float = 30 * 60,
        reconnect_seconds: float = 5.0,
        heartbeat_seconds: float = 60.0,
        max_pending_events: int = 500,
        shadow_only: bool = True,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        if keepalive_seconds <= 0:
            raise ValueError("keepalive_seconds precisa ser maior que zero")
        if reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds precisa ser maior que zero")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds precisa ser maior que zero")
        if max_pending_events < 1:
            raise ValueError("max_pending_events precisa ser no minimo 1")

        self.client = client
        self.base_url = base_url.rstrip("/")
        self.keepalive_seconds = float(keepalive_seconds)
        self.reconnect_seconds = float(reconnect_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.shadow_only = bool(shadow_only)
        self.event_callback = event_callback

        self.pending_events: deque[UserDataStreamEvent] = deque(maxlen=int(max_pending_events))
        self.listen_key = ""

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._ws_app: Any | None = None
        self._running = False
        self._connected = False
        self._last_message_at: float | None = None
        self._last_keepalive_at: float | None = None
        self._last_error = ""
        self._events_since_heartbeat = 0
        self._last_heartbeat_at = 0.0
        self.logger = get_logger(__name__)

    @property
    def stream_url(self) -> str:
        if not self.listen_key:
            return ""
        return f"{self.base_url}/ws/{self.listen_key}"

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                name="alphora-user-data-stream",
                daemon=True,
            )
            self._thread.start()

        self.logger.info(
            "User Data Stream shadow iniciado | endpoint=%s | shadow_only=%s",
            self.base_url,
            self.shadow_only,
        )

    def stop(self) -> None:
        listen_key = ""
        try:
            with self._lock:
                self._running = False
                self._connected = False
                self._stop_event.set()
                ws_app = self._ws_app
                thread = self._thread
                keepalive_thread = self._keepalive_thread
                listen_key = self.listen_key

            if ws_app is not None:
                try:
                    ws_app.close()
                except Exception as error:
                    self.logger.warning(
                        "Falha ao fechar User Data Stream WebSocket: %s",
                        error,
                    )

            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=min(2.0, self.reconnect_seconds))

            if (
                keepalive_thread is not None
                and keepalive_thread.is_alive()
                and keepalive_thread is not threading.current_thread()
            ):
                keepalive_thread.join(timeout=1.0)

            if listen_key:
                try:
                    self.client.close_user_data_stream()
                except Exception as error:
                    self.logger.warning(
                        "Falha ao encerrar listenKey do User Data Stream: %s",
                        error,
                    )
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self.listen_key = ""

    def ingest_message(
        self,
        raw: str | dict[str, Any],
        *,
        received_at: float | None = None,
    ) -> bool:
        event = parse_user_data_stream_payload(
            raw,
            received_at=received_at,
            shadow_only=self.shadow_only,
        )
        if event is None:
            return False

        with self._lock:
            self.pending_events.append(event)
            self._last_message_at = event.received_at
            self._events_since_heartbeat += 1

        self._notify_event(event)
        return True

    def drain_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            return []

        events: list[dict[str, Any]] = []
        with self._lock:
            while self.pending_events and len(events) < limit:
                events.append(self.pending_events.popleft().to_dict())

        return events

    def health_snapshot(self, now: float | None = None) -> dict[str, Any]:
        current_time = time.time() if now is None else float(now)
        with self._lock:
            last_message_age = (
                None
                if self._last_message_at is None
                else max(0.0, current_time - self._last_message_at)
            )
            last_keepalive_age = (
                None
                if self._last_keepalive_at is None
                else max(0.0, current_time - self._last_keepalive_at)
            )
            return {
                "enabled": True,
                "running": self._running,
                "connected": self._connected,
                "listen_key_active": bool(self.listen_key),
                "pending_events": len(self.pending_events),
                "events_since_heartbeat": self._events_since_heartbeat,
                "last_message_at": self._last_message_at,
                "last_message_age_seconds": last_message_age,
                "last_keepalive_at": self._last_keepalive_at,
                "last_keepalive_age_seconds": last_keepalive_age,
                "last_error": self._last_error,
                "shadow_only": self.shadow_only,
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._open_listen_key()
                self._start_keepalive_loop()

                import websocket

                ws_app = websocket.WebSocketApp(
                    self.stream_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                with self._lock:
                    self._ws_app = ws_app

                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as error:
                self._record_error(error)
            finally:
                with self._lock:
                    self._connected = False
                    self._ws_app = None

            if self._stop_event.is_set():
                break

            self.logger.warning(
                "User Data Stream desconectado; reconectando em %.1fs",
                self.reconnect_seconds,
            )
            self._stop_event.wait(self.reconnect_seconds)

        with self._lock:
            self._running = False
            self._connected = False

    def _open_listen_key(self) -> None:
        response = self.client.start_user_data_stream()
        listen_key = str(response.get("listenKey", "")).strip()
        if not listen_key:
            raise RuntimeError("Binance nao retornou listenKey para User Data Stream")

        with self._lock:
            self.listen_key = listen_key
            self._last_error = ""

    def _start_keepalive_loop(self) -> None:
        with self._lock:
            thread = self._keepalive_thread
            if thread is not None and thread.is_alive():
                return

            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop,
                name="alphora-user-data-keepalive",
                daemon=True,
            )
            self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        while not self._stop_event.wait(self.keepalive_seconds):
            try:
                self.client.keepalive_user_data_stream()
                with self._lock:
                    self._last_keepalive_at = time.time()
                    self._last_error = ""
            except Exception as error:
                self._record_error(error)

    def _notify_event(self, event: UserDataStreamEvent) -> None:
        callback = self.event_callback
        if callback is None:
            return

        try:
            callback(event.to_dict())
        except Exception as error:
            self._record_error(error)

    def _on_open(self, _ws_app: Any) -> None:
        with self._lock:
            self._connected = True
            self._last_error = ""
            self._last_heartbeat_at = time.time()
        self.logger.info("User Data Stream conectado em shadow")

    def _on_message(self, _ws_app: Any, message: str) -> None:
        now = time.time()
        self.ingest_message(message, received_at=now)
        self._maybe_log_heartbeat(now)

    def _on_error(self, _ws_app: Any, error: Exception) -> None:
        self._record_error(error)

    def _on_close(
        self,
        _ws_app: Any,
        status_code: int | None,
        message: str | None,
    ) -> None:
        with self._lock:
            self._connected = False
        if not self._stop_event.is_set():
            self.logger.warning(
                "User Data Stream fechado | status=%s | motivo=%s",
                status_code,
                message or "",
            )

    def _record_error(self, error: Exception) -> None:
        message = str(error)
        with self._lock:
            self._connected = False
            self._last_error = message
        self.logger.error("Erro no User Data Stream: %s", message)

    def _maybe_log_heartbeat(self, now: float) -> None:
        with self._lock:
            if now - self._last_heartbeat_at < self.heartbeat_seconds:
                return

            events = self._events_since_heartbeat
            self._events_since_heartbeat = 0
            self._last_heartbeat_at = now

        self.logger.info(
            "User Data Stream ativo | shadow_only=%s | eventos_ultimo_periodo=%s",
            self.shadow_only,
            events,
        )


def classify_user_data_stream_payload(
    payload: dict[str, Any],
    *,
    shadow_only: bool = True,
) -> UserDataStreamClassification:
    event_type = str(payload.get("e", "")).upper().strip()
    symbol = _extract_symbol(payload)

    if event_type == "ORDER_TRADE_UPDATE":
        return _classify_order_trade_update(
            payload,
            symbol=symbol,
            shadow_only=shadow_only,
        )

    if event_type == "ACCOUNT_UPDATE":
        return _classify_account_update(
            payload,
            symbol=symbol,
            shadow_only=shadow_only,
        )

    if event_type == "MARGIN_CALL":
        return UserDataStreamClassification(
            category="RISK",
            operational_type="MARGIN_CALL",
            severity="CRITICAL",
            symbol=symbol,
            symbols=_extract_symbols(payload),
            summary="Chamada de margem recebida pelo User Data Stream.",
            details={"event_type": event_type},
            should_update_position_cache=True,
            should_update_account_cache=True,
            shadow_only=shadow_only,
        )

    if event_type == "ACCOUNT_CONFIG_UPDATE":
        return UserDataStreamClassification(
            category="ACCOUNT_CONFIG",
            operational_type="ACCOUNT_CONFIG_UPDATE",
            severity="INFO",
            symbol=symbol,
            symbols=_extract_symbols(payload),
            summary="Configuracao de conta atualizada.",
            details={"event_type": event_type},
            should_update_account_cache=True,
            shadow_only=shadow_only,
        )

    if event_type == "LISTENKEYEXPIRED":
        return UserDataStreamClassification(
            category="CONNECTION",
            operational_type="LISTEN_KEY_EXPIRED",
            severity="CRITICAL",
            symbol="",
            symbols=[],
            summary="ListenKey expirou; stream privado precisa reconectar.",
            details={"event_type": event_type},
            shadow_only=shadow_only,
        )

    return UserDataStreamClassification(
        category="UNKNOWN",
        operational_type=event_type or "UNKNOWN",
        severity="INFO",
        symbol=symbol,
        symbols=_extract_symbols(payload),
        summary="Evento privado ainda sem classificador especifico.",
        details={"event_type": event_type},
        shadow_only=shadow_only,
    )


def _classify_order_trade_update(
    payload: dict[str, Any],
    *,
    symbol: str,
    shadow_only: bool,
) -> UserDataStreamClassification:
    order = payload.get("o")
    if not isinstance(order, dict):
        order = {}

    execution_type = str(order.get("x", "") or "").upper().strip()
    order_status = str(order.get("X", "") or "").upper().strip()
    side = str(order.get("S", "") or "").upper().strip()
    order_type = str(order.get("o", "") or "").upper().strip()
    client_order_id = str(order.get("c", "") or "").strip()
    order_id = str(order.get("i", "") or "").strip()
    position_side = str(order.get("ps", "") or "").upper().strip()
    reduce_only = bool(order.get("R", False))

    details = {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "side": side,
        "order_type": order_type,
        "position_side": position_side,
        "reduce_only": reduce_only,
        "quantity": str(order.get("q", "") or ""),
        "price": str(order.get("p", "") or ""),
        "average_price": str(order.get("ap", "") or ""),
        "last_filled_quantity": str(order.get("l", "") or ""),
        "filled_quantity": str(order.get("z", "") or ""),
        "last_filled_price": str(order.get("L", "") or ""),
        "commission_asset": str(order.get("N", "") or ""),
        "commission": str(order.get("n", "") or ""),
        "realized_pnl": str(order.get("rp", "") or ""),
    }

    if execution_type == "TRADE":
        operational_type = "ORDER_TRADE_FILL"
        severity = "INFO"
        summary = "Ordem recebeu execucao/fill."
        should_update_position_cache = True
        should_update_account_cache = True
    elif order_status in {"FILLED", "PARTIALLY_FILLED"}:
        operational_type = "ORDER_FILL_STATUS"
        severity = "INFO"
        summary = "Status de ordem indica preenchimento."
        should_update_position_cache = True
        should_update_account_cache = True
    elif order_status in {"CANCELED", "EXPIRED"}:
        operational_type = "ORDER_TERMINAL_UPDATE"
        severity = "INFO"
        summary = "Ordem encerrada sem novo fill relevante."
        should_update_position_cache = False
        should_update_account_cache = False
    elif order_status == "REJECTED" or execution_type == "REJECTED":
        operational_type = "ORDER_REJECTED"
        severity = "WARNING"
        summary = "Ordem rejeitada pela exchange."
        should_update_position_cache = False
        should_update_account_cache = False
    elif order_status == "NEW":
        operational_type = "ORDER_ACCEPTED"
        severity = "INFO"
        summary = "Ordem aceita pela exchange."
        should_update_position_cache = False
        should_update_account_cache = False
    else:
        operational_type = "ORDER_STATUS_UPDATE"
        severity = "INFO"
        summary = "Atualizacao de ordem recebida."
        should_update_position_cache = False
        should_update_account_cache = False

    return UserDataStreamClassification(
        category="ORDER",
        operational_type=operational_type,
        severity=severity,
        symbol=symbol,
        symbols=[symbol] if symbol else [],
        summary=summary,
        order_status=order_status,
        execution_type=execution_type,
        details=details,
        should_update_order_cache=True,
        should_update_position_cache=should_update_position_cache,
        should_update_account_cache=should_update_account_cache,
        shadow_only=shadow_only,
    )


def _classify_account_update(
    payload: dict[str, Any],
    *,
    symbol: str,
    shadow_only: bool,
) -> UserDataStreamClassification:
    account = payload.get("a")
    if not isinstance(account, dict):
        account = {}

    reason = str(account.get("m", "") or "").upper().strip()
    balances = account.get("B")
    positions = account.get("P")

    balance_count = len(balances) if isinstance(balances, list) else 0
    position_count = len(positions) if isinstance(positions, list) else 0
    symbols = _extract_symbols(payload)

    if position_count > 0:
        operational_type = "POSITION_ACCOUNT_UPDATE"
        summary = "Atualizacao de conta com mudanca de posicao."
        should_update_position_cache = True
    else:
        operational_type = "ACCOUNT_BALANCE_UPDATE"
        summary = "Atualizacao de conta/saldo recebida."
        should_update_position_cache = False

    return UserDataStreamClassification(
        category="ACCOUNT",
        operational_type=operational_type,
        severity="INFO",
        symbol=symbol,
        symbols=symbols,
        summary=summary,
        account_update_reason=reason,
        details={
            "reason": reason,
            "balance_count": balance_count,
            "position_count": position_count,
        },
        should_update_order_cache=False,
        should_update_position_cache=should_update_position_cache,
        should_update_account_cache=True,
        shadow_only=shadow_only,
    )


def _extract_symbols(payload: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()

    direct = str(payload.get("s", "") or "").upper().strip()
    if direct:
        symbols.add(direct)

    order = payload.get("o")
    if isinstance(order, dict):
        symbol = str(order.get("s", "") or "").upper().strip()
        if symbol:
            symbols.add(symbol)

    account = payload.get("a")
    if isinstance(account, dict):
        positions = account.get("P")
        if isinstance(positions, list):
            for position in positions:
                if not isinstance(position, dict):
                    continue
                symbol = str(position.get("s", "") or "").upper().strip()
                if symbol:
                    symbols.add(symbol)

    margin_positions = payload.get("p")
    if isinstance(margin_positions, list):
        for position in margin_positions:
            if not isinstance(position, dict):
                continue
            symbol = str(position.get("s", "") or "").upper().strip()
            if symbol:
                symbols.add(symbol)

    return sorted(symbols)


def _extract_symbol(payload: dict[str, Any]) -> str:
    direct = str(payload.get("s", "") or "").upper().strip()
    if direct:
        return direct

    order = payload.get("o")
    if isinstance(order, dict):
        symbol = str(order.get("s", "") or "").upper().strip()
        if symbol:
            return symbol

    account = payload.get("a")
    if isinstance(account, dict):
        positions = account.get("P")
        if isinstance(positions, list):
            for position in positions:
                if isinstance(position, dict):
                    symbol = str(position.get("s", "") or "").upper().strip()
                    if symbol:
                        return symbol

    return ""


def _event_time_seconds(value: Any) -> float | None:
    if value in {None, ""}:
        return None

    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None

    return raw / 1000.0
