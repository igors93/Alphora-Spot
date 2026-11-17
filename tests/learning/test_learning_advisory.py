from trading.learning_advisory import (
    ADVISORY_STATUS_NO_RELEVANT_RECOMMENDATION,
    ADVISORY_STATUS_NO_REPORT,
    ADVISORY_STATUS_WARNING,
    LearningAdvisoryEngine,
)
from trading.learning_recommendations import (
    ACTION_COLLECT_MORE_DATA,
    ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW,
    ACTION_REVIEW_GLOBAL_RISK,
    SCOPE_GLOBAL,
    SCOPE_SYMBOL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
)


def test_learning_advisory_handles_missing_report():
    evaluation = LearningAdvisoryEngine().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        report_payload=None,
    )

    assert evaluation.status == ADVISORY_STATUS_NO_REPORT
    assert evaluation.shadow_only is True
    assert evaluation.can_use_for_execution is False
    assert evaluation.would_block_trade is False


def test_learning_advisory_applies_global_warning_to_symbol():
    evaluation = LearningAdvisoryEngine().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        report_payload={
            "status": "READY",
            "total_outcomes": 30,
            "recommendations": [
                {
                    "action": ACTION_REVIEW_GLOBAL_RISK,
                    "severity": SEVERITY_WARNING,
                    "reason": "O PnL liquido total esta negativo",
                    "scope": SCOPE_GLOBAL,
                    "confidence": 75,
                }
            ],
        },
    )

    assert evaluation.status == ADVISORY_STATUS_WARNING
    assert evaluation.would_block_trade is True
    assert evaluation.relevant_recommendations_count == 1
    assert evaluation.total_outcomes == 30


def test_learning_advisory_applies_symbol_warning_only_to_matching_symbol():
    engine = LearningAdvisoryEngine()
    report = {
        "status": "READY",
        "total_outcomes": 30,
        "recommendations": [
            {
                "action": ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW,
                "severity": SEVERITY_WARNING,
                "reason": "Simbolo com PnL negativo e baixa taxa de acerto",
                "scope": SCOPE_SYMBOL,
                "symbol": "ETHUSDT",
                "confidence": 70,
            }
        ],
    }

    btc = engine.evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        report_payload=report,
    )
    eth = engine.evaluate(
        symbol="ETHUSDT",
        action="OPEN_LONG",
        report_payload=report,
    )

    assert btc.status == ADVISORY_STATUS_NO_RELEVANT_RECOMMENDATION
    assert btc.would_block_trade is False
    assert eth.status == ADVISORY_STATUS_WARNING
    assert eth.would_reduce_priority is True
    assert eth.would_block_trade is True


def test_learning_advisory_keeps_collect_more_data_informational():
    evaluation = LearningAdvisoryEngine().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        report_payload={
            "status": "INSUFFICIENT_DATA",
            "total_outcomes": 2,
            "recommendations": [
                {
                    "action": ACTION_COLLECT_MORE_DATA,
                    "severity": SEVERITY_INFO,
                    "reason": "Ainda existem poucos outcomes",
                    "scope": SCOPE_GLOBAL,
                    "confidence": 30,
                }
            ],
        },
    )

    assert evaluation.status == "INFO"
    assert evaluation.would_block_trade is False
    assert evaluation.relevant_recommendations_count == 1
