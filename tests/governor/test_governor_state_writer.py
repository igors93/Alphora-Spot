import json
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest

import governor.state_writer as state_writer_module
from governor.state_writer import GovernorStateWriter


class ExampleStatus(Enum):
    OK = "OK"


class BrokenString:
    def __str__(self) -> str:
        raise RuntimeError("synthetic serialization failure")


def _temporary_files_for(state_path: Path) -> list[Path]:
    return list(state_path.parent.glob(f".{state_path.name}.*.tmp"))


def test_governor_state_writer_creates_parent_and_writes_json(
    tmp_path,
):
    state_path = tmp_path / "runtime" / "governor_state.json"
    writer = GovernorStateWriter(state_path)

    writer.write(
        {
            "symbol": "BTCUSDT",
            "status": ExampleStatus.OK,
            "value": Decimal("1.23"),
        }
    )

    assert state_path.exists()

    raw_content = state_path.read_text(encoding="utf-8")
    assert raw_content.endswith("\n")

    payload = json.loads(raw_content)

    assert payload == {
        "symbol": "BTCUSDT",
        "status": "OK",
        "value": "1.23",
    }
    assert _temporary_files_for(state_path) == []


def test_governor_state_writer_replaces_existing_file(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "runtime" / "governor_state.json"
    state_path.parent.mkdir(parents=True)

    state_path.write_text(
        '{"version": "old"}\n',
        encoding="utf-8",
    )

    writer = GovernorStateWriter(state_path)

    original_replace = state_writer_module.os.replace
    replace_paths = {}

    def capture_replace(source, destination):
        replace_paths["source"] = Path(source)
        replace_paths["destination"] = Path(destination)
        original_replace(source, destination)

    monkeypatch.setattr(
        state_writer_module.os,
        "replace",
        capture_replace,
    )

    writer.write({"version": "new"})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": "new",
    }

    assert replace_paths["source"].parent == state_path.parent
    assert replace_paths["destination"] == state_path
    assert _temporary_files_for(state_path) == []


def test_governor_state_writer_preserves_existing_file_when_replace_fails(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "runtime" / "governor_state.json"
    state_path.parent.mkdir(parents=True)

    previous_content = '{"version": "old"}\n'

    state_path.write_text(
        previous_content,
        encoding="utf-8",
    )

    writer = GovernorStateWriter(state_path)

    def fail_replace(_source, _destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(
        state_writer_module.os,
        "replace",
        fail_replace,
    )

    with pytest.raises(
        OSError,
        match="synthetic replace failure",
    ):
        writer.write({"version": "new"})

    assert state_path.read_text(encoding="utf-8") == previous_content
    assert _temporary_files_for(state_path) == []


def test_governor_state_writer_preserves_existing_file_on_serialization_error(
    tmp_path,
):
    state_path = tmp_path / "runtime" / "governor_state.json"
    state_path.parent.mkdir(parents=True)

    previous_content = '{"version": "old"}\n'

    state_path.write_text(
        previous_content,
        encoding="utf-8",
    )

    writer = GovernorStateWriter(state_path)

    with pytest.raises(
        RuntimeError,
        match="synthetic serialization failure",
    ):
        writer.write(
            {
                "broken": BrokenString(),
            }
        )

    assert state_path.read_text(encoding="utf-8") == previous_content
    assert _temporary_files_for(state_path) == []
