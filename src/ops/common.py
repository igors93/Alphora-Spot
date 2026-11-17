import json
import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from binance.futures_client import BinanceFuturesClient
from config import AppConfig


class CheckSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    severity: str = CheckSeverity.INFO.value


def make_client(config: AppConfig, *, mutation_enabled: bool = False) -> BinanceFuturesClient:
    return BinanceFuturesClient(
        base_url=config.base_url,
        api_key=config.api_key,
        api_secret=config.api_secret,
        mutation_enabled=mutation_enabled,
        spot_holdings_path=str(
            Path(config.runtime_dir) / f"spot_holdings_{config.environment_key}.json"
        ),
        bot_order_prefix=config.bot_order_prefix,
    )


def extract_usdt_asset(account_info: dict) -> dict | None:
    for asset in account_info.get("assets", []):
        if asset.get("asset") == "USDT":
            return asset
    return None


def mask_api_key(api_key: str) -> str:
    if not api_key:
        return "<vazia>"
    if len(api_key) <= 10:
        return "***"
    return f"{api_key[:5]}...{api_key[-4:]}"


def robot_prefix(config: AppConfig) -> str:
    return config.bot_order_prefix.upper().strip()


def is_robot_client_id(config: AppConfig, client_id: str) -> bool:
    return client_id.upper().startswith(f"{robot_prefix(config)}_")


def extract_normal_client_order_id(order: dict) -> str:
    return str(order.get("clientOrderId") or order.get("origClientOrderId") or "")


def extract_algo_client_id(order: dict) -> str:
    return str(
        order.get("clientAlgoId") or order.get("clientAlgoID") or order.get("clientOrderId") or ""
    )


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)


def print_result(payload: dict, *, json_only: bool) -> None:
    if json_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return

    print(payload.get("summary", ""))
    for check in payload.get("checks", []):
        marker = "OK" if check.get("ok") else check.get("severity", "error").upper()
        print(f"[{marker}] {check.get('name')}: {check.get('message')}")
    print()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))


def stop_request_path(runtime_dir: str = "runtime") -> Path:
    return Path(runtime_dir) / "stop.request"


def request_stop(runtime_dir: str = "runtime") -> Path:
    path = stop_request_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def governor_stop_request_path(runtime_dir: str = "runtime") -> Path:
    return Path(runtime_dir) / "governor_stop.request"


def request_governor_stop(runtime_dir: str = "runtime") -> Path:
    path = governor_stop_request_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path
