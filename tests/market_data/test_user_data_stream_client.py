from binance.futures_client import BinanceFuturesClient


class FakeResponse:
    status_code = 200
    text = '{"listenKey": "abc123"}'

    def json(self):
        return {"listenKey": "abc123"}


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse()


def _client():
    client = BinanceFuturesClient(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    client.session = FakeSession()
    return client


def test_start_user_data_stream_uses_api_key_header_without_signature():
    client = _client()

    data = client.start_user_data_stream()

    assert data["listenKey"] == "abc123"

    call = client.session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/fapi/v1/listenKey")
    assert call["headers"] == {"X-MBX-APIKEY": "key"}
    assert call["params"] is None


def test_keepalive_and_close_user_data_stream_use_expected_methods():
    client = _client()

    client.keepalive_user_data_stream()
    client.close_user_data_stream()

    assert client.session.calls[0]["method"] == "PUT"
    assert client.session.calls[1]["method"] == "DELETE"
    assert client.session.calls[0]["url"].endswith("/fapi/v1/listenKey")
    assert client.session.calls[1]["url"].endswith("/fapi/v1/listenKey")
