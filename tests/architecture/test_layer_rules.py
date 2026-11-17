"""Keep imports flowing from higher architecture layers to lower ones.

Layers are defined in tools/architecture_metrics.py (LAYERS). Known violations are
listed in layer_exceptions.json with the migration phase that removes them; that
list may only shrink.
"""

import json
import warnings
from pathlib import Path

import pytest

from tools.architecture_metrics import collect

EXCEPTIONS_PATH = Path(__file__).with_name("layer_exceptions.json")


@pytest.fixture(scope="module")
def report() -> dict:
    return collect()


def test_every_top_level_package_has_a_layer(report: dict) -> None:
    assert report["unlayered_packages"] == [], "Add the new packages to LAYERS"


def test_no_package_cycles(report: dict) -> None:
    assert report["package_cycles"] == []


def test_imports_respect_the_layer_direction(report: dict) -> None:
    allowed = json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))
    violations = report["layer_violations"]

    new = {edge: sites for edge, sites in violations.items() if edge not in allowed}
    assert not new, f"Imports point to a higher layer: {new}"

    fixed = sorted(set(allowed) - set(violations))
    if fixed:
        warnings.warn(
            f"Remove fixed entries from tests/architecture/layer_exceptions.json: {fixed}",
            stacklevel=1,
        )
