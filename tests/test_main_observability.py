from __future__ import annotations

import json
from dataclasses import replace


def test_main_initializes_and_shutdowns_observability(
    config_factory,
    monkeypatch,
    tmp_path,
):
    import engine as main
    from observability.bootstrap import reset_observability_bootstrap_for_testing
    from utils.run_context import reset_run_context_for_testing

    reset_observability_bootstrap_for_testing()
    reset_run_context_for_testing()
    config = replace(
        config_factory(),
        audit_runs_dir=str(tmp_path / "runs"),
        audit_text_logs_enabled=False,
    )
    monkeypatch.setenv("ALPHORA_OBSERVABILITY_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ALPHORA_OBSERVABILITY_LEGACY_TEXT_LOG_ENABLED", "false")
    monkeypatch.setenv("ALPHORA_OBSERVABILITY_CRASH_HOOKS_ENABLED", "false")
    monkeypatch.setattr(main, "load_config", lambda: config)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeGovernor:
        @classmethod
        def from_config(cls, config, client):
            return cls()

        def start(self):
            return None

    monkeypatch.setattr(main, "BinanceFuturesClient", FakeClient)
    monkeypatch.setattr(main, "AlphoraGovernor", FakeGovernor)

    main.main()

    run_dirs = [path for path in (tmp_path / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    summary = json.loads(
        (run_dir / "reports" / "observability_summary.json").read_text(encoding="utf-8")
    )
    assert manifest["run_id"] == summary["run_id"]
    assert summary["status"] == "COMPLETED"
