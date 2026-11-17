from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class MessageSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class OperationMode(str, Enum):
    GOVERNOR_SINGLE_SYMBOL = "GOVERNOR_SINGLE_SYMBOL"
    GOVERNOR_MULTI_SYMBOL = "GOVERNOR_MULTI_SYMBOL"


class OperationPreparationStatus(str, Enum):
    READY = "READY"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    BLOCKED = "BLOCKED"


class ConfirmationKind(str, Enum):
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    CHECKLIST = "CHECKLIST"
    TYPED = "TYPED"


class PreflightStatus(str, Enum):
    READY = "READY"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class OperationMessage:
    code: str
    title: str
    message: str
    severity: MessageSeverity = MessageSeverity.INFO


@dataclass(frozen=True, slots=True)
class ConfirmationResponse:
    key: str
    accepted: bool
    typed_value: str = ""
    checked_items: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    key: str
    kind: ConfirmationKind
    title: str
    message: str
    severity: MessageSeverity = MessageSeverity.WARNING
    required_tokens: tuple[str, ...] = ()
    checklist_items: tuple[str, ...] = ()
    accept_label: str = "Continuar"
    reject_label: str = "Cancelar"

    def accepts(self, response: ConfirmationResponse) -> bool:
        if response.key != self.key or not response.accepted:
            return False

        if self.kind is ConfirmationKind.ACKNOWLEDGEMENT:
            return True

        if self.kind is ConfirmationKind.TYPED:
            normalized = response.typed_value.strip()
            return normalized in self.required_tokens

        if self.kind is ConfirmationKind.CHECKLIST:
            checked = set(response.checked_items)
            return set(self.checklist_items).issubset(checked)

        return False


@dataclass(frozen=True, slots=True)
class OperationLaunchPlan:
    mode: OperationMode
    command: tuple[str, ...]
    symbol_profiles_path: str | None
    summary: str
    confirmations: tuple[ConfirmationRequest, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    enabled_profiles: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False)
    runtime_profiles: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False)
    environment: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "settings", _freeze_mapping(self.settings))
        object.__setattr__(
            self,
            "enabled_profiles",
            tuple(_freeze_mapping(profile) for profile in self.enabled_profiles),
        )
        object.__setattr__(
            self,
            "runtime_profiles",
            tuple(_freeze_mapping(profile) for profile in self.runtime_profiles),
        )
        object.__setattr__(
            self,
            "environment",
            MappingProxyType({str(key): str(value) for key, value in self.environment.items()}),
        )

    @property
    def multi_symbol(self) -> bool:
        return self.mode is OperationMode.GOVERNOR_MULTI_SYMBOL

    def settings_dict(self) -> dict[str, Any]:
        return _thaw_mapping(self.settings)

    def enabled_profiles_list(self) -> list[dict[str, Any]]:
        return [_thaw_mapping(profile) for profile in self.enabled_profiles]

    def runtime_profiles_list(self) -> list[dict[str, Any]]:
        return [_thaw_mapping(profile) for profile in self.runtime_profiles]

    def environment_dict(self) -> dict[str, str]:
        return {str(key): str(value) for key, value in self.environment.items()}


@dataclass(frozen=True, slots=True)
class OperationPreparationResult:
    status: OperationPreparationStatus
    messages: tuple[OperationMessage, ...]
    plan: OperationLaunchPlan | None = None

    @property
    def blocked(self) -> bool:
        return self.status is OperationPreparationStatus.BLOCKED

    @property
    def requires_confirmation(self) -> bool:
        return self.status is OperationPreparationStatus.REQUIRES_CONFIRMATION


@dataclass(frozen=True, slots=True)
class OperationAuthorizationResult:
    authorized: bool
    missing_confirmation_keys: tuple[str, ...] = ()
    invalid_confirmation_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreflightResult:
    status: PreflightStatus
    messages: tuple[OperationMessage, ...]
    output: str = ""
    approved_symbols: tuple[str, ...] = ()
    blocked_symbols: tuple[str, ...] = ()
    confirmation: ConfirmationRequest | None = None
    payload: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))

    @property
    def ready_to_launch(self) -> bool:
        return self.status is PreflightStatus.READY

    @property
    def requires_confirmation(self) -> bool:
        return self.status in {
            PreflightStatus.REQUIRES_CONFIRMATION,
            PreflightStatus.PARTIAL,
        }

    @property
    def can_continue(self) -> bool:
        return self.ready_to_launch

    def payload_dict(self) -> dict[str, Any]:
        return _thaw_mapping(self.payload)


def authorize_operation(
    plan: OperationLaunchPlan,
    responses: Sequence[ConfirmationResponse] | Mapping[str, ConfirmationResponse],
) -> OperationAuthorizationResult:
    if isinstance(responses, Mapping):
        response_map = dict(responses)
    else:
        response_map = {response.key: response for response in responses}

    missing: list[str] = []
    invalid: list[str] = []

    for request in plan.confirmations:
        response = response_map.get(request.key)
        if response is None:
            missing.append(request.key)
            continue
        if not request.accepts(response):
            invalid.append(request.key)

    return OperationAuthorizationResult(
        authorized=not missing and not invalid,
        missing_confirmation_keys=tuple(missing),
        invalid_confirmation_keys=tuple(invalid),
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return deepcopy(value)


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _thaw_value(item) for key, item in value.items()}


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw_value(item) for item in value}
    return deepcopy(value)
