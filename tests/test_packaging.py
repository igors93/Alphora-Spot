"""Guard the wheel contents: an installed Alphora must run without the source tree."""

import importlib
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: pytest itself depends on tomli there.
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC


@pytest.fixture(scope="module")
def setuptools_config():
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)


def test_python_package_has_no_gui_toolkit_dependency(setuptools_config):
    # The interface is the native Rust app in interface/; the wheel is the core only.
    dependencies = " ".join(setuptools_config["project"]["dependencies"]).lower()

    assert "pyside" not in dependencies
    assert setuptools_config["tool"]["setuptools"]["package-data"] == {
        "market_data.snapshot_engine": ["py.typed"],
        "resilience.retry_engine": ["py.typed"],
    }
    assert not (PACKAGE / "desktop" / "UI").exists()


def test_desktop_bridge_is_runnable_as_module():
    assert (PACKAGE / "desktop" / "bridge" / "__main__.py").is_file()
    importlib.import_module("desktop.bridge.server")


def test_wheel_installs_the_flat_source_layout(setuptools_config):
    find = setuptools_config["tool"]["setuptools"]["packages"]["find"]

    assert find["where"] == ["src"]
    assert find["namespaces"] is False
    assert set(find["include"]) == {
        f"{path.name}*" for path in SRC.iterdir() if (path / "__init__.py").is_file()
    }
    assert set(setuptools_config["tool"]["setuptools"]["py-modules"]) == {
        path.stem for path in SRC.glob("*.py") if path.name != "__init__.py"
    }
    assert not (SRC / "alphora").exists()
    assert (SRC / "market_data" / "snapshot_engine" / "__init__.py").is_file()
    assert (SRC / "resilience" / "retry_engine" / "__init__.py").is_file()
    assert not (SRC / "coalestra").exists()
    assert not (SRC / "relinker").exists()
    dependencies = " ".join(setuptools_config["project"]["dependencies"]).lower()
    assert "coalestra" not in dependencies
    assert "relinker" not in dependencies
    assert not (SRC / "models").exists()
    assert (ROOT / "models" / "ai" / ".gitkeep").is_file()
    assert not (ROOT / "research").exists()


@pytest.mark.parametrize(
    "script",
    ["alphora-engine", "alphora-research", "alphora-backtest"],
)
def test_console_scripts_resolve_to_callables(setuptools_config, script):
    target = setuptools_config["project"]["scripts"][script]
    module_name, attribute = target.split(":")

    assert module_name.split(".", 1)[0] in {"engine", "research", "backtesting"}
    assert callable(getattr(importlib.import_module(module_name), attribute))


def test_source_checkout_main_py_delegates_to_the_packaged_engine():
    import engine as alphora_engine
    import main

    assert main.run is alphora_engine.run
