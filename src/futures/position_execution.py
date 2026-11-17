from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class PositionExecutionStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    DRY_RUN = "DRY_RUN"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"


class PositionChangeState(str, Enum):
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    UNKNOWN = "UNKNOWN"


class PositionRecoveryStatus(str, Enum):
    NONE = "NONE"
    PARTIAL = "PARTIAL"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class PositionExecutionResult:
    status: PositionExecutionStatus
    position_changed: bool
    reason: str
    order_ids: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    position_state: PositionChangeState = PositionChangeState.UNCHANGED
    recovery_status: PositionRecoveryStatus = PositionRecoveryStatus.NONE

    def __post_init__(self) -> None:
        status = PositionExecutionStatus(self.status)
        position_state = PositionChangeState(self.position_state)
        recovery_status = PositionRecoveryStatus(self.recovery_status)
        order_ids = tuple(
            str(order_id)
            for order_id in self.order_ids
            if order_id is not None and str(order_id).strip()
        )
        diagnostics = dict(self.diagnostics)
        diagnostics.setdefault("position_state", position_state.value)
        diagnostics.setdefault("recovery_status", recovery_status.value)

        object.__setattr__(self, "status", status)
        object.__setattr__(self, "position_state", position_state)
        object.__setattr__(self, "recovery_status", recovery_status)
        object.__setattr__(self, "order_ids", order_ids)
        object.__setattr__(self, "reason", str(self.reason or status.value))
        object.__setattr__(self, "diagnostics", freeze_mapping(diagnostics))

        # Preserve the public validation messages used by existing callers and tests.
        if status is PositionExecutionStatus.CONFIRMED:
            if not self.position_changed:
                raise ValueError("CONFIRMED execution requires position_changed=True")
        elif self.position_changed:
            raise ValueError(f"{status.value} execution cannot change position")

        if position_state is PositionChangeState.CHANGED and not self.position_changed:
            raise ValueError("CHANGED position state requires position_changed=True")
        if position_state is not PositionChangeState.CHANGED and self.position_changed:
            raise ValueError(
                f"{position_state.value} position state cannot use position_changed=True"
            )

        if status is PositionExecutionStatus.CONFIRMED:
            if position_state is not PositionChangeState.CHANGED:
                raise ValueError("CONFIRMED execution requires CHANGED position state")
            if recovery_status is not PositionRecoveryStatus.NONE:
                raise ValueError("CONFIRMED execution cannot carry a recovery status")
            return

        if recovery_status is PositionRecoveryStatus.PARTIAL:
            if status is not PositionExecutionStatus.UNCERTAIN:
                raise ValueError("PARTIAL recovery requires UNCERTAIN execution status")
            if position_state is not PositionChangeState.UNKNOWN:
                raise ValueError("PARTIAL recovery requires UNKNOWN position state")

        if recovery_status is PositionRecoveryStatus.ROLLED_BACK:
            if status is not PositionExecutionStatus.SKIPPED:
                raise ValueError("ROLLED_BACK recovery requires SKIPPED execution status")
            if position_state is not PositionChangeState.UNCHANGED:
                raise ValueError("ROLLED_BACK recovery requires UNCHANGED position state")

    @classmethod
    def confirmed(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.CONFIRMED,
            position_changed=True,
            position_state=PositionChangeState.CHANGED,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def dry_run(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.DRY_RUN,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def blocked(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.BLOCKED,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def skipped(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.SKIPPED,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def uncertain(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.UNCERTAIN,
            position_changed=False,
            position_state=PositionChangeState.UNKNOWN,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def failed(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.FAILED,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def partial(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.UNCERTAIN,
            position_changed=False,
            position_state=PositionChangeState.UNKNOWN,
            recovery_status=PositionRecoveryStatus.PARTIAL,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def rolled_back(
        cls,
        reason: str,
        *,
        order_ids: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> "PositionExecutionResult":
        return cls(
            status=PositionExecutionStatus.SKIPPED,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            recovery_status=PositionRecoveryStatus.ROLLED_BACK,
            reason=reason,
            order_ids=order_ids,
            diagnostics=diagnostics or {},
        )


def freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {str(key): _freeze_value(item) for key, item in value.items()},
    )


def _freeze_value(value: Any) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value
