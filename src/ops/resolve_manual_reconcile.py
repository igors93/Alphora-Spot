import argparse
import json

from config import load_config
from futures.operational import clear_manual_reconcile_required, manual_reconcile_path
from ops.common import json_default

CONFIRM_RESOLVED_MANUALLY = "RESOLVED_MANUALLY"


def resolve_manual_reconcile(*, symbol: str, confirm: str = "") -> dict:
    config = load_config()
    normalized = symbol.upper().strip() or config.symbol
    path = manual_reconcile_path(config.runtime_dir, normalized)
    if confirm != CONFIRM_RESOLVED_MANUALLY:
        return {
            "ok": False,
            "symbol": normalized,
            "path": str(path),
            "required_confirmation": CONFIRM_RESOLVED_MANUALLY,
            "message": "Nenhum arquivo foi alterado sem confirmacao textual.",
        }

    removed = clear_manual_reconcile_required(config.runtime_dir, normalized)
    return {
        "ok": True,
        "symbol": normalized,
        "path": str(path),
        "removed": removed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    print(
        json.dumps(
            resolve_manual_reconcile(symbol=args.symbol, confirm=args.confirm),
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )
    )


if __name__ == "__main__":
    main()
