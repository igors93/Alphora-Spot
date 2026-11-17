from trading.learning import TradePlanLearningAnalyzer
from trading.learning_recommendations import (
    ACTION_COLLECT_MORE_DATA,
    ACTION_KEEP_ADAPTIVE_OBSERVATION,
    ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW,
    ACTION_REVIEW_ADAPTIVE_MANAGEMENT,
    ACTION_REVIEW_GLOBAL_RISK,
    REPORT_STATUS_INSUFFICIENT_DATA,
    REPORT_STATUS_READY,
    TradePlanLearningAdvisor,
)


def _outcome(
    *,
    symbol="BTCUSDT",
    trade_plan_id="tp",
    outcome_status="WIN",
    net_pnl="1",
    adaptive_adjustments_applied=0,
):
    return {
        "symbol": symbol,
        "trade_plan_id": trade_plan_id,
        "outcome_status": outcome_status,
        "net_pnl": net_pnl,
        "adaptive_adjustments_applied": adaptive_adjustments_applied,
    }


def test_learning_advisor_requests_more_data_when_sample_is_small():
    summary = TradePlanLearningAnalyzer().summarize_outcomes(
        [
            _outcome(outcome_status="WIN", net_pnl="1"),
            _outcome(outcome_status="LOSS", net_pnl="-1"),
        ]
    )

    report = TradePlanLearningAdvisor(min_total_outcomes=5).build_report(summary)

    assert report.status == REPORT_STATUS_INSUFFICIENT_DATA
    assert report.can_use_for_execution is False
    assert report.recommendations[0].action == ACTION_COLLECT_MORE_DATA


def test_learning_advisor_recommends_global_risk_review_when_pnl_is_negative():
    outcomes = [
        _outcome(symbol="BTCUSDT", outcome_status="LOSS", net_pnl="-1"),
        _outcome(symbol="BTCUSDT", outcome_status="LOSS", net_pnl="-1"),
        _outcome(symbol="BTCUSDT", outcome_status="LOSS", net_pnl="-1"),
        _outcome(symbol="BTCUSDT", outcome_status="LOSS", net_pnl="-1"),
        _outcome(symbol="BTCUSDT", outcome_status="WIN", net_pnl="0.5"),
    ]
    summary = TradePlanLearningAnalyzer().summarize_outcomes(outcomes)

    report = TradePlanLearningAdvisor(
        min_total_outcomes=5,
        min_symbol_outcomes=5,
    ).build_report(summary)

    actions = {recommendation.action for recommendation in report.recommendations}

    assert report.status == REPORT_STATUS_READY
    assert report.can_use_for_execution is False
    assert ACTION_REVIEW_GLOBAL_RISK in actions
    assert ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW in actions


def test_learning_advisor_flags_adaptive_management_when_it_performs_worse():
    outcomes = [
        _outcome(outcome_status="LOSS", net_pnl="-1.00", adaptive_adjustments_applied=1),
        _outcome(outcome_status="LOSS", net_pnl="-0.80", adaptive_adjustments_applied=1),
        _outcome(outcome_status="WIN", net_pnl="0.50", adaptive_adjustments_applied=0),
        _outcome(outcome_status="WIN", net_pnl="0.40", adaptive_adjustments_applied=0),
        _outcome(outcome_status="WIN", net_pnl="0.30", adaptive_adjustments_applied=0),
    ]
    summary = TradePlanLearningAnalyzer().summarize_outcomes(outcomes)

    report = TradePlanLearningAdvisor(
        min_total_outcomes=5,
        min_bucket_outcomes=2,
    ).build_report(summary)

    actions = {recommendation.action for recommendation in report.recommendations}

    assert ACTION_REVIEW_ADAPTIVE_MANAGEMENT in actions


def test_learning_advisor_keeps_adaptive_observation_when_it_performs_better():
    outcomes = [
        _outcome(outcome_status="WIN", net_pnl="1.00", adaptive_adjustments_applied=1),
        _outcome(outcome_status="WIN", net_pnl="0.80", adaptive_adjustments_applied=1),
        _outcome(outcome_status="LOSS", net_pnl="-0.20", adaptive_adjustments_applied=0),
        _outcome(outcome_status="WIN", net_pnl="0.10", adaptive_adjustments_applied=0),
        _outcome(outcome_status="WIN", net_pnl="0.20", adaptive_adjustments_applied=0),
    ]
    summary = TradePlanLearningAnalyzer().summarize_outcomes(outcomes)

    report = TradePlanLearningAdvisor(
        min_total_outcomes=5,
        min_bucket_outcomes=2,
    ).build_report(summary)

    actions = {recommendation.action for recommendation in report.recommendations}

    assert ACTION_KEEP_ADAPTIVE_OBSERVATION in actions
