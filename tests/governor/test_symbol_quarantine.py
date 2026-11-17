from governor.symbol_quarantine import (
    SymbolQuarantine,
    SymbolQuarantineLevel,
    SymbolQuarantineSettings,
)


def _quarantine():
    return SymbolQuarantine(
        SymbolQuarantineSettings(
            watch_after_errors=2,
            cooldown_after_errors=3,
            quarantine_after_errors=5,
            cooldown_seconds=10,
            quarantine_seconds=20,
        )
    )


def test_symbol_quarantine_thresholds_escalate_conservatively():
    quarantine = _quarantine()

    _, first = quarantine.record_event("btcusdt", event="ERROR", reason="boom", now=1)
    _, second = quarantine.record_event("BTCUSDT", event="ERROR", reason="boom", now=2)
    _, third = quarantine.record_event("BTCUSDT", event="ERROR", reason="boom", now=3)
    quarantine.record_event("BTCUSDT", event="ERROR", reason="boom", now=4)
    _, fifth = quarantine.record_event("BTCUSDT", event="ERROR", reason="boom", now=5)

    assert first.level is SymbolQuarantineLevel.NONE
    assert second.level is SymbolQuarantineLevel.WATCH
    assert third.level is SymbolQuarantineLevel.COOLDOWN
    assert fifth.level is SymbolQuarantineLevel.QUARANTINED


def test_symbol_quarantine_blocks_only_new_entries():
    quarantine = _quarantine()
    for offset in range(5):
        quarantine.record_event("BTCUSDT", event="ERROR", reason="boom", now=offset)

    open_decision = quarantine.evaluate(
        "BTCUSDT",
        action="OPEN_LONG",
        position_open=False,
        now=5,
    )
    close_decision = quarantine.evaluate(
        "BTCUSDT",
        action="CLOSE_POSITION",
        position_open=False,
        now=5,
    )
    protected_position_decision = quarantine.evaluate(
        "BTCUSDT",
        action="OPEN_SHORT",
        position_open=True,
        now=5,
    )

    assert open_decision.allowed is False
    assert open_decision.blocks_new_entry is True
    assert close_decision.allowed is True
    assert protected_position_decision.allowed is True


def test_symbol_quarantine_expires_automatically():
    quarantine = _quarantine()
    for offset in range(5):
        quarantine.record_event("BTCUSDT", event="ERROR", reason="boom", now=offset)

    expired = quarantine.state_for("BTCUSDT", now=25)

    assert expired.level is SymbolQuarantineLevel.NONE
    assert expired.error_count == 0
    assert expired.reason == "QUARANTINE_EXPIRED"
