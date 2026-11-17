from typing import Any
from urllib.parse import urlparse

REAL_USDM_FUTURES_WS_BASE_URL = "wss://fstream.binance.com"
TESTNET_USDM_FUTURES_WS_BASE_URL = "wss://fstream.binancefuture.com"
REAL_SPOT_WS_BASE_URL = "wss://stream.binance.com:9443"
TESTNET_SPOT_WS_BASE_URL = "wss://stream.testnet.binance.vision"


def resolve_public_market_ws_base_url(config: Any) -> str:
    configured = str(getattr(config, "market_ws_base_url", "") or "").strip()
    if configured:
        return configured.rstrip("/")

    base_url = str(getattr(config, "base_url", "") or "").strip()
    host = urlparse(base_url).netloc.lower()
    environment = getattr(getattr(config, "environment", None), "value", "")
    market_type = str(getattr(config, "market_type", "FUTURES")).upper()

    if host in {"api.binance.com", "testnet.binance.vision"} or market_type == "SPOT":
        if host == "api.binance.com" or (
            host != "testnet.binance.vision" and environment == "REAL"
        ):
            return REAL_SPOT_WS_BASE_URL
        return TESTNET_SPOT_WS_BASE_URL

    if host == "fapi.binance.com" or environment == "REAL":
        return REAL_USDM_FUTURES_WS_BASE_URL

    return TESTNET_USDM_FUTURES_WS_BASE_URL


def resolve_user_data_ws_base_url(config: Any) -> str:
    configured = str(getattr(config, "user_data_stream_ws_base_url", "") or "").strip()
    if configured:
        return configured.rstrip("/")

    return resolve_public_market_ws_base_url(config)
