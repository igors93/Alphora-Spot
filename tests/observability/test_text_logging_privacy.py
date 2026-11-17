from __future__ import annotations

import logging
from types import SimpleNamespace

from observability.bootstrap import initialize_observability, shutdown_observability
from observability.config import ObservabilitySettings
from utils.run_context import initialize_run_context


def test_text_logs_are_filtered_by_logprivacy(tmp_path):
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        legacy_text_log_enabled=True,
        crash_hooks_enabled=False,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1.0,
    )
    initialize_run_context(run_id="privacy-run")
    initialize_observability(SimpleNamespace(observability=settings))

    logger = logging.getLogger("tests.observability.privacy")
    logger.warning("warning api_secret=MY_REAL_SECRET")
    logger.error("error api_secret=MY_REAL_SECRET")
    for handler in logging.getLogger().handlers:
        handler.flush()
    shutdown_observability(status="COMPLETED")

    logs_dir = tmp_path / "privacy-run" / "logs"
    for filename in ("alphora.log", "warnings.log", "errors.log"):
        content = (logs_dir / filename).read_text(encoding="utf-8")
        assert "MY_REAL_SECRET" not in content
