import pytest

from binance.futures_client import BinanceFuturesClient, BinanceFuturesMutationBlocked


class FakeResponse:
    status_code = 200
    text = "[]"

    def json(self):
        return []


class FakeSession:
    def __init__(self):
        self.calls = []
        self.closed = False

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse()

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("new_order", (), {"symbol": "BTCUSDT"}),
        ("new_algo_order", (), {"symbol": "BTCUSDT"}),
        ("cancel_order", (), {"symbol": "BTCUSDT", "order_id": 1}),
        ("cancel_algo_order", (), {"algo_id": 1}),
        ("cancel_all_open_orders", ("BTCUSDT",), {}),
        ("cancel_all_open_algo_orders", ("BTCUSDT",), {}),
        ("change_margin_type", ("BTCUSDT", "ISOLATED"), {}),
        ("change_leverage", ("BTCUSDT", 1), {}),
    ],
)
def test_dry_run_client_blocks_all_mutations(method, args, kwargs):
    client = BinanceFuturesClient(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
        mutation_enabled=False,
    )

    with pytest.raises(BinanceFuturesMutationBlocked):
        getattr(client, method)(*args, **kwargs)


def test_client_reuses_session_for_signed_income_history():
    client = BinanceFuturesClient(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    session = FakeSession()
    client.session = session

    result = client.income_history(
        symbol="BTCUSDT",
        income_type="REALIZED_PNL",
        limit=1,
    )
    client.close()

    assert result == []
    assert session.closed is True
    assert session.calls[0]["url"].endswith("/fapi/v1/income")
    assert session.calls[0]["params"]["symbol"] == "BTCUSDT"
    assert session.calls[0]["params"]["incomeType"] == "REALIZED_PNL"
    assert session.calls[0]["headers"]["X-MBX-APIKEY"] == "key"


@pytest.fixture
def credential_free_env(monkeypatch):
    monkeypatch.setattr("config.loaders.load_env_file", lambda: "")
    monkeypatch.delenv("BINANCE_FUTURES_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_API_SECRET", raising=False)
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    return monkeypatch


@pytest.mark.parametrize("mode", ["DRY_RUN", "PAPER_LIVE"])
def test_read_only_modes_explain_why_credentials_are_required(credential_free_env, mode):
    from config import load_config

    credential_free_env.setenv("ALPHORA_EXECUTION_MODE", mode)

    with pytest.raises(ValueError) as error:
        load_config()

    message = str(error.value)
    assert message.startswith("BINANCE_FUTURES_API_KEY nao foi configurada")
    assert f"{mode} nao envia ordens" in message
    assert "endpoints assinados" in message


def test_live_mode_keeps_plain_missing_credential_message(credential_free_env):
    from config import load_config

    credential_free_env.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")

    with pytest.raises(ValueError, match=r"^BINANCE_FUTURES_API_KEY nao foi configurada no \.env$"):
        load_config()


def test_missing_secret_is_reported_by_name(credential_free_env):
    from config import load_config

    credential_free_env.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    credential_free_env.setenv("BINANCE_FUTURES_API_KEY", "key")

    with pytest.raises(ValueError, match="BINANCE_FUTURES_API_SECRET nao foi configurada"):
        load_config()


@pytest.mark.parametrize("mode", ["DRY_RUN", "PAPER_LIVE"])
def test_read_only_modes_never_enable_mutations(credential_free_env, mode):
    from config import load_config

    credential_free_env.setenv("ALPHORA_EXECUTION_MODE", mode)
    credential_free_env.setenv("BINANCE_FUTURES_API_KEY", "key")
    credential_free_env.setenv("BINANCE_FUTURES_API_SECRET", "secret")

    config = load_config()

    assert config.mutations_enabled is False
    assert config.execute_orders is False


def test_client_lists_open_orders_of_every_symbol_when_symbol_is_none():
    client = BinanceFuturesClient(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    session = FakeSession()
    client.session = session

    client.open_orders(None)
    client.open_algo_orders(None)
    client.open_orders("BTCUSDT")

    assert session.calls[0]["url"].endswith("/fapi/v1/openOrders")
    assert "symbol" not in session.calls[0]["params"]
    assert session.calls[1]["url"].endswith("/fapi/v1/openAlgoOrders")
    assert "symbol" not in session.calls[1]["params"]
    assert session.calls[2]["params"]["symbol"] == "BTCUSDT"
