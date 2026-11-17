import os
from pathlib import Path

import pytest

from config import (
    build_effective_config_summary,
    format_effective_config_summary,
    load_config,
)
from config import env_file as env_file_module

ROOT = Path(__file__).resolve().parents[2]


def _write_env(directory: Path, key: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".env"
    path.write_text(
        f"BINANCE_FUTURES_API_KEY={key}\n"
        "BINANCE_FUTURES_API_SECRET=file-secret\n"
        "ALPHORA_EXECUTION_MODE=DRY_RUN\n"
        "ALPHORA_ENVIRONMENT=TESTNET\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    # load_dotenv writes into os.environ; a private copy keeps tests independent.
    monkeypatch.setattr(
        os,
        "environ",
        {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("ALPHORA_", "BINANCE_FUTURES_"))
        },
    )
    monkeypatch.setattr(env_file_module, "_source_checkout_root", lambda: None)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return tmp_path


def test_env_file_variable_selects_an_explicit_file(isolated_env, monkeypatch):
    explicit = _write_env(isolated_env / "secrets", "explicit-key")
    _write_env(isolated_env / "workdir", "cwd-key")
    monkeypatch.setenv("ALPHORA_ENV_FILE", str(explicit))

    config = load_config()

    assert config.api_key == "explicit-key"
    assert config.env_file == str(explicit.resolve())


def test_missing_explicit_env_file_fails_by_name(isolated_env, monkeypatch):
    monkeypatch.setenv("ALPHORA_ENV_FILE", str(isolated_env / "missing.env"))

    with pytest.raises(ValueError, match="ALPHORA_ENV_FILE aponta para um arquivo inexistente"):
        load_config()


def test_workspace_env_file_wins_over_working_directory(isolated_env, monkeypatch):
    workspace = isolated_env / "workspace"
    expected = _write_env(workspace, "workspace-key")
    _write_env(isolated_env / "workdir", "cwd-key")
    monkeypatch.setenv("ALPHORA_PROJECT_ROOT", str(workspace))

    config = load_config()

    assert config.api_key == "workspace-key"
    assert config.env_file == str(expected.resolve())


def test_working_directory_env_file_is_read_without_a_checkout(isolated_env):
    # The installed-wheel case: no source checkout around the installed code.
    expected = _write_env(isolated_env / "workdir", "cwd-key")

    config = load_config()

    assert config.api_key == "cwd-key"
    assert config.env_file == str(expected.resolve())


def test_source_checkout_env_file_is_the_last_fallback(isolated_env, monkeypatch):
    checkout = isolated_env / "checkout"
    expected = _write_env(checkout, "checkout-key")
    monkeypatch.setattr(env_file_module, "_source_checkout_root", lambda: checkout)

    config = load_config()

    assert config.api_key == "checkout-key"
    assert config.env_file == str(expected.resolve())


def test_process_environment_wins_over_the_env_file(isolated_env, monkeypatch):
    _write_env(isolated_env / "workdir", "cwd-key")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "process-key")

    config = load_config()

    assert config.api_key == "process-key"
    assert config.api_secret == "file-secret"


def test_no_env_file_is_reported_as_empty(isolated_env, monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")

    config = load_config()

    assert config.env_file == ""


def test_startup_summary_reports_the_loaded_env_file(isolated_env):
    expected = _write_env(isolated_env / "workdir", "cwd-key")

    summary = format_effective_config_summary(build_effective_config_summary(load_config()))

    assert f"- env_file: {expected.resolve()}" in summary.splitlines()


def test_source_checkout_root_is_this_repository():
    assert env_file_module._source_checkout_root() == ROOT
