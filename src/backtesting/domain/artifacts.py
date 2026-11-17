from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MANIFEST_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class BacktestManifest:
    schema_version: int
    run_id: str
    status: str
    started_at: str
    finished_at: str | None
    request_path: str | None
    summary_json: str | None
    trades_csv: str | None
    decisions_jsonl: str | None
    rejected_jsonl: str | None
    diagnostics_json: str | None = None
    error: str | None = None
    equity_csv: str | None = None
    request_hash: str | None = None
    config_hash: str | None = None
    git_commit: str | None = None
    git_dirty: bool = False
    source_tree_sha256: str | None = None
    config: dict[str, Any] | None = None
    input_files: dict[str, Any] | None = None
    model_files: dict[str, Any] | None = None
    output_files: dict[str, Any] | None = None
    python_version: str | None = None
    platform: str | None = None
    dependencies: dict[str, str] | None = None
    random_seed: int | None = None
