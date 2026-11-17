from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from governor.governor_helpers import _extract_available_usdt, _extract_wallet_usdt
from governor.portfolio_risk_engine import (
    BALANCE_UNAVAILABLE_FIX_HINT,
    BALANCE_UNAVAILABLE_MESSAGE,
    BALANCE_UNAVAILABLE_REASON,
    BALANCE_UNAVAILABLE_SAFETY_ACTION,
)

DAILY_DRAWDOWN_SOURCE = "UTC_DAY_REALIZED_PEAK_PLUS_OPEN_LOSS_OVER_WALLET"


class PortfolioSnapshotProvider:
    def __init__(
        self,
        *,
        symbol_configs: Iterable[Any],
        position_for_symbol: Callable[..., Any],
        daily_metrics: Callable[..., dict[str, Any]],
        account_info: Callable[..., dict[str, Any] | None],
    ):
        self.symbol_configs = tuple(symbol_configs)
        self.position_for_symbol = position_for_symbol
        self.daily_metrics = daily_metrics
        self.account_info = account_info

    def build(
        self,
        *,
        context: Any | None = None,
        new_positions_this_cycle: int = 0,
    ) -> dict[str, Any]:
        positions = []
        position_failed_symbols: list[str] = []
        open_unrealized_loss: Decimal | None = Decimal("0")

        for symbol_config in self.symbol_configs:
            try:
                position = self.position_for_symbol(symbol_config, context=context)
            except Exception:
                # A failed position query must not read as "no position": that
                # undercounts the portfolio and lets MAX_OPEN_POSITIONS and the
                # exposure limits approve trades against incomplete data.
                position_failed_symbols.append(symbol_config.symbol)
                continue

            if not position.is_open:
                continue

            unrealized_pnl = _finite_decimal(getattr(position, "unrealized_pnl", None))
            if unrealized_pnl is None or open_unrealized_loss is None:
                open_unrealized_loss = None
            else:
                open_unrealized_loss += max(Decimal("0"), -unrealized_pnl)

            positions.append(
                {
                    "symbol": position.symbol,
                    "direction": position.direction.value,
                    "quantity": str(position.quantity),
                    "entry_price": str(position.entry_price),
                    "notional": str(position.quantity * position.entry_price),
                }
            )

        metrics = self.daily_metrics(context=context)

        account_snapshot = {
            "open_positions": positions,
            "available_balance_usdt": "0",
            "new_positions_this_cycle": new_positions_this_cycle,
            **metrics,
        }
        if position_failed_symbols:
            account_snapshot["position_snapshot_failed_symbols"] = position_failed_symbols

        account_info = self.account_info(context=context)

        spot_equity = None
        if account_info is not None and "spotEquityUSDT" in account_info:
            if not account_info.get("spotEquityComplete", False):
                account_snapshot["account_info_failed"] = True
                account_snapshot["account_info_error"] = (
                    "Ativos Spot sem cotacao USDT: "
                    + ", ".join(account_info.get("spotUnpricedAssets", []))
                )
            else:
                spot_equity = account_info["spotEquityUSDT"]
                account_snapshot["equity_usdt"] = spot_equity
                prices = account_info.get("spotAssetPricesUSDT", {})
                for position in positions:
                    asset = position["symbol"].removesuffix("USDT")
                    price = _finite_decimal(prices.get(asset))
                    if price is not None and price > 0:
                        position["notional"] = str(Decimal(position["quantity"]) * price)

        available_balance = (
            _extract_available_usdt(account_info) if account_info is not None else None
        )

        if available_balance is not None and not account_snapshot.get("account_info_failed"):
            account_snapshot["available_balance_usdt"] = available_balance
        else:
            account_snapshot["account_info_failed"] = True
            account_snapshot["account_info_error"] = "Falha ao consultar account_info da Binance"
            account_snapshot["balance_unavailable_blocks_new_trades"] = True
            account_snapshot["motivo_pt"] = BALANCE_UNAVAILABLE_MESSAGE
            account_snapshot["como_corrigir"] = BALANCE_UNAVAILABLE_FIX_HINT
            account_snapshot["acao_de_seguranca"] = BALANCE_UNAVAILABLE_SAFETY_ACTION
            account_snapshot["blocked_symbols"] = [BALANCE_UNAVAILABLE_REASON]

        account_snapshot["daily_drawdown"] = daily_drawdown_snapshot(
            metrics=metrics,
            wallet_balance_usdt=(
                spot_equity or _extract_wallet_usdt(account_info)
                if account_info is not None else None
            ),
            open_unrealized_loss_usdt=open_unrealized_loss,
            position_snapshot_complete=not position_failed_symbols,
        )

        return account_snapshot


def daily_drawdown_snapshot(
    *,
    metrics: dict[str, Any],
    wallet_balance_usdt: Any,
    open_unrealized_loss_usdt: Decimal | None,
    position_snapshot_complete: bool,
) -> dict[str, Any]:
    """Return the current UTC-day drawdown as a fraction of the wallet balance.

    drawdown_usdt = (realized PnL high-water mark - realized PnL)
                    + open unrealized losses (open gains never offset it)

    Dividing by the current wallet balance instead of the day's peak equity
    can only overstate the fraction, so the measure errs on the safe side.
    Any missing or unreliable input makes the measure unavailable instead of
    reporting a drawdown of zero.
    """

    snapshot: dict[str, Any] = {
        "available": False,
        "fraction": None,
        "drawdown_usdt": None,
        "source": DAILY_DRAWDOWN_SOURCE,
        "reason": None,
    }

    if not position_snapshot_complete:
        snapshot["reason"] = "POSITION_SNAPSHOT_INCOMPLETE"
        return snapshot

    if open_unrealized_loss_usdt is None:
        snapshot["reason"] = "UNREALIZED_PNL_UNAVAILABLE"
        return snapshot

    pnl_unknown_closes = _finite_decimal(metrics.get("pnl_unknown_closes", 0))
    if pnl_unknown_closes is None or pnl_unknown_closes > 0:
        snapshot["reason"] = "DAILY_PNL_INCOMPLETE"
        return snapshot

    realized = _finite_decimal(metrics.get("daily_realized_pnl"))
    realized_peak = _finite_decimal(metrics.get("daily_realized_pnl_peak"))
    if realized is None or realized_peak is None:
        snapshot["reason"] = "DAILY_PNL_UNAVAILABLE"
        return snapshot

    wallet_balance = _finite_decimal(wallet_balance_usdt)
    if wallet_balance is None or wallet_balance <= 0:
        snapshot["reason"] = "WALLET_BALANCE_UNAVAILABLE"
        return snapshot

    realized_drawdown = max(Decimal("0"), realized_peak - realized)
    drawdown_usdt = realized_drawdown + open_unrealized_loss_usdt
    snapshot.update(
        {
            "available": True,
            "fraction": str(drawdown_usdt / wallet_balance),
            "drawdown_usdt": str(drawdown_usdt),
            "realized_drawdown_usdt": str(realized_drawdown),
            "open_unrealized_loss_usdt": str(open_unrealized_loss_usdt),
            "wallet_balance_usdt": str(wallet_balance),
        }
    )
    return snapshot


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal if decimal.is_finite() else None
