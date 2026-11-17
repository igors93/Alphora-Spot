"""Keep the README's setup, operational commands and links executable."""

import argparse
import importlib
import os
import re
import shlex
import sys
from pathlib import Path

import pytest

from config import ExecutionMode, load_config
from tests.fakes import FakeFuturesClient

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
MARKDOWN_FILES = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CODE_OF_CONDUCT.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


def _code_blocks(language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", README, flags=re.DOTALL)


def _readme_module_commands(module_prefix: str) -> list[tuple[str, list[str]]]:
    commands = []
    for block in _code_blocks("bash"):
        for line in block.splitlines():
            command = line.split("#", 1)[0].strip()
            if command.startswith(f"python -m {module_prefix}"):
                parts = shlex.split(command)
                commands.append((parts[2], parts[3:]))
    return commands


def _emergency_flatten_arguments() -> list[list[str]]:
    return [
        arguments
        for module, arguments in _readme_module_commands("ops.emergency_flatten")
        if module == "ops.emergency_flatten"
    ]


OPS_COMMANDS = _readme_module_commands("ops.")


class _ParsedArguments(Exception):
    pass


def test_readme_documents_the_symbol_control_commands():
    modules = {module for module, _arguments in OPS_COMMANDS}

    assert {
        "ops.pause_symbol",
        "ops.resume_symbol",
        "ops.stop_symbol",
    } <= modules


@pytest.mark.parametrize(
    "module_name,arguments",
    OPS_COMMANDS,
    ids=[" ".join([module, *arguments]) for module, arguments in OPS_COMMANDS],
)
def test_every_readme_ops_command_is_accepted_by_its_parser(monkeypatch, module_name, arguments):
    module = importlib.import_module(module_name)
    original_parse_args = argparse.ArgumentParser.parse_args

    def parse_and_stop(self, args=None, namespace=None):
        # Stop right after parsing so the command's side effects never run.
        raise _ParsedArguments(original_parse_args(self, args, namespace))

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_and_stop)
    monkeypatch.setattr(sys, "argv", [module_name, *arguments])

    with pytest.raises(_ParsedArguments):
        module.main()


@pytest.mark.parametrize(
    "example",
    [_code_blocks("env")[0], (ROOT / ".env.example").read_text(encoding="utf-8")],
    ids=["readme", "env_example_file"],
)
def test_documented_env_examples_configure_the_loader(monkeypatch, example):
    variables = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    )
    monkeypatch.setattr("config.loaders.load_env_file", lambda: "")
    for name in list(os.environ):
        if name.startswith(("ALPHORA_", "BINANCE_FUTURES_", "BINANCE_SPOT_")):
            monkeypatch.delenv(name)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    config = load_config()

    assert config.api_key == variables["BINANCE_SPOT_API_KEY"]
    assert config.api_secret == variables["BINANCE_SPOT_API_SECRET"]
    assert config.execution_mode.value == variables["ALPHORA_EXECUTION_MODE"]
    assert config.environment.value == variables["ALPHORA_ENVIRONMENT"]


def test_readme_emergency_flatten_commands_parse_and_cover_every_form(monkeypatch, capsys):
    import ops.emergency_flatten as module

    calls = []
    monkeypatch.setattr(module, "emergency_flatten", lambda **kwargs: calls.append(kwargs) or {})
    for arguments in _emergency_flatten_arguments():
        monkeypatch.setattr(sys, "argv", ["ops.emergency_flatten", *arguments])
        module.main()
    capsys.readouterr()

    assert {"confirm": "", "symbol": None, "all_symbols": False} in calls
    assert {call["confirm"] for call in calls if call["symbol"] and not call["all_symbols"]} == {
        "EMERGENCY_FLATTEN",
        "EMERGENCY_FLATTEN_REAL",
    }
    assert {"confirm": "EMERGENCY_FLATTEN", "symbol": None, "all_symbols": True} in calls


@pytest.mark.parametrize(
    "mode,token",
    [
        (ExecutionMode.TESTNET_LIVE, "EMERGENCY_FLATTEN"),
        (ExecutionMode.REAL_LIVE, "EMERGENCY_FLATTEN_REAL"),
    ],
)
def test_readme_emergency_flatten_tokens_match_the_command(
    monkeypatch,
    config_factory,
    mode,
    token,
):
    from ops.emergency_flatten import emergency_flatten

    config = config_factory(execution_mode=mode)
    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr(
        "ops.emergency_flatten.make_client",
        lambda *_args, **_kwargs: FakeFuturesClient(),
    )

    preview = emergency_flatten(confirm="")

    assert preview["required_confirmation"] == token
    assert f"--confirm {token}" in README


@pytest.mark.parametrize("path", MARKDOWN_FILES, ids=lambda path: path.name)
def test_markdown_relative_links_resolve(path):
    text = path.read_text(encoding="utf-8")
    broken = [
        target
        for target in re.findall(r"\]\(([^)\s]+)\)", text)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
        and not (path.parent / target.split("#", 1)[0]).exists()
    ]

    assert broken == []
