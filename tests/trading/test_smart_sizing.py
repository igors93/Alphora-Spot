from decimal import Decimal

from config.smart_settings import SmartSizingMinNotionalPolicy, SmartSizingSettings
from governor.smart_sizing import SmartSizingEngine, SmartSizingInput
from operational_module_modes import SmartSizingMode


def _input(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": "OPEN_LONG",
        "base_notional_usdt": Decimal("60"),
        "entry_price": Decimal("100"),
        "reward_risk_liquido": Decimal("2"),
        "cost_to_reward_ratio": Decimal("0.10"),
        "risk_abs": Decimal("1"),
        "reward_abs": Decimal("2"),
        "strategy_score": 85,
        "min_required_notional_usdt": Decimal("5"),
        "mode": SmartSizingMode.OFF,
    }
    payload.update(overrides)
    return SmartSizingInput(**payload)


def test_smart_sizing_off_keeps_base_notional():
    decision = SmartSizingEngine().evaluate(_input(mode=SmartSizingMode.OFF))

    assert decision.final_notional_usdt == Decimal("60")
    assert decision.affected_result is False
    assert decision.blocked is False
    assert decision.diagnostics["min_notional_policy"] == "BLOCK"


def test_smart_sizing_default_settings_keep_previous_behavior():
    settings = SmartSizingSettings()
    decision = SmartSizingEngine(settings=settings).evaluate(
        _input(mode=SmartSizingMode.REDUCE_ONLY, reward_risk_liquido=Decimal("0.8"))
    )

    assert decision.final_notional_usdt == Decimal("30.0")
    assert decision.sizing_multiplier == Decimal("0.5")


def test_smart_sizing_observe_calculates_without_affecting_result():
    decision = SmartSizingEngine().evaluate(
        _input(mode=SmartSizingMode.OBSERVE, reward_risk_liquido=Decimal("0.8"))
    )

    assert decision.recommended_notional_usdt == Decimal("30.0")
    assert decision.final_notional_usdt == Decimal("60")
    assert decision.affected_result is False


def test_smart_sizing_shadow_calculates_without_affecting_result():
    decision = SmartSizingEngine().evaluate(
        _input(mode=SmartSizingMode.SHADOW, cost_to_reward_ratio=Decimal("0.60"))
    )

    assert decision.recommended_notional_usdt == Decimal("30.0")
    assert decision.final_notional_usdt == Decimal("60")
    assert decision.reason == "SMART_SIZING_SHADOW"
    assert decision.diagnostics["would_have_reduced_in_shadow"] is True


def test_smart_sizing_reduce_only_reduces_when_risk_is_high():
    decision = SmartSizingEngine().evaluate(
        _input(
            mode=SmartSizingMode.REDUCE_ONLY,
            reward_risk_liquido=Decimal("0.8"),
            consecutive_losses=3,
        )
    )

    assert decision.final_notional_usdt == Decimal("30.0")
    assert decision.reduced is True
    assert decision.affected_result is True


def test_smart_sizing_reduce_only_never_increases():
    decision = SmartSizingEngine().evaluate(
        _input(mode=SmartSizingMode.REDUCE_ONLY, reward_risk_liquido=Decimal("4"))
    )

    assert decision.recommended_notional_usdt == Decimal("60")
    assert decision.final_notional_usdt == Decimal("60")
    assert decision.affected_result is False


def test_smart_sizing_active_v1_never_increases_above_base():
    decision = SmartSizingEngine(
        settings=SmartSizingSettings(full_size_multiplier=Decimal("1.25"))
    ).evaluate(_input(mode=SmartSizingMode.ACTIVE, reward_risk_liquido=Decimal("4")))

    assert decision.recommended_notional_usdt == Decimal("60")
    assert decision.final_notional_usdt == Decimal("60")
    assert decision.affected_result is False


def test_smart_sizing_active_respects_exposure_limit():
    decision = SmartSizingEngine().evaluate(
        _input(mode=SmartSizingMode.ACTIVE, max_exposure_usdt=Decimal("40"))
    )

    assert decision.final_notional_usdt == Decimal("40")
    assert decision.affected_result is True


def test_smart_sizing_blocks_when_reduction_violates_min_notional():
    decision = SmartSizingEngine().evaluate(
        _input(
            mode=SmartSizingMode.REDUCE_ONLY,
            base_notional_usdt=Decimal("6"),
            reward_risk_liquido=Decimal("0.8"),
            min_required_notional_usdt=Decimal("5"),
        )
    )

    assert decision.blocked is True
    assert decision.reason == "SMART_SIZING_BELOW_MIN_NOTIONAL"
    assert decision.diagnostics["min_notional_adjustment"] == "BLOCK"


def test_smart_sizing_floor_to_min_uses_min_when_not_above_base():
    decision = SmartSizingEngine(
        settings=SmartSizingSettings(min_notional_policy=SmartSizingMinNotionalPolicy.FLOOR_TO_MIN)
    ).evaluate(
        _input(
            mode=SmartSizingMode.REDUCE_ONLY,
            base_notional_usdt=Decimal("6"),
            reward_risk_liquido=Decimal("0.8"),
            min_required_notional_usdt=Decimal("5"),
        )
    )

    assert decision.blocked is False
    assert decision.final_notional_usdt == Decimal("5")
    assert decision.reason == "SMART_SIZING_FLOORED_TO_MIN_NOTIONAL"
    assert decision.affected_result is True


def test_smart_sizing_fallback_to_base_when_reduction_below_min():
    decision = SmartSizingEngine(
        settings=SmartSizingSettings(
            min_notional_policy=SmartSizingMinNotionalPolicy.FALLBACK_TO_BASE
        )
    ).evaluate(
        _input(
            mode=SmartSizingMode.REDUCE_ONLY,
            base_notional_usdt=Decimal("6"),
            reward_risk_liquido=Decimal("0.8"),
            min_required_notional_usdt=Decimal("5"),
        )
    )

    assert decision.blocked is False
    assert decision.final_notional_usdt == Decimal("6")
    assert decision.affected_result is False
    assert decision.reason == "SMART_SIZING_MIN_NOTIONAL_FALLBACK_TO_BASE"


def test_smart_sizing_uses_planned_exit_reward_risk_when_available():
    decision = SmartSizingEngine().evaluate(
        _input(
            mode=SmartSizingMode.REDUCE_ONLY,
            reward_risk_liquido=None,
            planned_exit={
                "reward_risk_liquido": "0.9",
                "cost_to_reward_ratio": "0.1",
                "risk_abs": "1",
                "reward_abs": "2",
            },
        )
    )

    assert decision.final_notional_usdt == Decimal("30.0")
    assert decision.diagnostics["reward_risk_liquido"] == "0.9"


def test_smart_sizing_diagnostics_include_reduction_policy_and_reasons():
    decision = SmartSizingEngine().evaluate(
        _input(mode=SmartSizingMode.REDUCE_ONLY, reward_risk_liquido=Decimal("0.8"))
    )

    assert decision.diagnostics["reduction_pct"] == "0.50"
    assert decision.diagnostics["reduction_amount_usdt"] == "30.00"
    assert decision.diagnostics["min_notional_policy"] == "BLOCK"
    assert "LOW_REWARD_RISK" in decision.diagnostics["applied_reasons"]


def test_smart_sizing_reduces_at_elevated_and_high_drawdown_thresholds():
    elevated = SmartSizingEngine().evaluate(
        _input(
            mode=SmartSizingMode.ACTIVE,
            current_drawdown=Decimal("0.05"),
            require_current_drawdown=True,
        )
    )
    high = SmartSizingEngine().evaluate(
        _input(
            mode=SmartSizingMode.ACTIVE,
            current_drawdown=Decimal("0.10"),
            require_current_drawdown=True,
        )
    )

    assert elevated.final_notional_usdt == Decimal("45.00")
    assert "ELEVATED_DRAWDOWN" in elevated.diagnostics["applied_reasons"]
    assert high.final_notional_usdt == Decimal("15.00")
    assert "HIGH_DRAWDOWN" in high.diagnostics["applied_reasons"]


def test_smart_sizing_blocks_when_required_drawdown_is_unavailable_in_active_modes():
    for mode in (SmartSizingMode.ACTIVE, SmartSizingMode.REDUCE_ONLY):
        decision = SmartSizingEngine().evaluate(
            _input(
                mode=mode,
                current_drawdown=None,
                require_current_drawdown=True,
                drawdown_diagnostics={"available": False, "reason": "WALLET_BALANCE_UNAVAILABLE"},
            )
        )

        assert decision.blocked is True
        assert decision.final_notional_usdt == Decimal("0")
        assert decision.reason == "SMART_SIZING_DRAWDOWN_UNAVAILABLE"
        assert decision.diagnostics["drawdown"]["reason"] == "WALLET_BALANCE_UNAVAILABLE"


def test_smart_sizing_only_reports_unavailable_drawdown_in_observation_modes():
    for mode in (SmartSizingMode.OBSERVE, SmartSizingMode.SHADOW):
        decision = SmartSizingEngine().evaluate(
            _input(mode=mode, current_drawdown=None, require_current_drawdown=True)
        )

        assert decision.blocked is False
        assert decision.final_notional_usdt == Decimal("60")
        assert decision.affected_result is False
        assert "DRAWDOWN_UNAVAILABLE" in decision.diagnostics["applied_reasons"]


def test_smart_sizing_without_drawdown_requirement_keeps_previous_behavior():
    decision = SmartSizingEngine().evaluate(
        _input(mode=SmartSizingMode.ACTIVE, current_drawdown=None)
    )

    assert decision.blocked is False
    assert decision.final_notional_usdt == Decimal("60")
