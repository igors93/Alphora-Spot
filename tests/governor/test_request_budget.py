from governor.request_budget import RequestBudget


def test_request_budget_allows_until_limit():
    budget = RequestBudget(max_heavy_checks_per_cycle=2)

    first = budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="HOT",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )
    second = budget.evaluate_heavy_check(
        symbol="ETHUSDT",
        priority="HOT",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=True,
    )
    third = budget.evaluate_heavy_check(
        symbol="SOLUSDT",
        priority="HOT",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.reason == "HEAVY_CHECK_BUDGET_EXHAUSTED"
    assert budget.summary()["used_heavy_checks"] == 2
    assert budget.summary()["denied_heavy_checks"] == 1


def test_request_budget_open_position_override_allows_safety_check():
    budget = RequestBudget(
        max_heavy_checks_per_cycle=0,
        open_position_override=True,
    )

    decision = budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="COLD",
        action="NO_ACTION",
        position_open=True,
        fast_lane=False,
    )

    assert decision.allowed is True
    assert decision.override is True
    assert decision.reason == "OPEN_POSITION_OVERRIDE"
    assert budget.summary()["overridden_heavy_checks"] == 1


def test_request_budget_can_deny_when_limit_is_zero_without_override():
    budget = RequestBudget(
        max_heavy_checks_per_cycle=0,
        open_position_override=False,
    )

    decision = budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="HOT",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=True,
    )

    assert decision.allowed is False
    assert decision.reason == "HEAVY_CHECK_BUDGET_DISABLED_OR_ZERO"


def test_request_budget_ready_consumes_ready_category():
    budget = RequestBudget(
        max_heavy_checks_per_cycle=3,
        max_heavy_checks_by_category={"READY": 1},
    )

    decision = budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="READY",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )

    assert decision.allowed is True
    assert decision.category == "READY"
    assert decision.category_used_heavy_checks == 1
    assert budget.summary()["used_heavy_checks_by_category"]["READY"] == 1


def test_request_budget_borrows_when_category_is_exhausted_and_global_available():
    budget = RequestBudget(
        max_heavy_checks_per_cycle=2,
        max_heavy_checks_by_category={"READY": 0},
        allow_budget_borrowing=True,
    )

    decision = budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="READY",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )

    assert decision.allowed is True
    assert decision.reason == "HEAVY_CHECK_BUDGET_BORROWED"
    assert decision.borrowed_budget is True


def test_request_budget_blocks_when_global_budget_is_exhausted():
    budget = RequestBudget(
        max_heavy_checks_per_cycle=1,
        max_heavy_checks_by_category={"READY": 5},
    )
    budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="READY",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )

    decision = budget.evaluate_heavy_check(
        symbol="ETHUSDT",
        priority="READY",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )

    assert decision.allowed is False
    assert decision.reason == "HEAVY_CHECK_BUDGET_EXHAUSTED"


def test_request_budget_warm_and_cold_can_borrow_when_global_is_free():
    budget = RequestBudget(
        max_heavy_checks_per_cycle=2,
        max_heavy_checks_by_category={"WARM": 0, "COLD": 0},
        allow_budget_borrowing=True,
    )

    warm = budget.evaluate_heavy_check(
        symbol="BTCUSDT",
        priority="WARM",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
    )
    cold = budget.evaluate_heavy_check(
        symbol="ETHUSDT",
        priority="COLD",
        action="NO_ACTION",
        position_open=False,
        fast_lane=False,
    )

    assert warm.allowed is True
    assert warm.reason == "HEAVY_CHECK_BUDGET_BORROWED"
    assert cold.allowed is True
    assert cold.reason == "HEAVY_CHECK_BUDGET_BORROWED"
