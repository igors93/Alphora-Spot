import argparse
import json
from pathlib import Path


def pause_symbol(symbol: str, *, runtime_dir: str = "runtime") -> dict:
    return _write_request("pause", symbol, runtime_dir=runtime_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()
    print(json.dumps(pause_symbol(args.symbol, runtime_dir=args.runtime_dir), indent=2))


def _write_request(action: str, symbol: str, *, runtime_dir: str) -> dict:
    normalized = symbol.lower().strip()
    path = Path(runtime_dir) / f"{action}_{normalized}.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(action, encoding="utf-8")
    return {"ok": True, "path": str(path)}


if __name__ == "__main__":
    main()
