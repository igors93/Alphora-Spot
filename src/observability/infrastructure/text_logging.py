"""Centralized text log setup for observability runs."""

from __future__ import annotations

import logging
from pathlib import Path

from utils.logprivacy_adapter import install_privacy_filter

_INSTALLED_RUN_DIRS: set[Path] = set()
_LOCK = __import__("threading").Lock()


def configure_text_logging(
    run_dir: Path,
    *,
    run_id: str,
    legacy_enabled: bool,
) -> None:
    """Set up alphora.log, warnings.log, errors.log in run_dir/logs/.

    Preserves run_id in formatter. Avoids installing duplicate handlers
    if called more than once with the same run_dir.
    """
    if not legacy_enabled:
        return

    resolved = run_dir.resolve()
    with _LOCK:
        if resolved in _INSTALLED_RUN_DIRS:
            return

        logs_dir = run_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        formatter = logging.Formatter(
            f"%(asctime)s | %(levelname)s | run_id={run_id} | %(name)s | %(message)s"
        )

        root_logger = logging.getLogger()
        added_handlers: list[logging.Handler] = []

        try:
            for filename, level in (
                ("alphora.log", logging.INFO),
                ("warnings.log", logging.WARNING),
                ("errors.log", logging.ERROR),
            ):
                path = logs_dir / filename
                # Avoid duplicate handlers
                path_str = str(path.resolve())
                already_installed = any(
                    isinstance(h, logging.FileHandler) and h.baseFilename == path_str
                    for h in root_logger.handlers
                )
                if already_installed:
                    continue

                handler = logging.FileHandler(path, encoding="utf-8")
                handler.setLevel(level)
                handler.setFormatter(formatter)
                root_logger.addHandler(handler)
                added_handlers.append(handler)

            install_privacy_filter(root_logger, required=True)
        except Exception:
            for added_handler in added_handlers:
                root_logger.removeHandler(added_handler)
                try:
                    added_handler.close()
                except Exception:
                    pass
            raise

        _INSTALLED_RUN_DIRS.add(resolved)


def reset_text_logging_registry() -> None:
    """Remove all tracked run dirs (for testing only)."""
    with _LOCK:
        _INSTALLED_RUN_DIRS.clear()
