import argparse
import json
from pathlib import Path


def resume_symbol(symbol: str, *, runtime_dir: str = "runtime") -> dict:
    normalized = symbol.lower().strip()
    path = Path(runtime_dir) / f"resume_{normalized}.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("resume", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()
    print(json.dumps(resume_symbol(args.symbol, runtime_dir=args.runtime_dir), indent=2))


if __name__ == "__main__":
    main()
