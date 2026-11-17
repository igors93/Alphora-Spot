import argparse
import json
from pathlib import Path


def governor_status(*, runtime_dir: str = "runtime") -> dict:
    path = Path(runtime_dir) / "governor_state.json"
    if not path.exists():
        return {"ok": False, "message": "governor_state.json nao encontrado"}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"governor_state.json corrompido: {exc}"}
    return {"ok": True, "state": payload}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()
    print(json.dumps(governor_status(runtime_dir=args.runtime_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
