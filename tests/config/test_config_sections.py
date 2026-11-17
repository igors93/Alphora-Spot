"""Structure of ``load_config`` after migration M7 (OPT-029): one section per ``AppConfig`` field."""

import ast
import dataclasses
from pathlib import Path

from config.models import AppConfig
from config.rest_weight_env import RestWeightSettingsFields
from config.sections.execution import ExecutionFields
from config.sections.intelligence import IntelligenceFields
from config.sections.market_data import (
    MarketStreamFields,
    UserDataCacheFields,
    UserDataStreamFields,
)
from config.sections.observability import ObservabilityFields
from config.sections.risk import EconomicFields, RiskGuardFields
from config.sections.runtime import RuntimePathFields, SchedulingFields
from config.sections.trading import StrategyFields, SymbolFields, TradeParameterFields
from config.snapshot_env import SnapshotSettingsFields

CONFIG_DIR = Path(__file__).resolve().parents[2] / "src" / "config"
MAX_FUNCTION_LINES = 150

SECTIONS = (
    ExecutionFields,
    IntelligenceFields,
    MarketStreamFields,
    UserDataStreamFields,
    UserDataCacheFields,
    SnapshotSettingsFields,
    RestWeightSettingsFields,
    SymbolFields,
    StrategyFields,
    TradeParameterFields,
    RiskGuardFields,
    EconomicFields,
    RuntimePathFields,
    SchedulingFields,
    ObservabilityFields,
)


def _keys(section: type) -> set[str]:
    return set(section.__required_keys__) | set(section.__optional_keys__)  # type: ignore[attr-defined]


def test_every_app_config_field_comes_from_exactly_one_section():
    owners: dict[str, list[str]] = {}
    for section in SECTIONS:
        for key in _keys(section):
            owners.setdefault(key, []).append(section.__name__)

    shared = {key: names for key, names in owners.items() if len(names) > 1}
    fields = {field.name for field in dataclasses.fields(AppConfig)} - {"env_file"}

    assert shared == {}
    assert set(owners) == fields


def test_no_config_function_is_longer_than_the_migration_limit():
    long_functions = []
    for path in sorted(CONFIG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = (node.end_lineno or node.lineno) - node.lineno + 1
                if size > MAX_FUNCTION_LINES:
                    long_functions.append(f"{path.name}:{node.lineno} {node.name} ({size})")

    assert long_functions == []
