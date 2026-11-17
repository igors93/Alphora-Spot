from __future__ import annotations

import threading
from types import SimpleNamespace

from utils.run_context import (
    get_run_context,
    get_run_id,
    initialize_run_context,
    reset_run_context_for_testing,
)


def test_run_context_prefers_explicit_run_id(monkeypatch):
    monkeypatch.setenv("ALPHORA_RUN_ID", "env-run")

    context = initialize_run_context(run_id="explicit-run", parent_run_id="parent-run")

    assert context.run_id == "explicit-run"
    assert context.parent_run_id == "parent-run"
    assert get_run_id() == "explicit-run"


def test_run_context_uses_env_before_config(monkeypatch):
    monkeypatch.setenv("ALPHORA_RUN_ID", "env-run")
    config = SimpleNamespace(run_id="config-run")

    assert initialize_run_context(config=config).run_id == "env-run"


def test_run_context_uses_config_when_env_missing():
    config = SimpleNamespace(run_id="config-run", parent_run_id="config-parent")

    context = initialize_run_context(config=config)

    assert context.run_id == "config-run"
    assert context.parent_run_id == "config-parent"


def test_run_context_is_created_once():
    first = initialize_run_context(run_id="first")
    second = initialize_run_context(run_id="second")

    assert second is first
    assert second.run_id == "first"


def test_threads_share_same_run_id():
    initialize_run_context(run_id="thread-run")
    captured: list[str] = []

    def worker() -> None:
        captured.append(get_run_context().run_id)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)

    assert captured == ["thread-run"]


def test_reset_run_context_for_testing_allows_new_id():
    initialize_run_context(run_id="old-run")

    reset_run_context_for_testing()

    assert initialize_run_context(run_id="new-run").run_id == "new-run"
