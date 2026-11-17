import argparse
from pathlib import Path

from config.env_file import load_env_file
from domain.subprocess_protocol import encode_task_event
from research.ai.probabilistic_training_service import ProbabilisticAITrainingService
from research.ai.registry import AI_REGISTRY_PATH
from research.ai.training_defaults import (
    DEFAULT_HORIZON_CANDLES,
    DEFAULT_MIN_TRAINING_ROWS,
    DEFAULT_THRESHOLD_BPS,
)
from research.binance_funding_downloader import download_funding_to_csv
from research.binance_klines_downloader import download_klines_to_csv


def _ai_progress(message: str) -> None:
    print(encode_task_event("ai_progress", message=message), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m research.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_download_klines_parser(subparsers)
    _add_download_funding_parser(subparsers)
    _add_train_ai_probabilistic_global_parser(subparsers)
    _add_train_ai_probabilistic_refined_parser(subparsers)
    _add_train_ai_probabilistic_all_parser(subparsers)
    return parser


def main() -> None:
    load_env_file()
    args = build_parser().parse_args()

    if args.command == "download-klines":
        run_download_klines(args)
        return

    if args.command == "download-funding":
        output_path = download_funding_to_csv(
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            output_path=args.output,
        )
        print(f"Funding baixado: {output_path}")
        return

    if args.command == "train-ai-probabilistic-global":
        run_train_ai_probabilistic_global(args)
        return

    if args.command == "train-ai-probabilistic-refined":
        run_train_ai_probabilistic_refined(args)
        return

    if args.command == "train-ai-probabilistic-all":
        run_train_ai_probabilistic_all(args)
        return

    raise ValueError(f"Comando nao tratado: {args.command}")


def run_download_klines(args: argparse.Namespace) -> None:
    output_path = download_klines_to_csv(
        symbol=args.symbol,
        interval=args.interval,
        start=args.start,
        end=args.end,
        output_path=args.output,
    )

    print(f"Candles baixados: {output_path}")


def _add_download_funding_parser(subparsers) -> None:
    parser = subparsers.add_parser("download-funding")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output")


def run_train_ai_probabilistic_global(args: argparse.Namespace) -> None:
    symbols = _parse_symbols(args.symbols)
    service = ProbabilisticAITrainingService(progress_callback=_ai_progress)
    _ai_progress(
        "Treino IA probabilistica global iniciado | "
        f"symbols={','.join(symbols)} | interval={args.interval}"
    )
    result = service.train_global(
        symbols=symbols,
        interval=args.interval,
        start=args.start,
        end=args.end,
        csv_by_symbol=_parse_csv_map(args.csv_map),
        horizon_candles=args.horizon_candles,
        threshold_bps=args.threshold_bps,
        min_training_rows=args.min_training_rows,
        output_path=args.output,
        output_dir=args.output_dir,
    )
    _print_probabilistic_training_result(result)


def run_train_ai_probabilistic_refined(args: argparse.Namespace) -> None:
    symbols = _parse_symbols(args.symbols or args.symbol)
    service = ProbabilisticAITrainingService(progress_callback=_ai_progress)
    _ai_progress(
        "Treino IA probabilistica refinada iniciado | "
        f"symbols={','.join(symbols)} | interval={args.interval}"
    )
    result = service.train_refined(
        symbols=symbols,
        interval=args.interval,
        global_model_path=args.global_model,
        start=args.start,
        end=args.end,
        csv_by_symbol=_parse_csv_map(args.csv_map),
        horizon_candles=args.horizon_candles,
        threshold_bps=args.threshold_bps,
        min_training_rows=args.min_training_rows,
        output_dir=args.output_dir,
    )
    _print_probabilistic_training_result(result)


def run_train_ai_probabilistic_all(args: argparse.Namespace) -> None:
    symbols = _parse_symbols(args.symbols)
    service = ProbabilisticAITrainingService(progress_callback=_ai_progress)
    _ai_progress(
        "Treino IA probabilistica completo iniciado | "
        f"symbols={','.join(symbols)} | interval={args.interval}"
    )
    result = service.train_all(
        symbols=symbols,
        interval=args.interval,
        start=args.start,
        end=args.end,
        csv_by_symbol=_parse_csv_map(args.csv_map),
        horizon_candles=args.horizon_candles,
        threshold_bps=args.threshold_bps,
        min_training_rows=args.min_training_rows,
        global_output_path=args.output,
        global_output_dir=args.global_output_dir,
        refined_output_dir=args.output_dir,
    )
    _print_probabilistic_training_result(result)


def _parse_symbols(value: str) -> list[str]:
    symbols = [
        item.upper().strip()
        for item in str(value or "").replace(";", ",").split(",")
        if item.strip()
    ]
    if not symbols:
        raise ValueError("Informe ao menos um simbolo")
    return symbols


def _parse_csv_map(value: str | None) -> dict[str, str | Path]:
    if not value:
        return {}

    result: dict[str, str | Path] = {}
    for item in str(value).split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError("csv-map precisa usar formato SYMBOL=caminho.csv")
        symbol, path = item.split("=", 1)
        normalized_symbol = symbol.upper().strip()
        normalized_path = path.strip()
        if not normalized_symbol or not normalized_path:
            raise ValueError("csv-map contem simbolo ou caminho vazio")
        result[normalized_symbol] = normalized_path

    return result


def _print_probabilistic_training_result(result: dict) -> None:
    global_model = result.get("global_model")
    if isinstance(global_model, dict):
        print(encode_task_event("ai_model", **global_model), flush=True)

    symbol_models = result.get("symbol_models")
    if isinstance(symbol_models, dict):
        for symbol, payload in symbol_models.items():
            if not isinstance(payload, dict):
                continue
            print(encode_task_event("ai_symbol_model", symbol=symbol, **payload), flush=True)

    warnings = result.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            print(encode_task_event("ai_warning", message=str(warning)), flush=True)

    print(encode_task_event("ai_registry", path=str(AI_REGISTRY_PATH)), flush=True)


def _add_download_klines_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("download-klines")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output")


def _add_train_ai_probabilistic_global_parser(
    subparsers: argparse._SubParsersAction,
) -> None:
    parser = subparsers.add_parser("train-ai-probabilistic-global")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--csv-map")
    parser.add_argument("--output")
    parser.add_argument("--output-dir")
    parser.add_argument("--horizon-candles", type=int, default=DEFAULT_HORIZON_CANDLES)
    parser.add_argument("--threshold-bps", type=float, default=DEFAULT_THRESHOLD_BPS)
    parser.add_argument("--min-training-rows", type=int, default=DEFAULT_MIN_TRAINING_ROWS)


def _add_train_ai_probabilistic_refined_parser(
    subparsers: argparse._SubParsersAction,
) -> None:
    parser = subparsers.add_parser("train-ai-probabilistic-refined")
    parser.add_argument("--symbol")
    parser.add_argument("--symbols")
    parser.add_argument("--interval", required=True)
    parser.add_argument("--global-model", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--csv-map")
    parser.add_argument("--output-dir")
    parser.add_argument("--horizon-candles", type=int, default=DEFAULT_HORIZON_CANDLES)
    parser.add_argument("--threshold-bps", type=float, default=DEFAULT_THRESHOLD_BPS)
    parser.add_argument("--min-training-rows", type=int, default=DEFAULT_MIN_TRAINING_ROWS)


def _add_train_ai_probabilistic_all_parser(
    subparsers: argparse._SubParsersAction,
) -> None:
    parser = subparsers.add_parser("train-ai-probabilistic-all")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--csv-map")
    parser.add_argument("--output")
    parser.add_argument("--global-output-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--horizon-candles", type=int, default=DEFAULT_HORIZON_CANDLES)
    parser.add_argument("--threshold-bps", type=float, default=DEFAULT_THRESHOLD_BPS)
    parser.add_argument("--min-training-rows", type=int, default=DEFAULT_MIN_TRAINING_ROWS)


if __name__ == "__main__":
    main()
