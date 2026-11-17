from decimal import Decimal

import pytest

from scanner.signal_score import SignalPriority, SignalScore, priority_from_score


@pytest.mark.parametrize(
    ("score", "priority"),
    [
        (0, SignalPriority.COLD),
        (39, SignalPriority.COLD),
        (40, SignalPriority.WARM),
        (69, SignalPriority.WARM),
        (70, SignalPriority.HOT),
        (89, SignalPriority.HOT),
        (90, SignalPriority.READY),
        (100, SignalPriority.READY),
    ],
)
def test_priority_from_score_boundaries(score, priority):
    assert priority_from_score(score) is priority


def test_signal_score_clamps_score_and_normalizes_symbol():
    low = SignalScore(symbol="btcusdt", score=-10, reason="baixo")
    high = SignalScore(symbol="ethusdt", score=130, reason="alto")

    assert low.score == 0
    assert low.priority is SignalPriority.COLD
    assert low.symbol == "BTCUSDT"
    assert high.score == 100
    assert high.priority is SignalPriority.READY
    assert high.symbol == "ETHUSDT"


def test_signal_score_rejects_incoherent_priority():
    with pytest.raises(ValueError, match="priority incoerente"):
        SignalScore(symbol="BTCUSDT", score=95, priority=SignalPriority.COLD, reason="x")


def test_signal_score_reason_default_never_empty():
    score = SignalScore(symbol="BTCUSDT", score=1, reason="")

    assert score.reason
    assert score.suggested_next_check_seconds == 90.0


def test_signal_score_accepts_text_priority_when_coherent():
    score = SignalScore(symbol="BTCUSDT", score=45, priority="WARM", reason="ok")

    assert score.priority is SignalPriority.WARM


def test_signal_score_accepts_decimal_score_and_confidence():
    score = SignalScore(
        symbol="BTCUSDT", score=Decimal("90.5"), confidence=Decimal("101"), reason="ok"
    )

    assert score.score == 90
    assert score.priority is SignalPriority.READY
    assert score.confidence == 100


def test_signal_score_preserves_structured_metadata():
    score = SignalScore(
        symbol="BTCUSDT",
        score=95,
        reason="ok",
        metadata={"model_source": "GLOBAL", "long_score": 0.42},
    )

    assert score.metadata == {"model_source": "GLOBAL", "long_score": 0.42}
