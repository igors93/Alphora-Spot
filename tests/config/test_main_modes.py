from dataclasses import replace

import pytest

from config import ExecutionMode


def test_single_symbol_mode_starts_governor_path_by_default(config_factory, monkeypatch):
    import engine as main

    calls = {"governor_start": 0}
    config = config_factory()

    class FakeClient:
        def __init__(self, **kwargs):
            pass

    class FakeGovernor:
        @classmethod
        def from_config(cls, config, client):
            return cls()

        def start(self):
            calls["governor_start"] += 1

    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(main, "BinanceFuturesClient", FakeClient)
    monkeypatch.setattr(main, "AlphoraGovernor", FakeGovernor)

    main.main()

    assert calls["governor_start"] == 1


def test_main_does_not_log_first_symbol_warning_when_governor_enabled(
    config_factory,
    monkeypatch,
):
    import engine as main

    warnings = []
    calls = {"governor_start": 0}
    config = replace(
        config_factory(symbols=("BTCUSDT", "ETHUSDT")),
        multi_symbol_enabled=True,
    )

    class FakeLogger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, message, *args):
            warnings.append(message % args if args else message)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

    class FakeGovernor:
        @classmethod
        def from_config(cls, config, client):
            return cls()

        def start(self):
            calls["governor_start"] += 1

    monkeypatch.setattr(main, "get_logger", lambda _name: FakeLogger())
    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(main, "BinanceFuturesClient", FakeClient)
    monkeypatch.setattr(main, "AlphoraGovernor", FakeGovernor)

    main.main()

    assert calls["governor_start"] == 1
    assert not any("Operando apenas o primeiro simbolo" in item for item in warnings)
    assert any("Alphora Governor e o caminho operacional oficial" in item for item in warnings)


def test_live_cli_without_gui_or_allow_flag_is_blocked(config_factory, monkeypatch):
    import engine as main

    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    monkeypatch.delenv("ALPHORA_LAUNCHED_BY_GUI", raising=False)
    monkeypatch.delenv("ALPHORA_ALLOW_CLI_RUN", raising=False)
    monkeypatch.setattr(main, "load_config", lambda: config)

    with pytest.raises(SystemExit, match="Execucao live direta"):
        main.main()
