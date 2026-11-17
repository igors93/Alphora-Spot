from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CREDENTIAL_ENVIRONMENTS = ("TESTNET", "REAL")
_CREDENTIAL_FIELDS = {
    "TESTNET": ("testnet_api_key", "testnet_api_secret"),
    "REAL": ("real_api_key", "real_api_secret"),
}
_MAX_CREDENTIAL_LENGTH = 512


@dataclass(frozen=True, slots=True)
class CredentialSummary:
    environment: str
    configured: bool
    api_key_configured: bool
    api_secret_configured: bool
    api_key_preview: str
    status_label: str
    tone: str

    def as_qml_map(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "configured": self.configured,
            "apiKeyConfigured": self.api_key_configured,
            "apiSecretConfigured": self.api_secret_configured,
            "apiKeyPreview": self.api_key_preview,
            "statusLabel": self.status_label,
            "tone": self.tone,
        }


@dataclass(frozen=True, slots=True)
class CredentialUpdateResult:
    settings: dict[str, Any]
    summary: CredentialSummary
    changed: bool


def summarize_credentials(
    settings: dict[str, Any],
    environment: str,
) -> CredentialSummary:
    normalized_environment = normalize_credential_environment(environment)
    key_field, secret_field = credential_field_names(normalized_environment)
    api_key = str(settings.get(key_field, "") or "").strip()
    api_secret = str(settings.get(secret_field, "") or "").strip()
    key_configured = bool(api_key)
    secret_configured = bool(api_secret)
    configured = key_configured and secret_configured

    if configured:
        status_label = "Configuradas"
        tone = "success"
    elif key_configured or secret_configured:
        status_label = "Incompletas"
        tone = "danger"
    else:
        status_label = "Não configuradas"
        tone = "neutral"

    return CredentialSummary(
        environment=normalized_environment,
        configured=configured,
        api_key_configured=key_configured,
        api_secret_configured=secret_configured,
        api_key_preview=mask_api_key(api_key),
        status_label=status_label,
        tone=tone,
    )


def prepare_credentials_update(
    current_settings: dict[str, Any],
    *,
    environment: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    clear: bool = False,
) -> CredentialUpdateResult:
    normalized_environment = normalize_credential_environment(environment)
    key_field, secret_field = credential_field_names(normalized_environment)
    prepared = deepcopy(current_settings)

    if clear:
        prepared[key_field] = ""
        prepared[secret_field] = ""
    else:
        if api_key is not None:
            prepared[key_field] = normalize_credential_value(api_key, label="API key")
        if api_secret is not None:
            prepared[secret_field] = normalize_credential_value(
                api_secret,
                label="API secret",
            )

    stored_key = str(prepared.get(key_field, "") or "").strip()
    stored_secret = str(prepared.get(secret_field, "") or "").strip()
    if bool(stored_key) != bool(stored_secret):
        raise ValueError(
            "API key e API secret precisam estar configuradas juntas para o ambiente selecionado."
        )

    changed = prepared != current_settings
    return CredentialUpdateResult(
        settings=prepared,
        summary=summarize_credentials(prepared, normalized_environment),
        changed=changed,
    )


def normalize_credential_environment(environment: str) -> str:
    normalized = str(environment or "").upper().strip()
    if normalized not in CREDENTIAL_ENVIRONMENTS:
        raise ValueError(f"Ambiente de credenciais inválido: {environment!r}.")
    return normalized


def credential_field_names(environment: str) -> tuple[str, str]:
    normalized = normalize_credential_environment(environment)
    return _CREDENTIAL_FIELDS[normalized]


def normalize_credential_value(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > _MAX_CREDENTIAL_LENGTH:
        raise ValueError(f"{label} excede o tamanho máximo aceito.")
    if any(character.isspace() for character in normalized):
        raise ValueError(f"{label} não pode conter espaços ou quebras de linha.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} contém caracteres de controle inválidos.")
    return normalized


def mask_api_key(api_key: str) -> str:
    normalized = str(api_key or "").strip()
    if not normalized:
        return "—"
    if len(normalized) <= 8:
        return normalized[:2] + "••••" + normalized[-2:]
    return normalized[:4] + "••••••••" + normalized[-4:]


def secure_credentials_file(path: str | Path) -> bool:
    target = Path(path)
    if os.name != "posix" or not target.exists():
        return False
    target.chmod(0o600)
    return True
