from typing import Any

from utils.logger import get_logger


def safe_append(journal, event_type: str, logger=None, **payload: Any) -> None:
    try:
        journal.append(event_type, **payload)
    except Exception as error:
        active_logger = logger or get_logger(__name__)
        active_logger.warning(
            "Falha ao gravar evento diagnostico %s: %s",
            event_type,
            error,
        )
