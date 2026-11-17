from __future__ import annotations

from utils.process_metrics import process_memory_snapshot


def test_process_memory_snapshot_shape():
    snapshot = process_memory_snapshot()

    assert set(snapshot) == {"pid", "rss_mb", "peak_rss_mb"}
    assert isinstance(snapshot["pid"], int)
    for key in ("rss_mb", "peak_rss_mb"):
        value = snapshot[key]
        assert value is None or (isinstance(value, float) and value > 0)
