"""Tests for Coalestra 0.6.3 operational APIs.

Verifies:
- require_capabilities() succeeds with degraded_snapshot_results and partitioned_request_builds.
- New imports (RequestDegradationPolicy, SnapshotResult, SnapshotResultState) are available.
- Degradation policy helpers produce correct RequestDegradationPolicy instances.
- SnapshotProviderSettings validates new operational fields.
- The snapshot engine is shipped in the Alphora source tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from market_data.snapshot_engine import (
    RequestDegradationPolicy,
    require_capabilities,
)
from market_data.snapshot_provider import (
    SnapshotProvider,
    SnapshotProviderSettings,
)

# ---------------------------------------------------------------------------
# Embedded source
# ---------------------------------------------------------------------------


def test_requirements_txt_has_no_snapshot_engine_dependency() -> None:
    txt = (Path(__file__).parents[2] / "requirements.txt").read_text()
    assert not any(line.strip().lower().startswith("coalestra") for line in txt.splitlines())


def test_pyproject_toml_packages_embedded_snapshot_engine() -> None:
    txt = (Path(__file__).parents[2] / "pyproject.toml").read_text()
    assert '"market_data*"' in txt
    assert '"coalestra==0.6.4"' not in txt
    assert (
        Path(__file__).parents[2] / "src" / "market_data" / "snapshot_engine" / "__init__.py"
    ).is_file()


# ---------------------------------------------------------------------------
# New capability check
# ---------------------------------------------------------------------------


def test_require_capabilities_063_succeeds() -> None:
    require_capabilities(
        features=[
            "degraded_snapshot_results",
            "partitioned_request_builds",
        ],
    )


# ---------------------------------------------------------------------------
# New imports are accessible from snapshot_provider
# ---------------------------------------------------------------------------


def test_new_types_importable_from_snapshot_provider() -> None:
    from market_data.snapshot_provider import (
        RequestDegradationPolicy,
        SnapshotResult,
        SnapshotResultState,
    )

    assert RequestDegradationPolicy is not None
    assert SnapshotResult is not None
    assert SnapshotResultState is not None


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_settings_defaults() -> None:
    s = SnapshotProviderSettings()
    assert s.operational_degradation_enabled is True
    assert s.evaluation_degraded_min_resources == 3
    assert s.baseline_degraded_min_resources == 1
    assert s.risk_reduction_degraded_min_resources == 2
    assert s.partitioned_symbol_request_concurrency == 4
    assert s.partitioned_symbol_deadline_seconds is None


def test_settings_evaluation_degraded_min_resources_must_be_positive() -> None:
    with pytest.raises(ValueError, match="evaluation_degraded_min_resources"):
        SnapshotProviderSettings(evaluation_degraded_min_resources=0)


def test_settings_baseline_degraded_min_resources_must_be_positive() -> None:
    with pytest.raises(ValueError, match="baseline_degraded_min_resources"):
        SnapshotProviderSettings(baseline_degraded_min_resources=0)


def test_settings_risk_reduction_degraded_min_resources_must_be_positive() -> None:
    with pytest.raises(ValueError, match="risk_reduction_degraded_min_resources"):
        SnapshotProviderSettings(risk_reduction_degraded_min_resources=0)


def test_settings_partitioned_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="partitioned_symbol_request_concurrency"):
        SnapshotProviderSettings(partitioned_symbol_request_concurrency=0)


def test_settings_deadline_none_allowed() -> None:
    s = SnapshotProviderSettings(partitioned_symbol_deadline_seconds=None)
    assert s.partitioned_symbol_deadline_seconds is None


def test_settings_deadline_positive_allowed() -> None:
    s = SnapshotProviderSettings(partitioned_symbol_deadline_seconds=5.0)
    assert s.partitioned_symbol_deadline_seconds == 5.0


def test_settings_deadline_zero_raises() -> None:
    with pytest.raises(ValueError, match="partitioned_symbol_deadline_seconds"):
        SnapshotProviderSettings(partitioned_symbol_deadline_seconds=0.0)


def test_settings_deadline_negative_raises() -> None:
    with pytest.raises(ValueError, match="partitioned_symbol_deadline_seconds"):
        SnapshotProviderSettings(partitioned_symbol_deadline_seconds=-1.0)


# ---------------------------------------------------------------------------
# Degradation policies
# ---------------------------------------------------------------------------


class FakeClient:
    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100"}

    def exchange_info(self, *, timeout=None):
        return {"symbols": []}

    def position_risk(self, symbol=None, *, timeout=None):
        return []

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": []}


def _make_provider(**kwargs) -> SnapshotProvider:
    return SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(**kwargs),
    )


def test_baseline_degradation_policy_degraded_mode() -> None:
    provider = _make_provider(
        operational_degradation_enabled=True, baseline_degraded_min_resources=1
    )
    policy = provider.policies.baseline_degradation()
    assert isinstance(policy, RequestDegradationPolicy)
    assert not policy.fail_closed


def test_baseline_degradation_policy_fail_closed_when_disabled() -> None:
    provider = _make_provider(operational_degradation_enabled=False)
    policy = provider.policies.baseline_degradation()
    assert isinstance(policy, RequestDegradationPolicy)
    assert policy.fail_closed


def test_evaluation_degradation_policy_degraded_mode() -> None:
    provider = _make_provider(
        operational_degradation_enabled=True,
        evaluation_degraded_min_resources=3,
    )
    policy = provider.policies.evaluation_degradation()
    assert isinstance(policy, RequestDegradationPolicy)
    assert not policy.fail_closed


def test_evaluation_degradation_policy_fail_closed_when_disabled() -> None:
    provider = _make_provider(operational_degradation_enabled=False)
    policy = provider.policies.evaluation_degradation()
    assert policy.fail_closed


def test_risk_reduction_degradation_policy_risk_reduction_mode() -> None:
    provider = _make_provider(
        operational_degradation_enabled=True,
        risk_reduction_degraded_min_resources=2,
    )
    policy = provider.policies.risk_reduction_degradation()
    assert isinstance(policy, RequestDegradationPolicy)
    assert not policy.fail_closed


def test_risk_reduction_degradation_policy_fail_closed_when_disabled() -> None:
    provider = _make_provider(operational_degradation_enabled=False)
    policy = provider.policies.risk_reduction_degradation()
    assert policy.fail_closed


def test_provider_has_last_snapshot_result_attribute() -> None:
    provider = _make_provider()
    assert hasattr(provider, "last_snapshot_result")
    assert provider.last_snapshot_result is None


def test_provider_close(close_providers) -> None:
    provider = _make_provider()
    provider.close()


@pytest.fixture(autouse=True)
def close_providers():
    yield
    from market_data.snapshot_provider import close_live_snapshot_providers_for_testing

    close_live_snapshot_providers_for_testing()
