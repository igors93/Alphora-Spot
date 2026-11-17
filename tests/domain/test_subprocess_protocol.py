from domain.subprocess_protocol import decode_task_event, encode_task_event


def test_task_event_round_trip_and_version_gate():
    line = encode_task_event("ai_progress", message="Treinando BTCUSDT")
    assert decode_task_event(line) == {
        "type": "ai_progress",
        "version": 1,
        "message": "Treinando BTCUSDT",
    }
    assert decode_task_event('{"type":"ai_progress","version":2,"message":"old"}') is None
    assert decode_task_event('{"type":"ai_progress","version":true}') is None
    assert decode_task_event("AI_PROGRESS: old output") is None
