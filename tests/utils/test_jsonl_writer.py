from __future__ import annotations

import shutil

from utils.jsonl_writer import JsonlAppendWriter


def test_append_line_creates_parents_and_appends(tmp_path):
    writer = JsonlAppendWriter()
    target = tmp_path / "nested" / "events.jsonl"

    writer.append_line(target, '{"a": 1}')
    writer.append_line(target, '{"a": 2}\n')
    writer.close_all()

    assert target.read_text(encoding="utf-8") == '{"a": 1}\n{"a": 2}\n'


def test_append_line_reopens_after_rotation(tmp_path):
    writer = JsonlAppendWriter()
    target = tmp_path / "events.jsonl"
    archive = tmp_path / "archive" / "events.jsonl"

    writer.append_line(target, "before")

    archive.parent.mkdir(parents=True)
    shutil.move(str(target), str(archive))
    target.touch()

    writer.append_line(target, "after")
    writer.close_all()

    assert archive.read_text(encoding="utf-8") == "before\n"
    assert target.read_text(encoding="utf-8") == "after\n"


def test_append_line_recreates_deleted_file(tmp_path):
    writer = JsonlAppendWriter()
    target = tmp_path / "events.jsonl"

    writer.append_line(target, "first")
    target.unlink()
    writer.append_line(target, "second")
    writer.close_all()

    assert target.read_text(encoding="utf-8") == "second\n"


def test_lru_eviction_keeps_writing_correctly(tmp_path):
    writer = JsonlAppendWriter(max_open_handles=2)
    paths = [tmp_path / f"journal_{index}.jsonl" for index in range(4)]

    for round_index in range(2):
        for path in paths:
            writer.append_line(path, f"round-{round_index}")
    writer.close_all()

    for path in paths:
        assert path.read_text(encoding="utf-8") == "round-0\nround-1\n"
