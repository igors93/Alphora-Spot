from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtesting.application import BacktestApplicationService
from backtesting.domain.requests import request_from_json
from domain.subprocess_protocol import encode_task_event


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backtesting.adapters.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--request", required=True)
    robust_parser = subparsers.add_parser("robust")
    robust_parser.add_argument("--request", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request = _load_request(Path(args.request))
    if args.command == "validate":
        print(encode_task_event("backtest_request_valid"), flush=True)
        return

    if args.command == "robust":
        payload = BacktestApplicationService().run_robust(request)
        output_path = Path(request.output.output_dir) / f"robust_{request.run_id}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
            file.write("\n")
        print(encode_task_event("robust_backtest_report", path=str(output_path)), flush=True)
        return

    result = BacktestApplicationService().run(request, request_path=str(args.request))
    if result.paths is not None and result.paths.manifest_json is not None:
        print(
            encode_task_event("backtest_manifest", path=str(result.paths.manifest_json)),
            flush=True,
        )


def _load_request(path: Path):
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return request_from_json(payload)


if __name__ == "__main__":
    main()
