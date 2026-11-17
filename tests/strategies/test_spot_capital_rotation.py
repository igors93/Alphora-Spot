from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from binance.futures_client import BinanceFuturesClient, BinanceSpotClient
from config import ExecutionMode
from config.symbol_context import (
    PortfolioRiskSettings,
    resolve_governor_symbol_configs,
)
from domain.candle import Candle
from domain.models import (
    FuturesPosition,
    MarketSnapshot,
    PositionDirection,
    TradeAction,
)
from futures.exchange_rules import extract_symbol_rules
from governor.alphora_governor import AlphoraGovernor
from strategies.futures.fixed.spot_capital_rotation import SpotCapitalRotationStrategy


def _make_candles(
    symbol: str,
    closes: list[float],
    volumes: list[float] | None = None,
    timeframe: str = "15m",
) -> dict[str, tuple[Candle, ...]]:
    vols = volumes or [1000.0] * len(closes)
    candles: list[Candle] = []
    base_ms = 1_700_000_000_000
    step_ms = 900_000
    for idx, close_val in enumerate(closes):
        open_val = closes[idx - 1] if idx > 0 else close_val * 0.998
        high_val = max(open_val, close_val) * 1.001
        low_val = min(open_val, close_val) * 0.999
        candles.append(
            Candle(
                symbol=symbol,
                interval=timeframe,
                open_time_ms=base_ms + idx * step_ms,
                close_time_ms=base_ms + (idx + 1) * step_ms - 1,
                open=Decimal(f"{open_val:.4f}"),
                high=Decimal(f"{high_val:.4f}"),
                low=Decimal(f"{low_val:.4f}"),
                close=Decimal(f"{close_val:.4f}"),
                volume=Decimal(f"{vols[idx]:.2f}"),
                is_closed=True,
            )
        )
    return {timeframe: tuple(candles)}


def _snapshot(
    symbol: str,
    closes: list[float],
    volumes: list[float] | None = None,
) -> MarketSnapshot:
    candles = _make_candles(symbol, closes, volumes)
    return MarketSnapshot(
        symbol=symbol,
        price=Decimal(f"{closes[-1]:.4f}"),
        prices=[Decimal(f"{c:.4f}") for c in closes],
        candles=candles,
    )


def _flat_position(symbol: str) -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _long_position(symbol: str, entry: float, mark: float, qty: float = 0.01) -> FuturesPosition:
    pnl = (mark - entry) * qty
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal(str(qty)),
        entry_price=Decimal(str(entry)),
        unrealized_pnl=Decimal(f"{pnl:.4f}"),
    )


def test_monitors_multiple_coins_and_picks_strongest_opportunity():
    """Requirement 1, 2 & 5: Monitor BTC, ETH, SOL, BNB, XRP simultaneously and buy the strongest."""
    strategy = SpotCapitalRotationStrategy(min_entry_score=65, rotation_score_delta=10)
    shared_board: dict[str, dict[str, Any]] = {}

    # BTC: moderate steady uptrend
    btc_closes = [50000.0 + i * 35.0 for i in range(40)]
    # ETH: flat/choppy
    eth_closes = [2500.0 + ((-1) ** i) * 3.0 for i in range(40)]
    # SOL: strong accelerating breakout with expanding volume
    sol_closes = [100.0 + (i * 0.4) + (max(0, i - 30) ** 1.65) * 0.45 for i in range(40)]
    sol_volumes = [1000.0] * 30 + [2200.0 + i * 250.0 for i in range(10)]
    # BNB: mild drift
    bnb_closes = [300.0 + i * 0.15 for i in range(40)]
    # XRP: slight downtrend
    xrp_closes = [0.65 - i * 0.001 for i in range(40)]

    markets = {
        "BTCUSDT": _snapshot("BTCUSDT", btc_closes),
        "ETHUSDT": _snapshot("ETHUSDT", eth_closes),
        "SOLUSDT": _snapshot("SOLUSDT", sol_closes, sol_volumes),
        "BNBUSDT": _snapshot("BNBUSDT", bnb_closes),
        "XRPUSDT": _snapshot("XRPUSDT", xrp_closes),
    }

    states = {sym: {"rotation_board": shared_board} for sym in markets}
    scores = {
        sym: strategy.score_symbol(sym, mkt, _flat_position(sym), states[sym])
        for sym, mkt in markets.items()
    }

    assert set(shared_board.keys()) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}
    assert scores["SOLUSDT"].score > scores["BTCUSDT"].score
    assert scores["SOLUSDT"].score > scores["ETHUSDT"].score

    decisions = {
        sym: strategy.decide_symbol(sym, mkt, _flat_position(sym), states[sym])
        for sym, mkt in markets.items()
    }
    assert decisions["SOLUSDT"].action is TradeAction.OPEN_LONG
    assert decisions["SOLUSDT"].reason == "STRONGEST_ROTATION_OPPORTUNITY_BUY"
    for other in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT"):
        assert decisions[other].action is TradeAction.HOLD


def test_detects_trend_weakening_while_btc_is_still_rising():
    """Requirement 3 & 4: Sell profitable BTC position when trend weakens while BTC is STILL rising."""
    strategy = SpotCapitalRotationStrategy(
        min_profit_pct_for_early_exit=0.30,
        min_weakening_signals=2,
    )
    board: dict[str, dict[str, Any]] = {}
    state: dict[str, Any] = {"rotation_board": board}

    # Build a strong rally from 60,000 to 63,500, then the last 3 candles KEEP RISING
    # (63,500 -> 63,680 -> 63,760 -> 63,800) but with clear momentum deceleration & volume fade
    closes: list[float] = []
    volumes: list[float] = []
    price = 60000.0
    for i in range(35):
        price += 80.0 + (i * 4.0)  # accelerating rise
        closes.append(price)
        volumes.append(1500.0 + i * 40.0)

    # Last 3 steps: still rising (every close > previous close), but decelerating sharply & volume fading
    closes.extend([price + 90.0, price + 130.0, price + 148.0])
    volumes.extend([1100.0, 850.0, 620.0])

    # Confirm BTC is still rising in the last candles
    assert closes[-1] > closes[-2] > closes[-3] > closes[-4]

    btc_market = _snapshot("BTCUSDT", closes, volumes)
    btc_pos = _long_position("BTCUSDT", entry=60500.0, mark=closes[-1], qty=0.01)

    score = strategy.score_symbol("BTCUSDT", btc_market, btc_pos, state)
    decision = strategy.decide_symbol("BTCUSDT", btc_market, btc_pos, state)

    assert score.metadata["price_still_rising"] is True
    assert score.metadata["trend_weakening_while_rising"] is True
    assert decision.action is TradeAction.CLOSE_POSITION
    assert "EARLY_PROFIT_LOCK_TREND_WEAKENING_WHILE_RISING" in decision.reason


def test_stays_in_usdt_when_no_coin_has_good_opportunity():
    """Requirement 6: Remain in USDT when no monitored coin has a strong buy signal."""
    strategy = SpotCapitalRotationStrategy(min_entry_score=68)
    board: dict[str, dict[str, Any]] = {}

    markets = {
        "BTCUSDT": _snapshot("BTCUSDT", [60000.0 - i * 25.0 for i in range(35)]),
        "ETHUSDT": _snapshot("ETHUSDT", [3000.0 - i * 2.0 for i in range(35)]),
        "SOLUSDT": _snapshot("SOLUSDT", [140.0 + ((-1) ** i) * 0.2 for i in range(35)]),
        "BNBUSDT": _snapshot("BNBUSDT", [550.0 - i * 0.5 for i in range(35)]),
        "XRPUSDT": _snapshot("XRPUSDT", [0.60 - i * 0.0008 for i in range(35)]),
    }
    states = {sym: {"rotation_board": board} for sym in markets}

    for sym, mkt in markets.items():
        strategy.score_symbol(sym, mkt, _flat_position(sym), states[sym])

    for sym, mkt in markets.items():
        decision = strategy.decide_symbol(sym, mkt, _flat_position(sym), states[sym])
        assert decision.action is TradeAction.HOLD
        assert "STAY_IN_USDT" in decision.reason


def test_rotates_from_open_coin_to_stronger_coin_in_same_governor_cycle(config_factory, tmp_path):
    """Requirement 4 & 5: Close a weaker open coin and rotate into the stronger coin in 1 cycle."""
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    base_config = replace(
        config_factory(
            execution_mode=ExecutionMode.PAPER_LIVE,
            symbols=symbols,
            position_notional_usdt=100.0,
        ),
        strategy_module="strategies.futures.fixed.spot_capital_rotation",
        strategy_class="SpotCapitalRotationStrategy",
        strategy_params={
            "min_entry_score": 65,
            "rotation_score_delta": 10,
            "min_profit_pct_for_early_exit": 0.30,
        },
        multi_symbol_enabled=True,
        symbol_profiles_path=str(tmp_path / "symbols.json"),
        portfolio_risk_path=str(tmp_path / "portfolio.json"),
        market_ws_enabled=False,
        user_data_stream_enabled=False,
        economic_gate_enabled=False,
        market_entry_require_economic_gate=False,
        pre_dispatch_price_check_enabled=False,
        trend_context_enabled=False,
    )

    # BTC is currently open but flattening/decelerating; SOL has a strong breakout
    btc_closes = [50000.0 + i * 20.0 for i in range(32)] + [50645.0, 50648.0, 50649.0]
    btc_vols = [1200.0] * 32 + [900.0, 700.0, 500.0]
    eth_closes = [2500.0 + ((-1) ** i) * 1.5 for i in range(35)]
    sol_closes = [100.0 + i * 0.5 + (max(0, i - 25) ** 1.7) * 0.5 for i in range(35)]
    sol_vols = [1000.0] * 25 + [2500.0 + i * 300.0 for i in range(10)]

    price_series = {
        "BTCUSDT": (btc_closes, btc_vols),
        "ETHUSDT": (eth_closes, [1000.0] * len(eth_closes)),
        "SOLUSDT": (sol_closes, sol_vols),
    }

    class FakeSpotRotationClient:
        spot_mode = True

        def exchange_info(self, *, timeout=None) -> dict[str, Any]:
            return {
                "symbols": [
                    {
                        "symbol": sym,
                        "status": "TRADING",
                        "filters": [
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                            },
                            {
                                "filterType": "NOTIONAL",
                                "minNotional": "5",
                            },
                            {
                                "filterType": "PRICE_FILTER",
                                "tickSize": "0.01",
                            },
                        ],
                    }
                    for sym in symbols
                ]
            }

        def ticker_price(self, symbol: str, *, timeout=None) -> dict[str, Any]:
            closes, _ = price_series[symbol]
            return {"symbol": symbol, "price": f"{closes[-1]:.2f}"}

        def mark_price(self, symbol: str, *, timeout=None) -> dict[str, Any]:
            closes, _ = price_series[symbol]
            price_str = f"{closes[-1]:.2f}"
            return {
                "symbol": symbol,
                "price": price_str,
                "markPrice": price_str,
                "indexPrice": price_str,
                "lastFundingRate": "0",
                "nextFundingTime": 0,
            }

        def account_info(self, *, timeout=None) -> dict[str, Any]:
            return {
                "availableBalance": "1000.0",
                "totalWalletBalance": "1000.0",
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "1000.0",
                        "availableBalance": "1000.0",
                        "marginBalance": "1000.0",
                        "unrealizedProfit": "0.0",
                    }
                ],
                "balances": [
                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                ],
            }

        def position_risk(self, symbol: str | None = None, *, timeout=None) -> list[dict[str, Any]]:
            return []

        def open_orders(self, symbol: str | None = None, *, timeout=None) -> list[dict[str, Any]]:
            return []

        def open_algo_orders(
            self, symbol: str | None = None, *, timeout=None
        ) -> list[dict[str, Any]]:
            return []

    symbol_configs = [
        replace(
            cfg,
            position_notional_usdt=100.0,
            stop_loss_pct=0.02,
            take_profit_pct=0.05,
            leverage=1,
            max_daily_loss_usdt=0.0,
            max_daily_trades=0,
            max_consecutive_losses_guard=0,
            cooldown_after_loss_seconds=0.0,
            max_position_hold_seconds=0.0,
            max_exposure_usdt=0.0,
            state_path=str(tmp_path / "runtime" / f"state_{cfg.symbol.lower()}.json"),
            journal_path=str(tmp_path / "data" / f"trades_{cfg.symbol.lower()}.jsonl"),
            lock_path=str(tmp_path / "runtime" / f"lock_{cfg.symbol.lower()}.lock"),
            runtime_dir=str(tmp_path / "runtime"),
        )
        for cfg in resolve_governor_symbol_configs(base_config)
    ]
    portfolio_settings = PortfolioRiskSettings(
        max_open_positions=1,
        max_new_positions_per_cycle=1,
        max_total_exposure_usdt=200.0,
        governor_max_symbols_per_cycle=3,
    )

    client = FakeSpotRotationClient()
    governor = AlphoraGovernor(
        config=base_config,
        client=client,  # type: ignore[arg-type]
        symbol_configs=symbol_configs,
        portfolio_settings=portfolio_settings,
        max_symbols_per_cycle=3,
    )
    governor.strategy_coordinator._rotation_board["ADAUSDT"] = {
        "symbol": "ADAUSDT",
        "score": 100,
        "qualifies_for_entry": True,
    }

    orig_light = governor.market_data_router.get_light_snapshot
    orig_heavy = governor.market_data_router.get_heavy_snapshot

    def _light_with_candles(symbol: str, *, now: float | None = None):
        snap = orig_light(symbol, now=now)
        closes, vols = price_series[symbol]
        candles = _make_candles(symbol, closes, vols)
        prices = [Decimal(f"{p:.4f}") for p in closes]
        return replace(snap, market=replace(snap.market, prices=prices, candles=candles))

    def _heavy_with_candles(symbol_or_cfg, *, now: float | None = None, **kwargs):
        snap = orig_heavy(symbol_or_cfg, now=now, **kwargs)
        sym = symbol_or_cfg if isinstance(symbol_or_cfg, str) else symbol_or_cfg.symbol
        closes, vols = price_series[sym]
        candles = _make_candles(sym, closes, vols)
        prices = [Decimal(f"{p:.4f}") for p in closes]
        return replace(snap, market=replace(snap.market, prices=prices, candles=candles))

    governor.market_data_router.get_light_snapshot = _light_with_candles  # type: ignore[method-assign]
    governor.market_data_router.get_heavy_snapshot = _heavy_with_candles  # type: ignore[method-assign]

    # Open an existing paper position on BTCUSDT (entry at 50,000, current 50,649 -> profitable)
    btc_cfg = symbol_configs[0]
    btc_engine = governor.execution_engine._get_engine(btc_cfg)
    btc_engine.rules = extract_symbol_rules(client.exchange_info(), "BTCUSDT")
    governor.execution_engine._paper_open_position(
        btc_engine,
        action=TradeAction.OPEN_LONG,
        price=Decimal("50000.0"),
        reason="INITIAL_BTC_POSITION",
    )
    assert governor.execution_engine.get_position(btc_cfg).is_open is True

    # Run 1 Governor cycle: BTC should close and SOL should open in the same cycle!
    governor.run_cycle()

    assert "ADAUSDT" not in governor.strategy_coordinator._rotation_board

    btc_pos_after = governor.execution_engine.get_position(symbol_configs[0])
    sol_pos_after = governor.execution_engine.get_position(symbol_configs[2])

    assert btc_pos_after.is_open is False, f"BTC still open: {governor.snapshots.get('BTCUSDT')}"
    assert sol_pos_after.is_open is True, f"SOL not open: {governor.snapshots.get('SOLUSDT')}"
    assert sol_pos_after.direction is PositionDirection.LONG


def test_binance_spot_client_adapts_endpoints_balances_positions_and_orders(monkeypatch):
    """Requirement 7: BinanceSpotClient adapts Spot /api/v3 endpoints and synthesizes positions."""
    client = BinanceSpotClient(
        api_key="spot_key",
        api_secret="spot_secret",
        base_url="https://testnet.binance.vision",
        spot_mode=True,
    )
    assert isinstance(client, BinanceFuturesClient)
    assert client.spot_mode is True

    calls: list[tuple[str, str, dict[str, Any] | None, bool]] = []

    def fake_request(method: str, path: str, params=None, signed: bool = False, timeout=None):
        calls.append((method, path, params, signed))
        if path == "/api/v3/account":
            return {
                "balances": [
                    {"asset": "USDT", "free": "850.00", "locked": "50.00"},
                    {"asset": "BTC", "free": "0.01000000", "locked": "0.00000000"},
                ]
            }
        if path == "/api/v3/ticker/price":
            sym = (params or {}).get("symbol", "BTCUSDT")
            return {"symbol": sym, "price": "65000.00"}
        if path == "/api/v3/order" and method == "POST":
            return {
                "symbol": params["symbol"],
                "orderId": 123456,
                "clientOrderId": params.get("newClientOrderId", "cid_1"),
                "status": "FILLED",
                "type": params["type"],
                "side": params["side"],
                "origQty": params["quantity"],
                "executedQty": params["quantity"],
                "cummulativeQuoteQty": "650.00",
            }
        return {}

    monkeypatch.setattr(client, "_request", fake_request)

    account = client.account_info()
    assert Decimal(account["availableBalance"]) == Decimal("850.00")
    assert Decimal(account["totalWalletBalance"]) == Decimal("900.00")

    buy_order = client.new_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.01",
        reduceOnly="true",  # Futures-only param should be stripped on Spot
    )
    assert buy_order["avgPrice"] == "65000"
    assert "reduceOnly" not in (calls[-1][2] or {})

    positions = client.position_risk(symbol="BTCUSDT")
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTCUSDT"
    assert Decimal(positions[0]["positionAmt"]) == Decimal("0.01")
    assert Decimal(positions[0]["entryPrice"]) == Decimal("65000")

    # Virtual algo orders on Spot should not call /fapi/v1/algoOrder
    algo = client.new_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        trigger_price="63000",
        close_position=True,
    )
    assert len(client.open_algo_orders("BTCUSDT")) == 1
    client.cancel_all_open_algo_orders("BTCUSDT")
    assert len(client.open_algo_orders("BTCUSDT")) == 0
    assert algo["algoStatus"] == "NEW"


def test_spot_position_uses_exchange_balance_and_reports_actual_sell_fill(monkeypatch):
    client = BinanceSpotClient(
        api_key="spot_key",
        api_secret="spot_secret",
        base_url="https://testnet.binance.vision",
        spot_mode=True,
    )
    submitted_orders: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, params=None, signed=False, timeout=None):
        if path == "/api/v3/account":
            return {
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BTC", "free": "0.009", "locked": "0"},
                ]
            }
        if path == "/api/v3/ticker/price":
            return {"symbol": "BTCUSDT", "price": "65000"}
        if path == "/api/v3/order" and method == "POST":
            submitted_orders.append(dict(params))
            return {
                "symbol": "BTCUSDT",
                "status": "FILLED",
                "side": "SELL",
                "executedQty": "0.009",
                "cummulativeQuoteQty": "585",
            }
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    client._spot_entry_prices["BTCUSDT"] = Decimal("60000")
    client._spot_position_quantities["BTCUSDT"] = Decimal("0.01")

    position = client.position_risk("BTCUSDT")[0]
    assert Decimal(position["positionAmt"]) == Decimal("0.009")
    assert Decimal(position["unRealizedProfit"]) == Decimal("45")

    order = client.new_order(
        symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity="0.01"
    )
    assert Decimal(submitted_orders[0]["quantity"]) == Decimal("0.009")
    assert Decimal(order["executedQty"]) == Decimal("0.009")
    assert client._spot_entry_prices["BTCUSDT"] == Decimal("60000")
