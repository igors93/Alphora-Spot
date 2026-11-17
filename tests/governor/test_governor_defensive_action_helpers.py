from governor.governor_trade_helpers import _is_new_entry_action


def test_is_new_entry_action_accepts_entry_actions():
    assert _is_new_entry_action("OPEN_LONG") is True
    assert _is_new_entry_action("OPEN_SHORT") is True
    assert _is_new_entry_action("ENTER_LONG") is True
    assert _is_new_entry_action("BUY") is True
    assert _is_new_entry_action("SELL") is True


def test_is_new_entry_action_rejects_exit_or_neutral_actions():
    assert _is_new_entry_action("CLOSE_LONG") is False
    assert _is_new_entry_action("CLOSE_SHORT") is False
    assert _is_new_entry_action("EXIT_LONG") is False
    assert _is_new_entry_action("REDUCE_POSITION") is False
    assert _is_new_entry_action("NO_ACTION") is False
    assert _is_new_entry_action(None) is False
