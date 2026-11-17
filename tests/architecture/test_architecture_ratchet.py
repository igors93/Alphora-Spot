"""Keep structural migration metrics at or below their committed ceilings."""

import json
import warnings
from pathlib import Path

from tools.architecture_metrics import collect


def test_architecture_ratchet() -> None:
    baseline_path = Path(__file__).with_name("baseline.json")
    ceilings = json.loads(baseline_path.read_text(encoding="utf-8"))
    report = collect()
    measured = {
        "package_cycles": len(report["package_cycles"]),
        "functions_over_150": report["functions_over_150"],
        "largest_function_lines": report["largest_functions"][0]["lines"],
        "untyped_context_fields": report["untyped_context_fields"],
        "governor_mixin_count": len(report["governor_mixins"]),
        "governor_dynamic_self_lookups": report["governor_dynamic_self_lookups"],
        "largest_governor_function_lines": report["largest_governor_function"]["lines"],
        "mypy_ignored_modules": report["mypy_ignored_modules"],
        "governor_new_bypass_files": report["governor_new_bypass_files"],
        "legacy_name_refs": report["legacy_name_refs"]["refs"],
        "largest_snapshot_provider_file_lines": report["largest_snapshot_provider_file"]["lines"],
        "legacy_event_calls": len(report["legacy_event_calls"]),
        "journal_constructions_outside_owner": len(report["journal_constructions_outside_owner"]),
    }
    assert measured.keys() == ceilings.keys(), "The architecture baseline is missing a metric"
    regressions = {
        key: (value, ceilings[key]) for key, value in measured.items() if value > ceilings[key]
    }
    assert not regressions, f"Architecture metrics exceeded their ceilings: {regressions}"
    improvements = {
        key: (value, ceilings[key]) for key, value in measured.items() if value < ceilings[key]
    }
    if improvements:
        warnings.warn(
            f"Lower tests/architecture/baseline.json ceilings: {improvements}",
            stacklevel=1,
        )
