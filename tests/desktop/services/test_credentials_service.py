from pathlib import Path

import pytest

from desktop.services.credentials_service import (
    mask_api_key,
    prepare_credentials_update,
    secure_credentials_file,
    summarize_credentials,
)


def _settings() -> dict:
    return {
        "execution_mode": "DRY_RUN",
        "testnet_api_key": "test-key-12345678",
        "testnet_api_secret": "test-secret",
        "real_api_key": "",
        "real_api_secret": "",
    }


def test_summary_exposes_only_masked_key() -> None:
    summary = summarize_credentials(_settings(), "TESTNET")

    assert summary.configured is True
    assert summary.api_key_preview == "test••••••••5678"
    assert "test-secret" not in str(summary.as_qml_map())


def test_blank_update_preserves_existing_credentials() -> None:
    current = _settings()

    result = prepare_credentials_update(current, environment="TESTNET")

    assert result.changed is False
    assert result.settings == current
    assert result.settings is not current


def test_updates_only_informed_value_when_counterpart_exists() -> None:
    result = prepare_credentials_update(
        _settings(),
        environment="TESTNET",
        api_key="replacement-key",
    )

    assert result.changed is True
    assert result.settings["testnet_api_key"] == "replacement-key"
    assert result.settings["testnet_api_secret"] == "test-secret"


def test_rejects_incomplete_credentials() -> None:
    with pytest.raises(ValueError, match="configuradas juntas"):
        prepare_credentials_update(
            _settings(),
            environment="REAL",
            api_key="real-key",
        )


def test_rejects_whitespace_in_credentials() -> None:
    with pytest.raises(ValueError, match="não pode conter espaços"):
        prepare_credentials_update(
            _settings(),
            environment="REAL",
            api_key="real key",
            api_secret="secret",
        )


def test_clear_removes_only_selected_environment() -> None:
    settings = _settings() | {
        "real_api_key": "real-key",
        "real_api_secret": "real-secret",
    }

    result = prepare_credentials_update(
        settings,
        environment="REAL",
        clear=True,
    )

    assert result.settings["real_api_key"] == ""
    assert result.settings["real_api_secret"] == ""
    assert result.settings["testnet_api_key"] == settings["testnet_api_key"]
    assert result.settings["testnet_api_secret"] == settings["testnet_api_secret"]


def test_mask_short_key_does_not_reveal_full_value() -> None:
    assert mask_api_key("abcd") == "ab••••cd"
    assert mask_api_key("") == "—"


@pytest.mark.skipif(__import__("os").name != "posix", reason="Permissões POSIX")
def test_secure_credentials_file_restricts_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".alphora_gui.local.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)

    assert secure_credentials_file(path) is True
    assert path.stat().st_mode & 0o777 == 0o600
