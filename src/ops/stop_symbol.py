import argparse
import json
from pathlib import Path


def stop_symbol(symbol: str, *, runtime_dir: str = "runtime") -> dict:
    normalized = symbol.lower().strip()
    path = Path(runtime_dir) / f"stop_{normalized}.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()
    print(json.dumps(stop_symbol(args.symbol, runtime_dir=args.runtime_dir), indent=2))


if __name__ == "__main__":
    main()
