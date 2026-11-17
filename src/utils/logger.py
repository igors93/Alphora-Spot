import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from utils.run_context import get_run_id

_LOGGING_CONFIGURED = False
_RUN_LOG_HANDLER_PATHS: set[Path] = set()
_LEGACY_FILE_HANDLER: logging.FileHandler | None = None


class RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = get_run_id()
        return True


def configure_logging() -> None:
    global _LOGGING_CONFIGURED, _LEGACY_FILE_HANDLER

    if _LOGGING_CONFIGURED:
        return

    legacy_log_path = _log_path("alphora")
    legacy_handler = logging.FileHandler(legacy_log_path, encoding="utf-8")
    _LEGACY_FILE_HANDLER = legacy_handler
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | run_id=%(run_id)s | %(name)s | %(message)s",
        handlers=[
            _handler_with_run_id(logging.StreamHandler(sys.stdout)),
            _handler_with_run_id(legacy_handler),
        ],
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in root_logger.handlers:
        _handler_with_run_id(handler)
    _ensure_legacy_file_handler(root_logger, legacy_log_path)

    _apply_privacy_filter(root_logger)

    _LOGGING_CONFIGURED = True
    logging.getLogger(__name__).info("Logging iniciado | run_id=%s", get_run_id())


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def configure_runtime_file_logging(run_dir: str | Path, *, enabled: bool = True) -> None:
    if not enabled:
        return

    configure_logging()
    logs_dir = Path(run_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | run_id=%(run_id)s | %(name)s | %(message)s"
    )
    for filename, level in (
        ("alphora.log", logging.INFO),
        ("warnings.log", logging.WARNING),
        ("errors.log", logging.ERROR),
    ):
        path = (logs_dir / filename).resolve()
        if path in _RUN_LOG_HANDLER_PATHS or _has_file_handler(logging.getLogger(), path):
            _RUN_LOG_HANDLER_PATHS.add(path)
            continue
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        _handler_with_run_id(handler)
        logging.getLogger().addHandler(handler)
        _RUN_LOG_HANDLER_PATHS.add(path)

    # Refresh privacy filters to cover the newly added handlers.
    _apply_privacy_filter(logging.getLogger())


def remove_runtime_file_handlers() -> None:
    """Remove all run-specific and legacy file handlers from the root logger.

    Called by audit_logger.reset_for_testing() so that log records produced
    inside one test are not written to a previous test's run directory or to
    the shared daily legacy log file.
    Must only be called from test code.
    """
    global _LEGACY_FILE_HANDLER

    root = logging.getLogger()
    for handler in list(root.handlers):
        if not isinstance(handler, logging.FileHandler):
            continue
        resolved = Path(handler.baseFilename).resolve()
        # Remove run-specific handlers (installed by configure_runtime_file_logging).
        if resolved in _RUN_LOG_HANDLER_PATHS:
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
            _RUN_LOG_HANDLER_PATHS.discard(resolved)
            continue
        # Remove the shared legacy daily log handler so test errors don't
        # bleed into logs/alphora_YYYY-MM-DD.log.
        if _LEGACY_FILE_HANDLER is not None and handler is _LEGACY_FILE_HANDLER:
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
            _LEGACY_FILE_HANDLER = None


def _log_path(prefix: str) -> Path:
    Path("logs").mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path("logs") / f"{prefix}_{day}.log"


def _handler_with_run_id(handler: logging.Handler) -> logging.Handler:
    handler.addFilter(RunIdFilter())
    return handler


def _ensure_legacy_file_handler(root_logger: logging.Logger, path: Path) -> None:
    if _has_file_handler(root_logger, path):
        return

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | run_id=%(run_id)s | %(name)s | %(message)s"
        )
    )
    root_logger.addHandler(_handler_with_run_id(handler))


def _has_file_handler(root_logger: logging.Logger, path: Path) -> bool:
    resolved = str(path.resolve())
    return any(
        isinstance(handler, logging.FileHandler) and handler.baseFilename == resolved
        for handler in root_logger.handlers
    )


def _apply_privacy_filter(logger: logging.Logger, *, required: bool = True) -> None:
    from utils.logprivacy_adapter import PrivacyBootFailure, install_privacy_filter

    try:
        install_privacy_filter(logger, required=required)
    except PrivacyBootFailure:
        raise
    except Exception as error:
        if required:
            raise PrivacyBootFailure(
                f"Erro inesperado ao instalar proteção de privacidade: {type(error).__name__}"
            ) from error
