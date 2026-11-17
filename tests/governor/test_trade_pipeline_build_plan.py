"""Tests that TradePipeline.build_plan is the canonical operational method
and build_shadow_plan is a backward-compatible alias for the same result.
"""

import inspect

from governor.trade_pipeline import TradePipeline


def test_build_plan_exists_and_is_callable():
    pipeline = TradePipeline()
    assert hasattr(pipeline, "build_plan")
    assert callable(pipeline.build_plan)


def test_build_shadow_plan_is_alias_for_build_plan():
    """build_shadow_plan must delegate to build_plan."""
    pipeline = TradePipeline()
    src = inspect.getsource(pipeline.build_shadow_plan)
    assert "build_plan" in src, "build_shadow_plan should delegate to build_plan"
