from desktop.services.operation_models import (
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    OperationLaunchPlan,
    OperationMode,
    authorize_operation,
)


def test_typed_confirmation_requires_exact_token():
    request = ConfirmationRequest(
        key="live",
        kind=ConfirmationKind.TYPED,
        title="Live",
        message="Confirmar",
        required_tokens=("REAL_LIVE",),
    )

    assert request.accepts(ConfirmationResponse(key="live", accepted=True, typed_value="REAL_LIVE"))
    assert not request.accepts(
        ConfirmationResponse(key="live", accepted=True, typed_value="real_live")
    )


def test_checklist_confirmation_requires_every_item():
    request = ConfirmationRequest(
        key="checklist",
        kind=ConfirmationKind.CHECKLIST,
        title="Checklist",
        message="Confirmar",
        checklist_items=("a", "b"),
    )

    assert request.accepts(
        ConfirmationResponse(
            key="checklist",
            accepted=True,
            checked_items=("a", "b"),
        )
    )
    assert not request.accepts(
        ConfirmationResponse(
            key="checklist",
            accepted=True,
            checked_items=("a",),
        )
    )


def test_authorization_reports_missing_and_invalid_responses():
    plan = OperationLaunchPlan(
        mode=OperationMode.GOVERNOR_SINGLE_SYMBOL,
        command=("python", "main.py"),
        symbol_profiles_path=None,
        summary="summary",
        confirmations=(
            ConfirmationRequest(
                key="ack",
                kind=ConfirmationKind.ACKNOWLEDGEMENT,
                title="Aviso",
                message="Confirmar",
            ),
            ConfirmationRequest(
                key="typed",
                kind=ConfirmationKind.TYPED,
                title="Token",
                message="Confirmar",
                required_tokens=("TOKEN",),
            ),
        ),
    )

    result = authorize_operation(
        plan,
        [ConfirmationResponse(key="typed", accepted=True, typed_value="errado")],
    )

    assert not result.authorized
    assert result.missing_confirmation_keys == ("ack",)
    assert result.invalid_confirmation_keys == ("typed",)


def test_launch_plan_exposes_immutable_mappings_and_mutable_copies():
    plan = OperationLaunchPlan(
        mode=OperationMode.GOVERNOR_SINGLE_SYMBOL,
        command=("python", "main.py"),
        symbol_profiles_path=None,
        summary="summary",
        settings={"symbol": "BTCUSDT", "strategy_params": {"period": 14}},
        environment={"ALPHORA_MODE": "DRY_RUN"},
    )

    try:
        plan.settings["symbol"] = "ETHUSDT"
    except TypeError:
        pass
    else:
        raise AssertionError("settings precisa ser imutável")

    copied = plan.settings_dict()
    copied["symbol"] = "ETHUSDT"
    copied["strategy_params"]["period"] = 20

    assert plan.settings["symbol"] == "BTCUSDT"
    assert plan.settings["strategy_params"]["period"] == 14


def test_preflight_result_only_launches_when_ready():
    from desktop.services.operation_models import PreflightResult, PreflightStatus

    waiting = PreflightResult(
        status=PreflightStatus.REQUIRES_CONFIRMATION,
        messages=(),
    )
    ready = PreflightResult(status=PreflightStatus.READY, messages=())

    assert not waiting.ready_to_launch
    assert waiting.requires_confirmation
    assert not waiting.can_continue
    assert ready.ready_to_launch
    assert ready.can_continue
