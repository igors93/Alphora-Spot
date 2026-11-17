from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backtesting.domain.artifacts import BacktestManifest


def write_manifest(path: Path, manifest: BacktestManifest) -> None:
    write_json_atomic(path, asdict(manifest))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
        file.write("\n")
    tmp.replace(path)
