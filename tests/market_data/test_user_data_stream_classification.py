from governor.user_data_stream import classify_user_data_stream_payload


def test_classifies_order_trade_fill():
    classification = classify_user_data_stream_payload(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1710000000000,
            "o": {
                "s": "BTCUSDT",
                "x": "TRADE",
                "X": "FILLED",
                "S": "BUY",
                "o": "MARKET",
                "q": "0.01",
                "z": "0.01",
                "ap": "100.5",
                "rp": "1.23",
            },
        }
    )

    assert classification.category == "ORDER"
    assert classification.operational_type == "ORDER_TRADE_FILL"
    assert classification.severity == "INFO"
    assert classification.symbol == "BTCUSDT"
    assert classification.order_status == "FILLED"
    assert classification.execution_type == "TRADE"
    assert classification.should_update_order_cache is True
    assert classification.should_update_position_cache is True
    assert classification.should_update_account_cache is True


def test_classifies_order_rejected_as_warning():
    classification = classify_user_data_stream_payload(
        {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "s": "ETHUSDT",
                "x": "REJECTED",
                "X": "REJECTED",
            },
        }
    )

    assert classification.category == "ORDER"
    assert classification.operational_type == "ORDER_REJECTED"
    assert classification.severity == "WARNING"
    assert classification.symbol == "ETHUSDT"


def test_classifies_account_update_with_positions():
    classification = classify_user_data_stream_payload(
        {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "m": "ORDER",
                "B": [{"a": "USDT", "wb": "100"}],
                "P": [
                    {"s": "BTCUSDT", "pa": "0.01"},
                    {"s": "ETHUSDT", "pa": "0"},
                ],
            },
        }
    )

    assert classification.category == "ACCOUNT"
    assert classification.operational_type == "POSITION_ACCOUNT_UPDATE"
    assert classification.account_update_reason == "ORDER"
    assert classification.symbols == ["BTCUSDT", "ETHUSDT"]
    assert classification.should_update_position_cache is True
    assert classification.should_update_account_cache is True


def test_classifies_margin_call_as_critical_risk():
    classification = classify_user_data_stream_payload(
        {
            "e": "MARGIN_CALL",
            "p": [
                {
                    "s": "BTCUSDT",
                    "pa": "0.01",
                }
            ],
        }
    )

    assert classification.category == "RISK"
    assert classification.operational_type == "MARGIN_CALL"
    assert classification.severity == "CRITICAL"
    assert classification.symbols == ["BTCUSDT"]


def test_classifies_listen_key_expired_as_connection_critical():
    classification = classify_user_data_stream_payload(
        {
            "e": "listenKeyExpired",
            "E": 1710000000000,
        }
    )

    assert classification.category == "CONNECTION"
    assert classification.operational_type == "LISTEN_KEY_EXPIRED"
    assert classification.severity == "CRITICAL"
    assert classification.symbol == ""
