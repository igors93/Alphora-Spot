from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from governor.alphora_governor import AlphoraGovernor
from tests.fakes import FakeFuturesClient
from tests.governor.factory import build_governor_for_tests


def test_composition_root_injects_unique_shared_components(config_factory):
    config = replace(config_factory(), market_ws_enabled=False)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeFuturesClient(),
        symbol_configs=[profile],
    )

    execution = governor.execution_engine
    symbol_engine = execution._get_engine(profile)

    assert execution.governor_journal is governor.events_journal
    assert execution.trade_audit_recorder is governor.trade_audit_recorder
    assert symbol_engine.trade_audit_recorder is governor.trade_audit_recorder
    assert execution.state_reconciliation_engine is governor.state_reconciliation_engine
    assert symbol_engine.state_reconciliation_engine is governor.state_reconciliation_engine
    assert symbol_engine.risk_guard is execution.risk_guard_for_symbol(profile)
    assert execution.risk_guard_for_symbol(profile) is execution.risk_guard_for_symbol(profile)
    assert symbol_engine.paper_journal.path == execution._paper_journal_path(profile)
    journals = governor.journals
    assert execution.journals is journals
    assert governor.events_journal is journals.journal(governor.governor_events_path)
    assert governor.score_journal is journals.journal(governor.score_journal.path)
    assert governor.trade_audit_recorder.journal is journals.journal(config.trade_audit_path)
    assert symbol_engine.journal is journals.journal(profile.journal_path)
    assert symbol_engine.paper_journal is journals.journal(execution._paper_journal_path(profile))
    assert len(dict(governor.components.items())) >= 70
    with pytest.raises(FrozenInstanceError):
        governor.components.data = governor.components.state


def test_unique_risk_guard_reads_the_paper_counter_used_by_opening_path(config_factory):
    config = replace(
        config_factory(execution_mode=ExecutionMode.PAPER_LIVE, max_daily_trades=1),
        market_ws_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeFuturesClient(),
        symbol_configs=[profile],
    )
    execution = governor.execution_engine
    symbol_engine = execution._get_engine(profile)
    symbol_engine.paper_journal.append("paper_position_opened", symbol=profile.symbol)

    evaluation_guard = execution.risk_guard_for_symbol(profile)
    opening_guard = symbol_engine.risk_guard
    evaluation_decision = evaluation_guard.can_open_position(notional=Decimal("10"))
    opening_decision = opening_guard.can_open_position(notional=Decimal("10"))

    assert evaluation_guard is opening_guard
    assert evaluation_decision.allowed is False
    assert evaluation_decision.reason == "max_daily_trades"
    assert opening_decision.allowed is False
    assert opening_decision.reason == evaluation_decision.reason
    assert opening_decision.metrics["opened_trades"] == 1


def test_test_factory_uses_composition_root_and_validates_overrides(config_factory):
    config = replace(config_factory(), market_ws_enabled=False)
    replacement = object()
    governor = build_governor_for_tests(
        config=config,
        client=FakeFuturesClient(),
        hot_symbol_worker=replacement,
    )

    assert governor.hot_symbol_worker is replacement
    assert governor.components is not None

    with pytest.raises(AttributeError, match="Unknown Governor test override"):
        build_governor_for_tests(
            config=config,
            client=FakeFuturesClient(),
            not_a_component=replacement,
        )
