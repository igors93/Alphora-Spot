"""Transport for the desktop bridge: stdin/stdout JSON lines served by a worker pool."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any

from desktop.bridge.errors import BridgeError

LOGGER = logging.getLogger("desktop.bridge")

# Requests run concurrently and answers are matched by id. Calls that can take tens of
# seconds (preflight subprocess, Binance downloads, backtest and training preparation,
# model validation) get their own pool, so they can never queue the quick reads the
# screens poll every few seconds behind them until those time out.
_WORKERS = 4
_SLOW_WORKERS = 4
SLOW_METHODS = frozenset(
    {
        "operation.authorize",
        "operation.confirm_preflight",
        "backtest.rules",
        "backtest.download",
        "backtest.run",
        "training.start",
        "intelligence.validate_model",
    }
)
# Requests slower than this are logged to stderr with their method and duration.
_SLOW_REQUEST_SECONDS = 3.0


class JsonLineChannel:
    """Serialized writer for protocol messages.

    Protocol output goes to a private duplicate of the original stdout; the process-wide
    stdout is redirected to stderr so a stray ``print`` from the core can never corrupt
    the stream the interface parses.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, default=_json_default)
        with self._lock:
            self._stream.write(payload)
            self._stream.write("\n")
            self._stream.flush()


def serve(
    dispatch: Callable[[str, dict[str, Any]], Any],
    *,
    reader: IO[str],
    channel: JsonLineChannel,
    ready_payload: dict[str, Any],
) -> int:
    channel.send({"event": "ready", "data": ready_payload})
    with (
        ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="bridge") as pool,
        ThreadPoolExecutor(max_workers=_SLOW_WORKERS, thread_name_prefix="bridge-slow") as slow,
    ):
        for raw_line in reader:
            line = raw_line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Requisição ignorada: JSON inválido")
                continue
            if not isinstance(request, dict) or "id" not in request:
                LOGGER.warning("Requisição ignorada: formato inválido")
                continue
            target = slow if str(request.get("method", "")) in SLOW_METHODS else pool
            target.submit(_handle, dispatch, channel, request)
        pool.shutdown(wait=False, cancel_futures=True)
        slow.shutdown(wait=False, cancel_futures=True)
    return 0


def _handle(
    dispatch: Callable[[str, dict[str, Any]], Any],
    channel: JsonLineChannel,
    request: dict[str, Any],
) -> None:
    request_id = request.get("id")
    method = str(request.get("method", ""))
    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    started = time.perf_counter()
    try:
        result = dispatch(method, params)
        message: dict[str, Any] = {"id": request_id, "ok": True, "result": result}
    except BridgeError as error:
        message = {"id": request_id, "ok": False, "error": error.as_payload()}
    except Exception as error:  # noqa: BLE001 - every failure must reach the interface
        LOGGER.error("Falha em %s\n%s", method, traceback.format_exc())
        message = {
            "id": request_id,
            "ok": False,
            "error": BridgeError("Erro interno", _safe_text(error)).as_payload(),
        }
    elapsed = time.perf_counter() - started
    if elapsed >= _SLOW_REQUEST_SECONDS:
        LOGGER.warning("Slow bridge request: %s took %.1f s", method, elapsed)
    try:
        channel.send(message)
    except (OSError, ValueError):
        # The interface went away; nothing left to answer.
        pass


def main() -> int:
    protocol_stream = _detach_protocol_stdout()
    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from desktop.bridge.handlers import Bridge  # noqa: PLC0415

    bridge = Bridge.from_environment()
    channel = JsonLineChannel(protocol_stream)
    return serve(
        bridge.dispatch,
        reader=sys.stdin,
        channel=channel,
        ready_payload=bridge.hello(),
    )


def _detach_protocol_stdout() -> IO[str]:
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", newline="\n", buffering=1)


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "__fspath__"):
        return os.fspath(value)
    return str(value)


def _safe_text(value: Any) -> str:
    try:
        from utils.privacy import sanitize_for_audit  # noqa: PLC0415

        text = str(sanitize_for_audit(str(value))).strip()
    except Exception:  # noqa: BLE001
        return "[SANITIZATION_ERROR]"
    return text or value.__class__.__name__
