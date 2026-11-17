from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from futures.performance.metrics import PerformanceMetrics


class ReadinessStatus(Enum):
    NOT_READY = "NOT_READY"
    NEEDS_MORE_DATA = "NEEDS_MORE_DATA"
    BACKTEST_OK = "BACKTEST_OK"
    PAPER_OK = "PAPER_OK"
    TESTNET_OK = "TESTNET_OK"
    REAL_CANDIDATE_SMALL_SIZE = "REAL_CANDIDATE_SMALL_SIZE"


@dataclass(frozen=True)
class ReadinessConfig:
    min_trades: int = 30
    min_profit_factor: Decimal = Decimal("1.2")
    min_expectancy: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0.2")
    max_consecutive_losses: int = 5
    require_positive_net_pnl: bool = True


@dataclass(frozen=True)
class ReadinessResult:
    status: ReadinessStatus
    passed: bool
    reasons: list[str]
    warnings: list[str]
    metrics: dict[str, str | int | bool]


class StrategyReadinessEvaluator:
    def __init__(self, config: ReadinessConfig | None = None):
        self.config = config or ReadinessConfig()

    def evaluate(self, metrics: PerformanceMetrics) -> ReadinessResult:
        summary = _metrics_summary(metrics)

        if metrics.total_trades < self.config.min_trades:
            return ReadinessResult(
                status=ReadinessStatus.NEEDS_MORE_DATA,
                passed=False,
                reasons=[
                    f"Amostra insuficiente: {metrics.total_trades}/{self.config.min_trades} trades."
                ],
                warnings=["Colete mais dados antes de concluir potencial de estrategia."],
                metrics=summary,
            )

        reasons = []

        if self.config.require_positive_net_pnl and metrics.net_pnl <= 0:
            reasons.append("PnL liquido nao ficou positivo apos custos.")

        if metrics.expectancy <= self.config.min_expectancy:
            reasons.append(
                f"Expectancy abaixo do minimo: {metrics.expectancy} <= {self.config.min_expectancy}."
            )

        if metrics.profit_factor < self.config.min_profit_factor:
            reasons.append(
                f"Profit factor abaixo do minimo: {metrics.profit_factor} < {self.config.min_profit_factor}."
            )

        if metrics.max_drawdown_pct > self.config.max_drawdown_pct:
            reasons.append(
                "Drawdown acima do limite: "
                f"{metrics.max_drawdown_pct} > {self.config.max_drawdown_pct}."
            )

        if metrics.max_consecutive_losses > self.config.max_consecutive_losses:
            reasons.append(
                "Sequencia de perdas acima do limite: "
                f"{metrics.max_consecutive_losses} > {self.config.max_consecutive_losses}."
            )

        if reasons:
            return ReadinessResult(
                status=ReadinessStatus.NOT_READY,
                passed=False,
                reasons=reasons,
                warnings=["Resultado nao e candidato para proxima fase de validacao."],
                metrics=summary,
            )

        return ReadinessResult(
            status=ReadinessStatus.BACKTEST_OK,
            passed=True,
            reasons=[
                "Criterios conservadores de backtest foram atendidos.",
                "Resultado e apenas candidato para proxima fase de validacao.",
            ],
            warnings=[
                "BACKTEST_OK nao significa operar real.",
                "Valide em paper/testnet antes de qualquer etapa com capital real.",
            ],
            metrics=summary,
        )


def _metrics_summary(metrics: PerformanceMetrics) -> dict[str, str | int | bool]:
    return {
        "total_trades": metrics.total_trades,
        "net_pnl": str(metrics.net_pnl),
        "gross_pnl": str(metrics.gross_pnl),
        "profit_factor": str(metrics.profit_factor),
        "expectancy": str(metrics.expectancy),
        "max_drawdown_pct": str(metrics.max_drawdown_pct),
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "sample_sufficient": metrics.sample_sufficient,
    }
