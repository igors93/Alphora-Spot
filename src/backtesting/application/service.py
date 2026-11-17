from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backtesting.application.config_factory import BacktestConfigFactory
from backtesting.domain.config import BacktestRunResult
from backtesting.domain.requests import BacktestRequest, request_hash
from backtesting.engine.engine import run_backtest
from backtesting.engine.robustness import run_robust_backtest
from backtesting.infrastructure.report_writer import build_reproducibility_metadata


class BacktestApplicationService:
    def __init__(self, config_factory: BacktestConfigFactory | None = None):
        self.config_factory = config_factory or BacktestConfigFactory()

    def run(
        self,
        request: BacktestRequest,
        *,
        write_reports: bool = True,
        request_path: str | None = None,
    ) -> BacktestRunResult:
        config = self.config_factory.from_request(request)
        result = run_backtest(config, write_reports=write_reports)
        if result.paths is not None and result.paths.manifest_json is not None:
            _touch_manifest_request_path(
                result.paths.manifest_json,
                request_path=request_path,
                resolved_request_hash=request_hash(request),
            )
        return result

    def run_robust(self, request: BacktestRequest, *, write_reports: bool = False) -> dict:
        config = self.config_factory.from_request(request)
        payload = run_robust_backtest(config, write_reports=write_reports)
        payload.update(
            {
                "run_id": request.run_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "request_hash": request_hash(request),
                "request": asdict(request),
                "config": asdict(config),
                "reproducibility": build_reproducibility_metadata(config),
            }
        )
        return payload


def _touch_manifest_request_path(
    manifest_path: Path,
    *,
    request_path: str | None,
    resolved_request_hash: str,
) -> None:
    with manifest_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if request_path:
        payload["request_path"] = str(Path(request_path).resolve())
    payload["request_hash"] = resolved_request_hash
    tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
        file.write("\n")
    tmp.replace(manifest_path)
