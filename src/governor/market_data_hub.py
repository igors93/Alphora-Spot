import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from governor.market_shock_detector import MarketShockDetector
from market_data.price_window import MarketPriceWindow
from utils.logger import get_logger

SUPPORTED_MARKET_WS_STREAMS = {"bookTicker"}


@dataclass(frozen=True)
class PriceSnapshot:
    symbol: str
    price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    event_time: float | None
    received_at: float
    source: str = "websocket"


@dataclass(frozen=True)
class MarketStateSnapshot:
    price_snapshot: PriceSnapshot
    prices: tuple[Decimal, ...]


def parse_combined_message(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if isinstance(data, dict):
        return data

    return payload


def parse_book_ticker_payload(
    payload: dict[str, Any],
    *,
    received_at: float | None = None,
) -> PriceSnapshot | None:
    if not isinstance(payload, dict):
        return None

    symbol = str(payload.get("s") or payload.get("symbol") or "").upper().strip()
    if not symbol:
        return None

    bid = _decimal_or_none(payload.get("b") or payload.get("bidPrice"))
    ask = _decimal_or_none(payload.get("a") or payload.get("askPrice"))
    if (
        bid is None
        or ask is None
        or not bid.is_finite()
        or not ask.is_finite()
        or bid <= 0
        or ask <= 0
        or ask < bid
    ):
        return None

    event_time = _event_time_seconds(payload.get("E") or payload.get("T"))
    received = time.time() if received_at is None else float(received_at)
    return PriceSnapshot(
        symbol=symbol,
        price=(bid + ask) / Decimal("2"),
        bid_price=bid,
        ask_price=ask,
        event_time=event_time,
        received_at=received,
    )


class MarketDataHub:
    def __init__(
        self,
        symbols: list[str] | tuple[str, ...],
        base_url: str,
        stream: str = "bookTicker",
        max_price_age_seconds: float = 3.0,
        heartbeat_seconds: float = 60.0,
        reconnect_seconds: float = 5.0,
        dirty_symbols_limit: int = 500,
        price_window: int = 120,
        max_symbols_per_drain: int = 50,
        market_shock_detector_enabled: bool = True,
        market_shock_window_seconds: float = 3.0,
        market_shock_move_bps_threshold: float = 50.0,
        market_shock_critical_bps_threshold: float = 120.0,
        market_shock_cooldown_seconds: float = 10.0,
        market_wakeup_callback: Callable[[dict[str, Any]], None] | None = None,
        price_snapshot_callback: Callable[[PriceSnapshot], None] | None = None,
    ):
        if stream not in SUPPORTED_MARKET_WS_STREAMS:
            raise ValueError("ALPHORA_MARKET_WS_STREAM aceita inicialmente apenas bookTicker")
        if max_price_age_seconds <= 0:
            raise ValueError("max_price_age_seconds precisa ser maior que zero")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds precisa ser maior que zero")
        if reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds precisa ser maior que zero")
        if dirty_symbols_limit < 1:
            raise ValueError("dirty_symbols_limit precisa ser no minimo 1")
        if price_window < 1:
            raise ValueError("price_window precisa ser no minimo 1")
        if max_symbols_per_drain < 1:
            raise ValueError("max_symbols_per_drain precisa ser no minimo 1")

        normalized_symbols = _normalize_symbols(symbols)
        if not normalized_symbols:
            raise ValueError("MarketDataHub exige pelo menos um simbolo")

        self.symbols = tuple(normalized_symbols)
        self._enabled_symbols = set(self.symbols)
        self.base_url = base_url.rstrip("/")
        self.stream = stream
        self.max_price_age_seconds = float(max_price_age_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.reconnect_seconds = float(reconnect_seconds)
        self.dirty_symbols_limit = int(dirty_symbols_limit)
        self.price_window = int(price_window)
        self.max_symbols_per_drain = int(max_symbols_per_drain)
        self.market_shock_detector = (
            MarketShockDetector(
                window_seconds=market_shock_window_seconds,
                move_bps_threshold=market_shock_move_bps_threshold,
                critical_bps_threshold=market_shock_critical_bps_threshold,
                cooldown_seconds=market_shock_cooldown_seconds,
            )
            if market_shock_detector_enabled
            else None
        )
        self.market_wakeup_callback = market_wakeup_callback
        self.price_snapshot_callback = price_snapshot_callback

        self.latest_by_symbol: dict[str, PriceSnapshot] = {}
        self.price_window_store = MarketPriceWindow(
            maxlen=self.price_window,
            symbols=self.symbols,
        )
        # Backward-compatible inspection view. MarketPriceWindow remains the sole owner.
        self.prices_by_symbol = self.price_window_store.prices_by_symbol
        self.dirty_symbols: set[str] = set()

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_app: Any | None = None
        self._running = False
        self._stopped = False
        self._connected = False
        self._last_message_at: float | None = None
        self._last_error = ""
        self._events_since_heartbeat = 0
        self._dirty_dropped_since_heartbeat = 0
        self._last_heartbeat_at = 0.0
        self.logger = get_logger(__name__)

    @property
    def stream_url(self) -> str:
        streams = "/".join(f"{symbol.lower()}@{self.stream}" for symbol in self.symbols)
        return f"{self.base_url}/stream?streams={quote(streams, safe='@/')}"

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._stopped = False
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                name="alphora-market-data-hub",
                daemon=True,
            )
            self._thread.start()

        self.logger.info(
            "WebSocket publico de mercado iniciado | symbols=%s | stream=%s | endpoint=%s",
            len(self.symbols),
            self.stream,
            self.base_url,
        )

    def stop(self) -> None:
        try:
            with self._lock:
                self._running = False
                self._stopped = True
                self._connected = False
                self._stop_event.set()
                ws_app = self._ws_app
                thread = self._thread

            if ws_app is not None:
                try:
                    ws_app.close()
                except Exception as error:
                    self.logger.warning(
                        "Falha ao fechar WebSocket publico de mercado: %s",
                        error,
                    )

            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=min(2.0, self.reconnect_seconds))
        except Exception as error:
            self.logger.warning("Falha ao parar WebSocket publico de mercado: %s", error)
        finally:
            with self._lock:
                self._running = False
                self._connected = False

    def latest_price(
        self,
        symbol: str,
        now: float | None = None,
    ) -> PriceSnapshot | None:
        normalized = _normalize_symbol(symbol)
        current_time = time.time() if now is None else float(now)
        with self._lock:
            if self._stopped or normalized not in self._enabled_symbols:
                return None
            snapshot = self.latest_by_symbol.get(normalized)
            if snapshot is None:
                return None
            if current_time - snapshot.received_at > self.max_price_age_seconds:
                return None
            return snapshot

    def latest_market_state(
        self,
        symbol: str,
        now: float | None = None,
    ) -> MarketStateSnapshot | None:
        """Return price and rolling history from one atomic read."""

        normalized = _normalize_symbol(symbol)
        current_time = time.time() if now is None else float(now)
        with self._lock:
            if self._stopped or normalized not in self._enabled_symbols:
                return None
            snapshot = self.latest_by_symbol.get(normalized)
            if snapshot is None:
                return None
            if current_time - snapshot.received_at > self.max_price_age_seconds:
                return None
            return MarketStateSnapshot(
                price_snapshot=snapshot,
                prices=self.price_window_store.snapshot(normalized),
            )

    def record_fallback_market_state(
        self,
        symbol: str,
        price: Decimal,
        *,
        observed_at: float,
        source: str,
    ) -> MarketStateSnapshot:
        """Build a fallback view while keeping one shared rolling-price owner."""

        normalized = _normalize_symbol(symbol)
        normalized_price = Decimal(str(price))
        fallback = PriceSnapshot(
            symbol=normalized,
            price=normalized_price,
            bid_price=normalized_price,
            ask_price=normalized_price,
            event_time=None,
            received_at=float(observed_at),
            source=source,
        )
        with self._lock:
            prices = self.price_window_store.record(normalized, normalized_price)
            return MarketStateSnapshot(price_snapshot=fallback, prices=prices)

    def has_recent_price(self, symbol: str, now: float | None = None) -> bool:
        return self.latest_market_state(symbol, now=now) is not None

    def drain_dirty_symbols(self, now: float | None = None) -> list[str]:
        with self._lock:
            symbols = sorted(self.dirty_symbols)[: self.max_symbols_per_drain]
            for symbol in symbols:
                self.dirty_symbols.discard(symbol)
            return symbols

    def health_snapshot(self, now: float | None = None) -> dict[str, Any]:
        current_time = time.time() if now is None else float(now)
        with self._lock:
            return self._health_snapshot_locked(current_time)

    def ingest_message(self, raw: str, *, received_at: float | None = None) -> bool:
        payload = parse_combined_message(raw)
        if payload is None:
            return False

        snapshot = parse_book_ticker_payload(payload, received_at=received_at)
        if snapshot is None:
            return False

        return self._record_snapshot(snapshot)

    def _record_snapshot(self, snapshot: PriceSnapshot) -> bool:
        accepted = False
        with self._lock:
            if snapshot.symbol not in self._enabled_symbols:
                return False

            self.latest_by_symbol[snapshot.symbol] = snapshot
            self.price_window_store.record(snapshot.symbol, snapshot.price)
            self._last_message_at = snapshot.received_at
            self._events_since_heartbeat += 1

            self._record_market_shock_if_needed(snapshot)

            if snapshot.symbol in self.dirty_symbols:
                accepted = True
            elif len(self.dirty_symbols) >= self.dirty_symbols_limit:
                self._dirty_dropped_since_heartbeat += 1
                accepted = True
            else:
                self.dirty_symbols.add(snapshot.symbol)
                accepted = True

        if accepted:
            self._notify_price_snapshot(snapshot)
        return accepted

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
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
                self._record_connection_error(error)
            finally:
                with self._lock:
                    self._connected = False
                    self._ws_app = None

            if self._stop_event.is_set():
                break

            self.logger.warning(
                "WebSocket publico de mercado desconectado; reconectando em %.1fs",
                self.reconnect_seconds,
            )
            self._stop_event.wait(self.reconnect_seconds)

        with self._lock:
            self._running = False
            self._connected = False

    def drain_market_shock_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            detector = self.market_shock_detector
            if detector is None:
                return []

            return detector.drain_events(limit=limit)

    def _record_market_shock_if_needed(self, snapshot: PriceSnapshot) -> None:
        detector = self.market_shock_detector
        if detector is None:
            return

        try:
            event = detector.evaluate_snapshot(snapshot)
            if event is not None:
                self._notify_market_wakeup(event.to_dict())
        except Exception as error:
            self._last_error = f"market_shock_detector: {error}"
            self.logger.warning("Falha no detector de choque de mercado: %s", error)

    def _notify_market_wakeup(self, event: dict[str, Any]) -> None:
        callback = self.market_wakeup_callback
        if callback is None:
            return

        try:
            callback(event)
        except Exception as error:
            self._last_error = f"market_wakeup_callback: {error}"
            self.logger.warning("Falha ao acionar wakeup do Governor: %s", error)

    def _notify_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        callback = self.price_snapshot_callback
        if callback is None:
            return

        try:
            callback(snapshot)
        except Exception as error:
            self._last_error = f"price_snapshot_callback: {error}"
            self.logger.warning("Falha ao publicar snapshot de mercado: %s", error)

    def _on_open(self, _ws_app: Any) -> None:
        with self._lock:
            self._connected = True
            self._last_error = ""
            self._last_heartbeat_at = time.time()
        self.logger.info(
            "WebSocket publico de mercado conectado | symbols=%s | stream=%s",
            len(self.symbols),
            self.stream,
        )

    def _on_message(self, _ws_app: Any, message: str) -> None:
        now = time.time()
        self.ingest_message(message, received_at=now)
        self._maybe_log_heartbeat(now)

    def _on_error(self, _ws_app: Any, error: Exception) -> None:
        self._record_connection_error(error)

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
                "WebSocket publico de mercado fechado | status=%s | motivo=%s",
                status_code,
                message or "",
            )

    def _record_connection_error(self, error: Exception) -> None:
        message = str(error)
        with self._lock:
            self._connected = False
            self._last_error = message
        self.logger.error("Erro no WebSocket publico de mercado: %s", message)

    def _maybe_log_heartbeat(self, now: float) -> None:
        with self._lock:
            if now - self._last_heartbeat_at < self.heartbeat_seconds:
                return
            health = self._health_snapshot_locked(now)
            events = self._events_since_heartbeat
            dirty_dropped = self._dirty_dropped_since_heartbeat
            self._events_since_heartbeat = 0
            self._dirty_dropped_since_heartbeat = 0
            self._last_heartbeat_at = now

        self.logger.info(
            "WebSocket de mercado ativo | governador_escutando=true | symbols=%s | "
            "known_prices=%s | eventos_ultimo_minuto=%s | stale_symbols=%s | "
            "maior_idade_preco=%.1fs | fallback_rest_disponivel=true",
            health["symbols"],
            health["known_prices"],
            events,
            health["stale_symbols"],
            health["largest_price_age_seconds"],
        )
        if dirty_dropped:
            self.logger.warning(
                "WebSocket de mercado atingiu limite de dirty_symbols | limit=%s | "
                "eventos_sem_marcacao=%s",
                self.dirty_symbols_limit,
                dirty_dropped,
            )

    def _health_snapshot_locked(self, now: float) -> dict[str, Any]:
        ages = [max(0.0, now - snapshot.received_at) for snapshot in self.latest_by_symbol.values()]
        stale_symbols = sum(1 for age in ages if age > self.max_price_age_seconds)
        detector = self.market_shock_detector
        pending_shock_events = detector.pending_count if detector is not None else 0

        return {
            "enabled": True,
            "running": self._running,
            "connected": self._connected,
            "symbols": len(self.symbols),
            "known_prices": len(self.latest_by_symbol),
            "stale_symbols": stale_symbols,
            "events_since_heartbeat": self._events_since_heartbeat,
            "largest_price_age_seconds": max(ages, default=0.0),
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
            "market_shock_pending_events": pending_shock_events,
        }


def _normalize_symbols(symbols: list[str] | tuple[str, ...]) -> list[str]:
    seen = set()
    normalized_symbols = []
    for raw_symbol in symbols:
        symbol = _normalize_symbol(raw_symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized_symbols.append(symbol)
    return normalized_symbols


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().strip()


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _event_time_seconds(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    return raw / 1000.0
