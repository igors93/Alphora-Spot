from __future__ import annotations

import math
import random
from decimal import Decimal
from statistics import NormalDist
from typing import Any

from backtesting.domain.realism import BacktestValidationSettings
from domain.trade_result import TradeResult


def build_statistical_validation(
    trades: list[TradeResult],
    *,
    initial_balance: Decimal,
    settings: BacktestValidationSettings,
) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda trade: (trade.exit_time, trade.symbol))
    pnls = [trade.net_pnl for trade in ordered]
    returns = [float(trade.net_pnl / trade.notional) for trade in ordered if trade.notional > 0]
    bootstrap = _block_bootstrap(
        pnls,
        samples=settings.bootstrap_samples,
        confidence=float(settings.confidence_level),
        seed=settings.random_seed,
    )
    win_interval = _wilson_interval(
        sum(1 for value in pnls if value > 0),
        len(pnls),
        confidence=float(settings.confidence_level),
    )
    probabilistic_sharpe = _probabilistic_sharpe_ratio(
        returns,
        benchmark=0.0,
    )
    deflated_sharpe = _deflated_sharpe_ratio(
        returns,
        parameter_trials=settings.parameter_trials,
    )
    risk_of_ruin = _risk_of_ruin(
        pnls,
        initial_balance=initial_balance,
        samples=settings.bootstrap_samples,
        seed=settings.random_seed + 1,
        horizon_trades=settings.risk_of_ruin_horizon_trades,
    )
    walk_forward = _walk_forward_stability(
        ordered,
        folds=settings.walk_forward_folds,
    )
    stress = _execution_stress_tests(ordered, settings=settings)
    gates = {
        "expectancy_ci_positive": Decimal(str(bootstrap["expectancy"]["lower"])) > 0,
        "profit_factor_ci_above_one": Decimal(str(bootstrap["profit_factor"]["lower"])) > 1,
        "probabilistic_sharpe_passed": (
            Decimal(str(probabilistic_sharpe)) >= settings.min_probabilistic_sharpe
        ),
        "deflated_sharpe_passed": (Decimal(str(deflated_sharpe)) >= settings.min_deflated_sharpe),
        "risk_of_ruin_passed": Decimal(str(risk_of_ruin)) <= settings.max_risk_of_ruin,
        "walk_forward_passed": (
            int(walk_forward["profitable_folds"])
            >= min(
                settings.min_profitable_walk_forward_folds,
                int(walk_forward["folds_with_trades"]),
            )
            and int(walk_forward["folds_with_trades"]) >= 2
        ),
        "stress_test_passed": bool(stress["all_scenarios_profitable"]),
    }
    return {
        "method": "CHRONOLOGICAL_CIRCULAR_BLOCK_BOOTSTRAP_V2",
        "sample_trades": len(ordered),
        "confidence_level": str(settings.confidence_level),
        "bootstrap_samples": settings.bootstrap_samples,
        "block_length": bootstrap["block_length"],
        "expectancy_ci": bootstrap["expectancy"],
        "profit_factor_ci": bootstrap["profit_factor"],
        "max_drawdown_ci": bootstrap["max_drawdown"],
        "win_rate_ci": win_interval,
        "probabilistic_sharpe_ratio": str(probabilistic_sharpe),
        "deflated_sharpe_ratio": str(deflated_sharpe),
        "parameter_trials": settings.parameter_trials,
        "risk_of_ruin_50pct": str(risk_of_ruin),
        "risk_of_ruin_horizon_trades": settings.risk_of_ruin_horizon_trades,
        "walk_forward": walk_forward,
        "execution_stress": stress,
        "gates": gates,
        "passed": bool(gates) and all(gates.values()),
    }


def _block_bootstrap(
    values: list[Decimal],
    *,
    samples: int,
    confidence: float,
    seed: int,
) -> dict[str, Any]:
    if not values:
        empty = {"lower": "0", "median": "0", "upper": "0"}
        return {
            "block_length": 0,
            "expectancy": dict(empty),
            "profit_factor": dict(empty),
            "max_drawdown": dict(empty),
        }
    rng = random.Random(seed)
    count = len(values)
    block_length = max(1, int(math.sqrt(count)))
    expectancy_samples: list[Decimal] = []
    profit_factor_samples: list[Decimal] = []
    drawdown_samples: list[Decimal] = []
    for _ in range(samples):
        sample: list[Decimal] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(values[(start + offset) % count] for offset in range(block_length))
        sample = sample[:count]
        expectancy_samples.append(sum(sample, Decimal("0")) / Decimal(str(count)))
        profit_factor_samples.append(_profit_factor(sample))
        drawdown_samples.append(_max_drawdown(sample))
    return {
        "block_length": block_length,
        "expectancy": _decimal_interval(expectancy_samples, confidence),
        "profit_factor": _decimal_interval(profit_factor_samples, confidence),
        "max_drawdown": _decimal_interval(drawdown_samples, confidence),
    }


def _decimal_interval(values: list[Decimal], confidence: float) -> dict[str, str]:
    ordered = sorted(values)
    alpha = max(0.0, min(1.0, (1.0 - confidence) / 2.0))
    lower = ordered[min(len(ordered) - 1, int(alpha * len(ordered)))]
    median = ordered[min(len(ordered) - 1, int(0.5 * len(ordered)))]
    upper = ordered[min(len(ordered) - 1, int((1.0 - alpha) * len(ordered)))]
    return {"lower": str(lower), "median": str(median), "upper": str(upper)}


def _wilson_interval(wins: int, total: int, *, confidence: float) -> dict[str, str]:
    if total <= 0:
        return {"lower": "0", "center": "0", "upper": "0"}
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    p = wins / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total) / denominator
    return {
        "lower": str(max(0.0, center - margin)),
        "center": str(center),
        "upper": str(min(1.0, center + margin)),
    }


def _probabilistic_sharpe_ratio(returns: list[float], *, benchmark: float) -> float:
    if len(returns) < 3:
        return 0.0
    mean, stddev, skewness, kurtosis = _moments(returns)
    if stddev <= 0:
        return 0.0
    sharpe = mean / stddev
    denominator = math.sqrt(
        max(
            1e-12,
            1.0 - skewness * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe,
        )
    )
    statistic = (sharpe - benchmark) * math.sqrt(len(returns) - 1) / denominator
    return NormalDist().cdf(statistic)


def _deflated_sharpe_ratio(returns: list[float], *, parameter_trials: int) -> float:
    if len(returns) < 3:
        return 0.0
    _, stddev, _, _ = _moments(returns)
    if stddev <= 0:
        return 0.0
    trials = max(1, int(parameter_trials))
    if trials == 1:
        benchmark = 0.0
    else:
        euler_gamma = 0.5772156649015329
        first_quantile = NormalDist().inv_cdf(1.0 - 1.0 / trials)
        second_quantile = NormalDist().inv_cdf(1.0 - 1.0 / (trials * math.e))
        benchmark = (
            (1.0 - euler_gamma) * first_quantile + euler_gamma * second_quantile
        ) / math.sqrt(len(returns) - 1)
    return _probabilistic_sharpe_ratio(returns, benchmark=benchmark)


def _moments(values: list[float]) -> tuple[float, float, float, float]:
    mean = sum(values) / len(values)
    centered = [value - mean for value in values]
    variance = sum(value * value for value in centered) / (len(values) - 1)
    stddev = math.sqrt(max(variance, 0.0))
    if stddev <= 0:
        return mean, 0.0, 0.0, 3.0
    skewness = sum((value / stddev) ** 3 for value in centered) / len(values)
    kurtosis = sum((value / stddev) ** 4 for value in centered) / len(values)
    return mean, stddev, skewness, kurtosis


def _risk_of_ruin(
    pnls: list[Decimal],
    *,
    initial_balance: Decimal,
    samples: int,
    seed: int,
    horizon_trades: int,
) -> Decimal:
    if not pnls or initial_balance <= 0:
        return Decimal("1")
    if all(value >= 0 for value in pnls):
        return Decimal("0")
    rng = random.Random(seed)
    float_pnls = [float(value) for value in pnls]
    initial_equity = float(initial_balance)
    ruin_level = initial_equity * 0.5
    block_length = max(1, int(math.sqrt(len(pnls))))
    horizon = max(len(pnls), int(horizon_trades))
    ruined = 0
    for _ in range(samples):
        equity = initial_equity
        simulated = 0
        while simulated < horizon:
            start = rng.randrange(len(float_pnls))
            for offset in range(block_length):
                equity += float_pnls[(start + offset) % len(float_pnls)]
                simulated += 1
                if equity <= ruin_level:
                    ruined += 1
                    break
                if simulated >= horizon:
                    break
            if equity <= ruin_level:
                break
    return Decimal(str(ruined)) / Decimal(str(samples))


def _walk_forward_stability(
    trades: list[TradeResult],
    *,
    folds: int,
) -> dict[str, Any]:
    if not trades:
        return {
            "folds": [],
            "folds_with_trades": 0,
            "profitable_folds": 0,
            "positive_expectancy_folds": 0,
        }
    fold_count = min(max(2, folds), len(trades))
    payloads = []
    profitable = 0
    positive_expectancy = 0
    for fold in range(fold_count):
        start = (len(trades) * fold) // fold_count
        end = (len(trades) * (fold + 1)) // fold_count
        group = trades[start:end]
        pnls = [trade.net_pnl for trade in group]
        net = sum(pnls, Decimal("0"))
        expectancy = net / Decimal(str(len(group)))
        profitable += int(net > 0)
        positive_expectancy += int(expectancy > 0)
        payloads.append(
            {
                "fold": fold + 1,
                "start": group[0].entry_time,
                "end": group[-1].exit_time,
                "trades": len(group),
                "net_pnl": str(net),
                "expectancy": str(expectancy),
                "profit_factor": str(_profit_factor(pnls)),
                "max_drawdown": str(_max_drawdown(pnls)),
            }
        )
    return {
        "folds": payloads,
        "folds_with_trades": len(payloads),
        "profitable_folds": profitable,
        "positive_expectancy_folds": positive_expectancy,
    }


def _execution_stress_tests(
    trades: list[TradeResult],
    *,
    settings: BacktestValidationSettings,
) -> dict[str, Any]:
    scenarios = []
    for multiplier in settings.stress_cost_multipliers:
        for extra_bps in settings.stress_extra_slippage_bps:
            net = Decimal("0")
            for trade in trades:
                original_cost = (
                    trade.fees
                    + trade.slippage_cost
                    + trade.spread_cost
                    + trade.funding_cost
                    + trade.liquidation_fee
                )
                extra_slippage = trade.notional * extra_bps / Decimal("10000") * Decimal("2")
                net += trade.gross_pnl - original_cost * multiplier - extra_slippage
            scenarios.append(
                {
                    "cost_multiplier": str(multiplier),
                    "extra_slippage_bps_per_fill": str(extra_bps),
                    "net_pnl": str(net),
                    "profitable": net > 0,
                }
            )
    return {
        "scenarios": scenarios,
        "worst_net_pnl": str(
            min(
                (Decimal(str(item["net_pnl"])) for item in scenarios),
                default=Decimal("0"),
            )
        ),
        "all_scenarios_profitable": bool(scenarios)
        and all(bool(item["profitable"]) for item in scenarios),
    }


def _profit_factor(values: list[Decimal]) -> Decimal:
    profit = sum((value for value in values if value > 0), Decimal("0"))
    loss = abs(sum((value for value in values if value < 0), Decimal("0")))
    if loss == 0:
        return Decimal("999999") if profit > 0 else Decimal("0")
    return profit / loss


def _max_drawdown(values: list[Decimal]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    maximum = Decimal("0")
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum
