from decimal import Decimal
from types import SimpleNamespace

import pytest

from governor.portfolio_risk_engine import (
    BALANCE_UNAVAILABLE_FIX_HINT,
    BALANCE_UNAVAILABLE_MESSAGE,
    BALANCE_UNAVAILABLE_REASON,
    BALANCE_UNAVAILABLE_SAFETY_ACTION,
)
from governor.portfolio_snapshot_provider import (
    DAILY_DRAWDOWN_SOURCE,
    PortfolioSnapshotProvider,
    daily_drawdown_snapshot,
)


def _position(
    *,
    symbol: str,
    is_open: bool,
    direction: str = "NONE",
    quantity: str = "0",
    entry_price: str = "0",
    unrealized_pnl: str = "0",
):
    return SimpleNamespace(
        symbol=symbol,
        is_open=is_open,
        direction=SimpleNamespace(value=direction),
        quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        unrealized_pnl=Decimal(unrealized_pnl),
    )


def test_portfolio_snapshot_provider_builds_open_positions_and_balance():
    symbol_configs = [
        SimpleNamespace(symbol="BTCUSDT"),
        SimpleNamespace(symbol="ETHUSDT"),
    ]

    def position_for_symbol(symbol_config, *, context=None):
        if symbol_config.symbol == "BTCUSDT":
            return _position(
                symbol="BTCUSDT",
                is_open=True,
                direction="LONG",
                quantity="0.5",
                entry_price="100",
                unrealized_pnl="-2",
            )

        return _position(symbol=symbol_config.symbol, is_open=False)

    def daily_metrics(*, context=None):
        return {
            "daily_realized_pnl": "3.50",
            "daily_realized_pnl_peak": "5.50",
            "daily_trades": 2,
            "consecutive_losses": 0,
            "pnl_unknown_closes": 0,
        }

    def account_info(*, context=None):
        return {
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "200",
                    "availableBalance": "123.45",
                }
            ]
        }

    provider = PortfolioSnapshotProvider(
        symbol_configs=symbol_configs,
        position_for_symbol=position_for_symbol,
        daily_metrics=daily_metrics,
        account_info=account_info,
    )

    snapshot = provider.build(new_positions_this_cycle=1)

    assert snapshot == {
        "open_positions": [
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "quantity": "0.5",
                "entry_price": "100",
                "notional": "50.0",
            }
        ],
        "available_balance_usdt": "123.45",
        "new_positions_this_cycle": 1,
        "daily_realized_pnl": "3.50",
        "daily_realized_pnl_peak": "5.50",
        "daily_trades": 2,
        "consecutive_losses": 0,
        "pnl_unknown_closes": 0,
        "daily_drawdown": {
            "available": True,
            # (5.50 - 3.50) realized drawdown + 2 open loss = 4 over a 200 wallet.
            "fraction": "0.02",
            "drawdown_usdt": "4.00",
            "realized_drawdown_usdt": "2.00",
            "open_unrealized_loss_usdt": "2",
            "wallet_balance_usdt": "200",
            "source": DAILY_DRAWDOWN_SOURCE,
            "reason": None,
        },
    }


def test_portfolio_snapshot_provider_marks_account_info_failure():
    symbol_configs = [SimpleNamespace(symbol="BTCUSDT")]

    def position_for_symbol(symbol_config, *, context=None):
        return _position(symbol=symbol_config.symbol, is_open=False)

    def daily_metrics(*, context=None):
        return {
            "daily_realized_pnl": "0",
            "daily_trades": 0,
            "consecutive_losses": 0,
            "pnl_unknown_closes": 0,
        }

    def account_info(*, context=None):
        return None

    provider = PortfolioSnapshotProvider(
        symbol_configs=symbol_configs,
        position_for_symbol=position_for_symbol,
        daily_metrics=daily_metrics,
        account_info=account_info,
    )

    snapshot = provider.build(new_positions_this_cycle=0)

    assert snapshot["open_positions"] == []
    assert snapshot["available_balance_usdt"] == "0"
    assert snapshot["account_info_failed"] is True
    assert snapshot["account_info_error"] == "Falha ao consultar account_info da Binance"
    assert snapshot["balance_unavailable_blocks_new_trades"] is True
    assert snapshot["motivo_pt"] == BALANCE_UNAVAILABLE_MESSAGE
    assert snapshot["como_corrigir"] == BALANCE_UNAVAILABLE_FIX_HINT
    assert snapshot["acao_de_seguranca"] == BALANCE_UNAVAILABLE_SAFETY_ACTION
    assert snapshot["blocked_symbols"] == [BALANCE_UNAVAILABLE_REASON]


def test_portfolio_snapshot_provider_rejects_untrusted_cached_account_balance():
    symbol_configs = [SimpleNamespace(symbol="BTCUSDT")]

    def position_for_symbol(symbol_config, *, context=None):
        return _position(symbol=symbol_config.symbol, is_open=False)

    def daily_metrics(*, context=None):
        return {
            "daily_realized_pnl": "0",
            "daily_trades": 0,
            "consecutive_losses": 0,
            "pnl_unknown_closes": 0,
        }

    def account_info(*, context=None):
        return {
            "source": "user_data_shadow_cache",
            "runtime_balance_fields_trusted": False,
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                }
            ],
        }

    provider = PortfolioSnapshotProvider(
        symbol_configs=symbol_configs,
        position_for_symbol=position_for_symbol,
        daily_metrics=daily_metrics,
        account_info=account_info,
    )

    snapshot = provider.build(new_positions_this_cycle=0)

    assert snapshot["available_balance_usdt"] == "0"
    assert snapshot["account_info_failed"] is True
    assert snapshot["blocked_symbols"] == [BALANCE_UNAVAILABLE_REASON]


def test_portfolio_snapshot_provider_ignores_position_read_errors():
    symbol_configs = [
        SimpleNamespace(symbol="BTCUSDT"),
        SimpleNamespace(symbol="ETHUSDT"),
    ]

    def position_for_symbol(symbol_config, *, context=None):
        if symbol_config.symbol == "BTCUSDT":
            raise RuntimeError("position failed")

        return _position(
            symbol="ETHUSDT",
            is_open=True,
            direction="SHORT",
            quantity="2",
            entry_price="10",
        )

    def daily_metrics(*, context=None):
        return {
            "daily_realized_pnl": "0",
            "daily_trades": 0,
            "consecutive_losses": 0,
            "pnl_unknown_closes": 0,
        }

    def account_info(*, context=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "20"}]}

    provider = PortfolioSnapshotProvider(
        symbol_configs=symbol_configs,
        position_for_symbol=position_for_symbol,
        daily_metrics=daily_metrics,
        account_info=account_info,
    )

    snapshot = provider.build()

    assert snapshot["open_positions"] == [
        {
            "symbol": "ETHUSDT",
            "direction": "SHORT",
            "quantity": "2",
            "entry_price": "10",
            "notional": "20",
        }
    ]


def _drawdown(**overrides):
    arguments = {
        "metrics": {
            "daily_realized_pnl": "-30",
            "daily_realized_pnl_peak": "20",
            "pnl_unknown_closes": 0,
        },
        "wallet_balance_usdt": "1000",
        "open_unrealized_loss_usdt": Decimal("10"),
        "position_snapshot_complete": True,
    }
    arguments.update(overrides)
    return daily_drawdown_snapshot(**arguments)


def test_daily_drawdown_measures_from_realized_peak_plus_open_losses():
    snapshot = _drawdown()

    assert snapshot["available"] is True
    assert snapshot["reason"] is None
    assert snapshot["realized_drawdown_usdt"] == "50"
    assert snapshot["drawdown_usdt"] == "60"
    assert Decimal(snapshot["fraction"]) == Decimal("0.06")


def test_daily_drawdown_is_zero_at_a_new_realized_high_without_open_losses():
    snapshot = _drawdown(
        metrics={
            "daily_realized_pnl": "20",
            "daily_realized_pnl_peak": "20",
            "pnl_unknown_closes": 0,
        },
        open_unrealized_loss_usdt=Decimal("0"),
    )

    assert snapshot["available"] is True
    assert Decimal(snapshot["fraction"]) == Decimal("0")


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"position_snapshot_complete": False}, "POSITION_SNAPSHOT_INCOMPLETE"),
        ({"open_unrealized_loss_usdt": None}, "UNREALIZED_PNL_UNAVAILABLE"),
        (
            {
                "metrics": {
                    "daily_realized_pnl": "0",
                    "daily_realized_pnl_peak": "0",
                    "pnl_unknown_closes": 1,
                }
            },
            "DAILY_PNL_INCOMPLETE",
        ),
        ({"metrics": {"daily_realized_pnl": "0"}}, "DAILY_PNL_UNAVAILABLE"),
        ({"wallet_balance_usdt": None}, "WALLET_BALANCE_UNAVAILABLE"),
        ({"wallet_balance_usdt": "0"}, "WALLET_BALANCE_UNAVAILABLE"),
        ({"wallet_balance_usdt": "-5"}, "WALLET_BALANCE_UNAVAILABLE"),
        ({"wallet_balance_usdt": "nan"}, "WALLET_BALANCE_UNAVAILABLE"),
    ],
)
def test_daily_drawdown_is_unavailable_instead_of_zero_when_inputs_are_unreliable(
    overrides,
    reason,
):
    snapshot = _drawdown(**overrides)

    assert snapshot["available"] is False
    assert snapshot["fraction"] is None
    assert snapshot["reason"] == reason


def test_portfolio_snapshot_marks_drawdown_unavailable_for_untrusted_balance():
    provider = PortfolioSnapshotProvider(
        symbol_configs=[SimpleNamespace(symbol="BTCUSDT")],
        position_for_symbol=lambda symbol_config, *, context=None: _position(
            symbol=symbol_config.symbol,
            is_open=False,
        ),
        daily_metrics=lambda *, context=None: {
            "daily_realized_pnl": "0",
            "daily_realized_pnl_peak": "0",
            "pnl_unknown_closes": 0,
        },
        account_info=lambda *, context=None: {
            "runtime_balance_fields_trusted": False,
            "assets": [{"asset": "USDT", "walletBalance": "100"}],
        },
    )

    snapshot = provider.build()

    assert snapshot["daily_drawdown"]["available"] is False
    assert snapshot["daily_drawdown"]["reason"] == "WALLET_BALANCE_UNAVAILABLE"


def test_portfolio_snapshot_ignores_open_gains_when_measuring_drawdown():
    positions = {
        "BTCUSDT": _position(
            symbol="BTCUSDT",
            is_open=True,
            direction="LONG",
            quantity="1",
            entry_price="100",
            unrealized_pnl="25",
        ),
        "ETHUSDT": _position(
            symbol="ETHUSDT",
            is_open=True,
            direction="SHORT",
            quantity="1",
            entry_price="10",
            unrealized_pnl="-7",
        ),
    }
    provider = PortfolioSnapshotProvider(
        symbol_configs=[SimpleNamespace(symbol="BTCUSDT"), SimpleNamespace(symbol="ETHUSDT")],
        position_for_symbol=lambda symbol_config, *, context=None: positions[symbol_config.symbol],
        daily_metrics=lambda *, context=None: {
            "daily_realized_pnl": "0",
            "daily_realized_pnl_peak": "0",
            "pnl_unknown_closes": 0,
        },
        account_info=lambda *, context=None: {
            "assets": [{"asset": "USDT", "walletBalance": "100", "availableBalance": "90"}]
        },
    )

    drawdown = provider.build()["daily_drawdown"]

    assert drawdown["available"] is True
    assert drawdown["open_unrealized_loss_usdt"] == "7"
    assert Decimal(drawdown["fraction"]) == Decimal("0.07")
