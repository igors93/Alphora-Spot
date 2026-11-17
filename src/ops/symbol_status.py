import argparse
import json
from pathlib import Path


def symbol_status(symbol: str | None = None, *, runtime_dir: str = "runtime") -> dict:
    path = Path(runtime_dir) / "governor_state.json"
    if not path.exists():
        return {"ok": False, "message": "governor_state.json nao encontrado"}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"governor_state.json corrompido: {exc}"}
    symbols = payload.get("symbols", {})
    if symbol is None:
        return {"ok": True, "symbols": symbols}
    normalized = symbol.upper().strip()
    if normalized not in symbols:
        return {"ok": False, "message": f"Simbolo nao encontrado no governor_state: {normalized}"}
    return {"ok": True, "symbol": normalized, "state": symbols[normalized]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()
    selected = None if args.all or not args.symbol else args.symbol
    print(
        json.dumps(
            symbol_status(selected, runtime_dir=args.runtime_dir), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
