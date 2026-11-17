from utils.jsonl import iter_lines, parse_json_object_line, tail_lines


def test_tail_lines_reads_only_requested_tail(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("a\nb\nc\n", encoding="utf-8")

    assert tail_lines(path, 2) == ["b", "c"]
    assert list(iter_lines(path)) == ["a", "b", "c"]
    assert tail_lines(path, 0) == []


def test_tail_lines_handles_limit_larger_than_file(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("a\nb\n", encoding="utf-8")

    assert tail_lines(path, 10) == ["a", "b"]


def test_tail_lines_without_trailing_newline(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("a\nb\nc", encoding="utf-8")

    assert tail_lines(path, 2) == ["b", "c"]
    assert tail_lines(path, 3) == ["a", "b", "c"]


def test_tail_lines_missing_and_empty_file(tmp_path):
    assert tail_lines(tmp_path / "missing.jsonl", 5) == []

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert tail_lines(empty, 5) == []


def test_tail_lines_crosses_chunk_boundaries(tmp_path):
    # Lines long enough that the requested tail spans multiple 64 KiB chunks.
    path = tmp_path / "events.jsonl"
    lines = [f"line-{index:05d}-" + "x" * 200 for index in range(2000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert tail_lines(path, 700) == lines[-700:]
    assert tail_lines(path, 1) == lines[-1:]


def test_parse_json_object_line_accepts_only_json_objects():
    assert parse_json_object_line('{"event_type": "probe"}') == {"event_type": "probe"}
    assert parse_json_object_line("[1, 2, 3]") is None
    assert parse_json_object_line("{bad json") is None
    assert parse_json_object_line("") is None
