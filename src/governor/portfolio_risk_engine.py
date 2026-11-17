from dataclasses import asdict
from decimal import Decimal
from typing import Any

from config.symbol_context import PortfolioRiskSettings, SymbolTradingConfig
from domain.models import TradeAction
from governor.models import GovernorDecision, PortfolioSnapshot
from trading.intent import TradeIntent

BALANCE_UNAVAILABLE_REASON = "SALDO_NAO_CONFIRMADO"
BALANCE_UNAVAILABLE_MESSAGE = (
    "Não foi possível confirmar o saldo disponível na Binance. "
    "Por segurança, novas entradas foram bloqueadas."
)
BALANCE_UNAVAILABLE_FIX_HINT = (
    "Verifique chave API, permissão de leitura da conta Futures, IP autorizado, "
    "ambiente TESTNET/REAL, conexão com a internet e possível limite de chamadas "
    "da Binance, como erros 429 ou 418."
)
BALANCE_UNAVAILABLE_SAFETY_ACTION = (
    "Bloquear somente novas entradas. Fechamentos, auditorias e proteções de "
    "posições abertas devem continuar permitidos."
)

# Operator guidance per block reason, surfaced as como_corrigir in the text
# log and in the trade_blocked_by_governor event. Deliberately NOT motivo_pt:
# downstream snapshots use motivo_pt (when present) as the machine-facing
# blocked reason, which must stay the reason code.
_BLOCK_REASON_FIX_HINTS: dict[str, str] = {
    "MAX_OPEN_POSITIONS": (
        "Aguarde o fechamento de uma posição ou aumente max_open_positions "
        "nas configurações de risco do portfólio."
    ),
    "MULTIPLE_POSITIONS_SAME_SYMBOL": (
        "Aguarde o fechamento da posição atual ou habilite "
        "allow_multiple_positions_same_symbol nas configurações de risco."
    ),
    "MULTIPLE_READY_BUT_PORTFOLIO_LIMITED": (
        "Aguarde o próximo ciclo ou aumente max_new_positions_per_cycle "
        "nas configurações de risco do portfólio."
    ),
    "MAX_TOTAL_EXPOSURE": "Reduza posições abertas ou aumente max_total_exposure_usdt.",
    "MAX_LONG_EXPOSURE": "Reduza posições LONG abertas ou aumente max_long_exposure_usdt.",
    "MAX_SHORT_EXPOSURE": "Reduza posições SHORT abertas ou aumente max_short_exposure_usdt.",
    "MAX_SYMBOL_EXPOSURE": ("Reduza a posição neste símbolo ou aumente max_symbol_exposure_usdt."),
    "MAX_GROUP_EXPOSURE": ("Reduza posições no grupo ou ajuste os limites de correlation_groups."),
    "DAILY_LOSS_LIMIT": "Aguarde o próximo dia de operação ou revise max_daily_loss_usdt.",
    "DAILY_PROFIT_TARGET_REACHED": (
        "Aguarde o próximo dia de operação ou revise stop_after_daily_profit_usdt."
    ),
    "BALANCE_INSUFFICIENT": (
        "Reduza position_notional_usdt, aumente o saldo da conta ou revise a alavancagem."
    ),
}


class PortfolioRiskEngine:
    def __init__(self, settings: PortfolioRiskSettings | None = None):
        self.settings = settings or PortfolioRiskSettings()

    def evaluate_trade_intent(
        self,
        intent: TradeIntent,
        symbol_contexts: list[SymbolTradingConfig],
        account_snapshot: dict[str, Any] | None = None,
        *,
        override_notional: Decimal | None = None,
    ) -> GovernorDecision:
        """Evaluate portfolio risk for a trade intent.

        ``override_notional`` allows the caller to supply the exact notional that
        will reach execution (e.g. after smart sizing or learning adjustments),
        instead of relying on the static value in ``intent.metadata`` or the
        symbol profile. When omitted the legacy fallback is used.
        """
        account_snapshot = account_snapshot or {}
        action = intent.action.upper()

        # Close/protect actions must not be blocked by a missing balance read.
        # The unavailable-balance guard applies only to new entries.
        if action not in {TradeAction.OPEN_LONG.value, TradeAction.OPEN_SHORT.value}:
            return self._approved(intent, account_snapshot, "APPROVED_NO_NEW_POSITION")

        positions = _open_positions(
            account_snapshot.get("open_positions", []),
            settings=self.settings,
        )
        context = _context_for_symbol(symbol_contexts, intent.symbol)
        if override_notional is not None:
            candidate_notional = Decimal(str(override_notional))
        else:
            candidate_notional = Decimal(
                str(intent.metadata.get("notional") or context.position_notional_usdt)
            )
        direction = "LONG" if action == TradeAction.OPEN_LONG.value else "SHORT"
        group, group_source = resolve_correlation_group(intent.symbol, self.settings)
        checks: dict[str, Any] = {
            "candidate_notional": str(candidate_notional),
            "direction": direction,
            "position_count": len(positions),
            "group": group,
            "group_source": group_source,
            "correlation_group_mode": self.settings.correlation_group_mode,
            "configured_correlation_groups_count": len(self.settings.correlation_groups or {}),
        }

        # If Alphora could not confirm balance/account on Binance,
        # no new position may be opened.
        if account_snapshot.get("account_info_failed"):
            checks.update(
                {
                    "account_info_failed": True,
                    "motivo_pt": BALANCE_UNAVAILABLE_MESSAGE,
                    "como_corrigir": BALANCE_UNAVAILABLE_FIX_HINT,
                    "acao_de_seguranca": BALANCE_UNAVAILABLE_SAFETY_ACTION,
                    "bloqueia_novas_entradas": True,
                    "bloqueia_fechamentos": False,
                    "erro_original": account_snapshot.get("account_info_error", ""),
                }
            )
            return self._blocked(
                intent,
                f"{BALANCE_UNAVAILABLE_REASON}: {BALANCE_UNAVAILABLE_MESSAGE}",
                checks,
                account_snapshot,
            )

        position_failed_symbols = list(
            account_snapshot.get("position_snapshot_failed_symbols") or []
        )
        if position_failed_symbols:
            checks.update(
                {
                    "position_snapshot_failed_symbols": position_failed_symbols,
                    "motivo_pt": (
                        "Não foi possível confirmar as posições abertas de todos os "
                        "símbolos; novas entradas foram bloqueadas por segurança."
                    ),
                    "como_corrigir": (
                        "Verifique conexão e permissões da API Binance. A consulta de "
                        "posição falhou para: " + ", ".join(position_failed_symbols)
                    ),
                    "bloqueia_novas_entradas": True,
                    "bloqueia_fechamentos": False,
                }
            )
            return self._blocked(
                intent,
                "POSITION_SNAPSHOT_INCOMPLETE",
                checks,
                account_snapshot,
            )

        same_symbol = [item for item in positions if item["symbol"] == intent.symbol]
        if same_symbol and not self.settings.allow_multiple_positions_same_symbol:
            return self._blocked(
                intent,
                "MULTIPLE_POSITIONS_SAME_SYMBOL",
                checks,
                account_snapshot,
            )

        if (
            self.settings.max_open_positions > 0
            and len(positions) >= self.settings.max_open_positions
        ):
            return self._blocked(intent, "MAX_OPEN_POSITIONS", checks, account_snapshot)

        new_positions_this_cycle = int(account_snapshot.get("new_positions_this_cycle", 0))
        if (
            self.settings.max_new_positions_per_cycle > 0
            and new_positions_this_cycle >= self.settings.max_new_positions_per_cycle
        ):
            return self._blocked(
                intent,
                "MULTIPLE_READY_BUT_PORTFOLIO_LIMITED",
                checks,
                account_snapshot,
            )

        exposure_by_symbol = _exposure_by(positions, "symbol")
        exposure_by_direction = _exposure_by(positions, "direction")
        exposure_by_group = _exposure_by_group(positions)
        total_exposure = sum((item["notional"] for item in positions), Decimal("0"))
        next_total = total_exposure + candidate_notional
        next_symbol = exposure_by_symbol.get(intent.symbol, Decimal("0")) + candidate_notional
        next_direction = exposure_by_direction.get(direction, Decimal("0")) + candidate_notional
        next_group = exposure_by_group.get(group, Decimal("0")) + candidate_notional

        checks.update(
            {
                "total_exposure_after": str(next_total),
                "symbol_exposure_after": str(next_symbol),
                "direction_exposure_after": str(next_direction),
                "group_exposure_after": str(next_group),
            }
        )

        if self.settings.max_total_exposure_usdt > 0 and next_total > Decimal(
            str(self.settings.max_total_exposure_usdt)
        ):
            return self._blocked(intent, "MAX_TOTAL_EXPOSURE", checks, account_snapshot)

        if (
            direction == "LONG"
            and self.settings.max_long_exposure_usdt > 0
            and next_direction > Decimal(str(self.settings.max_long_exposure_usdt))
        ):
            return self._blocked(intent, "MAX_LONG_EXPOSURE", checks, account_snapshot)

        if (
            direction == "SHORT"
            and self.settings.max_short_exposure_usdt > 0
            and next_direction > Decimal(str(self.settings.max_short_exposure_usdt))
        ):
            return self._blocked(intent, "MAX_SHORT_EXPOSURE", checks, account_snapshot)

        if self.settings.max_symbol_exposure_usdt > 0 and next_symbol > Decimal(
            str(self.settings.max_symbol_exposure_usdt)
        ):
            return self._blocked(intent, "MAX_SYMBOL_EXPOSURE", checks, account_snapshot)

        if self.settings.max_group_exposure_usdt > 0 and next_group > Decimal(
            str(self.settings.max_group_exposure_usdt)
        ):
            checks["max_group_exposure_usdt"] = str(self.settings.max_group_exposure_usdt)
            if _should_enforce_group_limit(group_source, self.settings):
                return self._blocked(intent, "MAX_GROUP_EXPOSURE", checks, account_snapshot)
            checks["group_exposure_shadow_blocked"] = True

        if self.settings.max_correlated_positions > 0:
            same_group_count = sum(1 for item in positions if item["group"] == group)
            checks["same_correlation_group_positions"] = same_group_count
            checks["max_correlated_positions"] = self.settings.max_correlated_positions
            if same_group_count >= self.settings.max_correlated_positions:
                if _should_enforce_group_limit(group_source, self.settings):
                    return self._blocked(
                        intent,
                        "MAX_CORRELATED_POSITIONS",
                        checks,
                        account_snapshot,
                    )
                checks["max_correlated_positions_shadow_blocked"] = True

        if not self.settings.allow_multiple_same_direction:
            if any(item["direction"] == direction for item in positions):
                return self._blocked(
                    intent,
                    "MAX_LONG_EXPOSURE" if direction == "LONG" else "MAX_SHORT_EXPOSURE",
                    checks,
                    account_snapshot,
                )

        pnl_unknown_closes = int(account_snapshot.get("pnl_unknown_closes", 0) or 0)
        daily_limits_enabled = (
            self.settings.max_daily_loss_usdt > 0 or self.settings.stop_after_daily_profit_usdt > 0
        )
        if pnl_unknown_closes > 0 and daily_limits_enabled:
            checks.update(
                {
                    "pnl_unknown_closes": pnl_unknown_closes,
                    "daily_pnl_incomplete": True,
                    "motivo_pt": (
                        "Ha fechamento live confirmado sem PnL realizado confiavel; "
                        "limites diarios podem estar incompletos."
                    ),
                }
            )
            return self._blocked(
                intent,
                "DAILY_PNL_INCOMPLETE",
                checks,
                account_snapshot,
            )

        daily_pnl = Decimal(str(account_snapshot.get("daily_realized_pnl", "0")))
        if self.settings.max_daily_loss_usdt > 0 and daily_pnl <= -Decimal(
            str(self.settings.max_daily_loss_usdt)
        ):
            return self._blocked(intent, "DAILY_LOSS_LIMIT", checks, account_snapshot)

        if self.settings.stop_after_daily_profit_usdt > 0 and daily_pnl >= Decimal(
            str(self.settings.stop_after_daily_profit_usdt)
        ):
            return self._blocked(
                intent,
                "DAILY_PROFIT_TARGET_REACHED",
                checks,
                account_snapshot,
            )

        available = Decimal(str(account_snapshot.get("available_balance_usdt", "0")))
        leverage = max(1, _leverage_for_symbol(context))
        required_margin = candidate_notional / Decimal(str(leverage))
        required_with_safety = required_margin * Decimal("1.10")
        checks.update(
            {
                "leverage": leverage,
                "required_margin_usdt": str(required_margin),
                "required_margin_with_safety_usdt": str(required_with_safety),
                "available_balance_usdt": str(available),
            }
        )
        if available > 0 and available < required_with_safety:
            return self._blocked(intent, "BALANCE_INSUFFICIENT", checks, account_snapshot)

        return self._approved(intent, account_snapshot, "APPROVED", checks)

    def snapshot(self, account_snapshot: dict[str, Any] | None = None) -> PortfolioSnapshot:
        account_snapshot = account_snapshot or {}
        positions = _open_positions(
            account_snapshot.get("open_positions", []),
            settings=self.settings,
        )
        exposure_by_symbol = _exposure_by(positions, "symbol")
        exposure_by_direction = _exposure_by(positions, "direction")
        exposure_by_group = _exposure_by_group(positions)
        total_exposure = sum((item["notional"] for item in positions), Decimal("0"))

        blocked_symbols = list(account_snapshot.get("blocked_symbols", []))
        if account_snapshot.get("account_info_failed"):
            blocked_symbols.append(BALANCE_UNAVAILABLE_REASON)

        return PortfolioSnapshot(
            total_open_positions=len(positions),
            max_open_positions=self.settings.max_open_positions,
            total_exposure_usdt=str(total_exposure),
            max_total_exposure_usdt=str(self.settings.max_total_exposure_usdt),
            exposure_by_symbol={key: str(value) for key, value in exposure_by_symbol.items()},
            exposure_by_direction={key: str(value) for key, value in exposure_by_direction.items()},
            exposure_by_group={key: str(value) for key, value in exposure_by_group.items()},
            daily_realized_pnl=str(account_snapshot.get("daily_realized_pnl", "0")),
            daily_trades=int(account_snapshot.get("daily_trades", 0)),
            consecutive_losses=int(account_snapshot.get("consecutive_losses", 0)),
            blocked_symbols=blocked_symbols,
        )

    def _approved(
        self,
        intent: TradeIntent,
        account_snapshot: dict[str, Any],
        reason: str,
        checks: dict[str, Any] | None = None,
    ) -> GovernorDecision:
        return GovernorDecision(
            allowed=True,
            symbol=intent.symbol,
            action=intent.action,
            reason=reason,
            checks=checks or {},
            portfolio_snapshot=asdict(self.snapshot(account_snapshot)),
        )

    def _blocked(
        self,
        intent: TradeIntent,
        reason: str,
        checks: dict[str, Any],
        account_snapshot: dict[str, Any],
    ) -> GovernorDecision:
        hint = _BLOCK_REASON_FIX_HINTS.get(reason)
        if hint is not None:
            checks.setdefault("como_corrigir", hint)
        return GovernorDecision(
            allowed=False,
            symbol=intent.symbol,
            action=intent.action,
            reason=reason,
            checks=checks,
            portfolio_snapshot=asdict(self.snapshot(account_snapshot)),
        )


def correlation_group(symbol: str) -> str:
    normalized = symbol.upper().strip()
    if normalized == "BTCUSDT":
        return "BTC"
    if normalized == "ETHUSDT":
        return "ETH"
    if normalized.endswith("USDT"):
        return "ALT_USDT"
    return normalized


def resolve_correlation_group(
    symbol: str,
    settings: PortfolioRiskSettings | None = None,
) -> tuple[str, str]:
    normalized = symbol.upper().strip()
    settings = settings or PortfolioRiskSettings()
    configured = (settings.correlation_groups or {}).get(normalized)
    if configured is not None:
        group = str(configured or "").upper().strip()
        if group:
            return group, "CONFIGURED"
    return correlation_group(normalized), "FALLBACK"


def _should_enforce_group_limit(
    group_source: str,
    settings: PortfolioRiskSettings,
) -> bool:
    mode = str(settings.correlation_group_mode or "OBSERVE").upper().strip()
    if group_source == "CONFIGURED" and mode == "OBSERVE":
        return False
    return True


def _context_for_symbol(
    symbol_contexts: list[SymbolTradingConfig],
    symbol: str,
) -> SymbolTradingConfig:
    for context in symbol_contexts:
        if context.symbol == symbol:
            return context
    raise ValueError(f"Perfil do simbolo nao encontrado: {symbol}")


def _leverage_for_symbol(context: SymbolTradingConfig) -> int:
    try:
        return max(1, int(context.leverage))
    except (TypeError, ValueError):
        return 1


def _open_positions(
    raw_positions: list[dict[str, Any]],
    *,
    settings: PortfolioRiskSettings | None = None,
) -> list[dict[str, Any]]:
    positions = []
    for raw in raw_positions:
        symbol = str(raw.get("symbol", "")).upper().strip()
        direction = str(raw.get("direction", "")).upper().strip()
        if direction in {"NONE", ""}:
            continue
        notional = Decimal(str(raw.get("notional", raw.get("exposure_usdt", "0"))))
        if notional <= 0:
            quantity = Decimal(str(raw.get("quantity", "0")))
            entry = Decimal(str(raw.get("entry_price", raw.get("entryPrice", "0"))))
            notional = abs(quantity * entry)
        group, group_source = resolve_correlation_group(symbol, settings)
        positions.append(
            {
                "symbol": symbol,
                "direction": direction,
                "notional": notional,
                "group": group,
                "group_source": group_source,
            }
        )
    return positions


def _exposure_by(positions: list[dict[str, Any]], key: str) -> dict[str, Decimal]:
    exposure: dict[str, Decimal] = {}
    for item in positions:
        bucket = str(item[key])
        exposure[bucket] = exposure.get(bucket, Decimal("0")) + item["notional"]
    return exposure


def _exposure_by_group(positions: list[dict[str, Any]]) -> dict[str, Decimal]:
    return _exposure_by(positions, "group")
