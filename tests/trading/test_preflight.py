from dataclasses import replace

from ops.preflight import run_preflight


def test_preflight_returns_critical_error_without_keys(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "")

    result = run_preflight()

    assert result["ok"] is False
    assert any(check["name"] == "config" for check in result["checks"])


def test_preflight_validates_strategy_before_binance(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_STRATEGY_MODULE", "strategies.futures.missing")
    monkeypatch.setenv("ALPHORA_STRATEGY_CLASS", "MissingStrategy")

    result = run_preflight()

    assert result["ok"] is False
    assert any(check["name"] == "strategy" for check in result["checks"])


def test_preflight_strategy_check_uses_strategy_params(config_factory, monkeypatch):
    import ops.preflight as preflight

    captured = {}
    config = replace(config_factory(), strategy_params={"min_history": 2})

    def fake_load_strategy(module_name, class_name, strategy_params=None):
        captured["strategy_params"] = strategy_params
        return object()

    monkeypatch.setattr(preflight, "load_strategy", fake_load_strategy)
    checks = []

    preflight._check_strategy(config, checks)

    assert captured["strategy_params"] == {"min_history": 2}
    assert checks[0].ok is True
