"""Per-symbol, per-environment paths for financial state and trade journals."""

import os
from pathlib import Path

from config.env_readers import _get_env


def financial_path_key(environment_key: str, execution_mode: str) -> str:
    """Keep non-live observations separate from exchange-backed positions."""
    environment = environment_key.lower()
    mode = execution_mode.upper()
    if mode in {"TESTNET_LIVE", "REAL_LIVE"}:
        return environment
    return f"{environment}_{mode.lower()}"


def default_state_path(runtime_dir: str, symbol: str, environment_key: str) -> str:
    return (Path(runtime_dir) / f"state_{symbol.lower()}_{environment_key.lower()}.json").as_posix()


def default_journal_path(symbol: str, environment_key: str) -> str:
    return (Path("data") / f"trades_{symbol.lower()}_{environment_key.lower()}.jsonl").as_posix()


def resolve_financial_paths(runtime_dir: str, symbol: str, environment_key: str) -> tuple[str, str]:
    reject_unmigrated_shared_paths(runtime_dir)
    return (
        _get_env("ALPHORA_STATE_PATH", default_state_path(runtime_dir, symbol, environment_key)),
        _get_env("ALPHORA_TRADE_JOURNAL_PATH", default_journal_path(symbol, environment_key)),
    )


def reject_unmigrated_shared_paths(runtime_dir: str) -> None:
    """A legacy file has no environment marker, so assigning it automatically is unsafe."""
    old_state = Path(runtime_dir) / "state.json"
    old_journal = Path("data") / "trades.jsonl"
    configured_state = os.environ.get("ALPHORA_STATE_PATH")
    configured_journal = os.environ.get("ALPHORA_TRADE_JOURNAL_PATH")
    if (configured_state is None and old_state.exists()) or (
        configured_state is not None
        and (
            Path(configured_state).name.casefold() == "state.json"
            or Path(configured_state).resolve() == old_state.resolve()
        )
    ):
        raise ValueError(
            f"Legacy shared state at {old_state} has no environment. "
            "Migrate it to a symbol/environment path before starting."
        )
    if (configured_journal is None and old_journal.exists()) or (
        configured_journal is not None
        and (
            Path(configured_journal).name.casefold() == "trades.jsonl"
            or Path(configured_journal).resolve() == old_journal.resolve()
        )
    ):
        raise ValueError(
            f"Legacy shared trade journal at {old_journal} has no environment. "
            "Migrate it to a symbol/environment path before starting."
        )
