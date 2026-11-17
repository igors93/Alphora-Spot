"""
Regression test for the PEP 561 typed-package marker.

The project declares `Typing :: Typed`, so the source marker must exist and be
included in built wheels.
"""

from pathlib import Path


def test_py_typed_marker_exists() -> None:
    """The source tree must contain the PEP 561 marker."""
    project_root = Path(__file__).resolve().parents[3]
    marker = project_root / "src" / "resilience" / "retry_engine" / "py.typed"

    assert marker.is_file(), f"missing typed-package marker: {marker}"
