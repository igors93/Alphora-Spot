import pytest

from domain.orders import InvalidOrderTransition, OrderLifecycle, OrderPurpose, OrderState


def _persisted() -> OrderLifecycle:
    order = OrderLifecycle(intent_id="intent-1", client_order_id="entry-1")
    return order.advance(OrderState.RESERVED).advance(OrderState.PERSISTED)


def test_normal_entry_has_one_submission_and_can_be_protected() -> None:
    order = _persisted()
    assert order.can_submit

    for state in (
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.PROTECTED,
        OrderState.CLOSING,
        OrderState.CLOSED,
    ):
        order = order.advance(state)

    assert order.state is OrderState.CLOSED
    assert order.history.count(OrderState.SUBMITTING) == 1
    assert not order.can_submit
    with pytest.raises(InvalidOrderTransition):
        order.advance(OrderState.SUBMITTING)


def test_unknown_result_must_reconcile_and_cannot_resubmit() -> None:
    order = _persisted().advance(OrderState.SUBMITTING).advance(OrderState.UNKNOWN)

    with pytest.raises(InvalidOrderTransition):
        order.advance(OrderState.SUBMITTING)

    order = order.advance(OrderState.RECONCILING).advance(OrderState.UNKNOWN)
    order = order.advance(OrderState.RECONCILING).advance(OrderState.FILLED)
    assert order.history.count(OrderState.SUBMITTING) == 1


def test_partial_fill_cannot_be_discarded_as_canceled() -> None:
    order = (
        _persisted()
        .advance(OrderState.SUBMITTING)
        .advance(OrderState.ACKNOWLEDGED)
        .advance(OrderState.PARTIALLY_FILLED)
    )
    with pytest.raises(InvalidOrderTransition):
        order.advance(OrderState.CANCELED)
    assert (
        order.advance(OrderState.PROTECTION_FAILED)
        .advance(OrderState.EMERGENCY_CLOSE)
        .advance(OrderState.CLOSED)
        .state
        is OrderState.CLOSED
    )


def test_unknown_close_can_only_finish_after_reconciliation() -> None:
    order = (
        _persisted()
        .advance(OrderState.SUBMITTING)
        .advance(OrderState.ACKNOWLEDGED)
        .advance(OrderState.FILLED)
        .advance(OrderState.PROTECTED)
        .advance(OrderState.CLOSING)
        .advance(OrderState.UNKNOWN)
    )
    with pytest.raises(InvalidOrderTransition):
        order.advance(OrderState.CLOSED)
    assert (
        order.advance(OrderState.RECONCILING).advance(OrderState.CLOSED).state is OrderState.CLOSED
    )


def test_replay_rejects_invalid_or_duplicate_submission_history() -> None:
    persisted = _persisted()
    assert OrderLifecycle(
        intent_id=persisted.intent_id,
        client_order_id=persisted.client_order_id,
        history=persisted.history,
    ).can_submit

    with pytest.raises(InvalidOrderTransition):
        OrderLifecycle(
            intent_id="intent-1",
            client_order_id="entry-1",
            history=(*persisted.history, OrderState.SUBMITTING, OrderState.SUBMITTING),
        )


def test_identity_and_initial_state_are_required() -> None:
    with pytest.raises(ValueError):
        OrderLifecycle(intent_id="", client_order_id="entry-1")
    with pytest.raises(InvalidOrderTransition):
        OrderLifecycle(
            intent_id="intent-1",
            client_order_id="entry-1",
            history=(OrderState.SUBMITTING,),
        )
    with pytest.raises(ValueError):
        _persisted().advance("SUBMITTING")  # type: ignore[arg-type]


def test_close_order_can_finish_only_after_a_confirmed_fill() -> None:
    close = OrderLifecycle(
        intent_id="close-1", client_order_id="CLOSE-1", purpose=OrderPurpose.CLOSE
    )
    for state in (OrderState.RESERVED, OrderState.PERSISTED, OrderState.SUBMITTING):
        close = close.advance(state)
    close = close.advance(OrderState.UNKNOWN).advance(OrderState.RECONCILING)
    with pytest.raises(InvalidOrderTransition):
        close.advance(OrderState.CLOSED)
    close = close.advance(OrderState.FILLED).advance(OrderState.CLOSED)
    assert close.history.count(OrderState.SUBMITTING) == 1


def test_entry_fill_cannot_skip_protection_to_closed() -> None:
    entry = _persisted().advance(OrderState.SUBMITTING).advance(OrderState.ACKNOWLEDGED)
    with pytest.raises(InvalidOrderTransition):
        entry.advance(OrderState.FILLED).advance(OrderState.CLOSED)
