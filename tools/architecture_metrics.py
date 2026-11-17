"""Architecture metrics for the migration plan (docs/MIGRATION_PLAN.md).

Measures, with the standard library only, the indicators the plan tracks:
package sizes, cyclic package dependencies, oversized functions, Governor mixin
coupling, untyped evaluation-context fields and leftover legacy names.

Usage:
    python tools/architecture_metrics.py            # human-readable report
    python tools/architecture_metrics.py --json     # machine-readable, for CI trends
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"

GOVERNOR_CLASS = ("governor/alphora_governor.py", "AlphoraGovernor")
# The Governor class and what was extracted from its mixins (migration M3).
GOVERNOR_SCOPE = (
    "governor/alphora_governor.py:",
    "governor/composition.py:",
    "governor/cycle/",
    "governor/evaluation/",
    "governor/services/",
    "governor/stages/",
)
EVALUATION_CONTEXT = ("governor/symbol_evaluation_pipeline.py", "SymbolEvaluationContext")
LEGACY_NAME = "coalestra"
# ADR-002: persisted wire names keep the legacy spelling until the M7 event schema. A module
# whose docstring carries LEGACY_MODULE_MARKER, or a line carrying LEGACY_LINE_MARKER, holds
# them on purpose; every other occurrence in src/ is a leftover the ratchet counts.
LEGACY_MODULE_MARKER = "legacy-wire-names (ADR-002)"
LEGACY_LINE_MARKER = "legacy-wire-name (ADR-002)"
# Migration M5 exit criterion: no snapshot provider module above this many lines.
SNAPSHOT_PROVIDER = "market_data/snapshot_provider/"
SNAPSHOT_PROVIDER_MAX_FILE_LINES = 800
LONG_FUNCTION = 150
# Migration M7 (ADR-006): events go through utils.events; these are the calls left on the
# legacy audit functions, which only utils/audit_logger.py itself may still use.
LEGACY_EVENT_CALLS = {"audit_event", "audit_error"}
LEGACY_EVENT_HOME = "utils/audit_logger.py"
# Migration M7: one owner per journal file. Journals come from the composition root's
# JournalRegistry; this counts TradeJournal(...) built anywhere else.
JOURNAL_CLASS = "TradeJournal"
JOURNAL_OWNER = "utils/journals.py"

# Architecture layers from docs/MIGRATION_PLAN.md §2.1. A package may import packages
# of its own layer or of the layers below it; every top-level package needs a layer.
LAYERS = {
    # 0 · base
    "config": 0,
    "utils": 0,
    "module_paths": 0,
    "operational_module_modes": 0,
    "trading_cadence": 0,
    # 1 · domain
    "domain": 1,
    "futures": 1,
    "indicators": 1,
    "scanner": 1,
    "strategies": 1,
    "trading": 1,
    # 2 · infrastructure
    "binance": 2,
    "execution": 2,
    "market_data": 2,
    "observability": 2,
    "resilience": 2,
    # 3 · application
    "backtesting": 3,
    "governor": 3,
    "research": 3,
    # 4 · entry points
    "desktop": 4,
    "engine": 4,
    "ops": 4,
}


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _code_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def _owner(relative: Path) -> str:
    return relative.parts[0].removesuffix(".py") if len(relative.parts) > 1 else relative.stem


def collect() -> dict:
    top_level = {p.name for p in SRC.iterdir() if (p / "__init__.py").exists()}
    top_level |= {p.stem for p in SRC.glob("*.py") if p.stem != "__init__"}

    lines: Counter[str] = Counter()
    edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    functions: list[tuple[int, str]] = []
    legacy_refs = 0
    legacy_files = 0
    legacy_marked = 0
    provider_files: list[tuple[int, str]] = []
    legacy_event_calls: list[str] = []
    journal_constructions: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        owner = _owner(relative)
        text = path.read_text(encoding="utf-8", errors="replace")
        lines[owner] += _code_lines(text)
        tree = _parse(path)
        unmarked, marked = _legacy_refs(text, tree)
        legacy_refs += unmarked
        legacy_marked += marked
        legacy_files += 1 if unmarked else 0
        if relative.as_posix().startswith(SNAPSHOT_PROVIDER):
            provider_files.append((len(text.splitlines()), relative.as_posix()))
        if tree is None:
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [node.module]
            elif isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = (node.end_lineno or node.lineno) - node.lineno + 1
                functions.append((size, f"{relative.as_posix()}:{node.lineno} {node.name}"))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in LEGACY_EVENT_CALLS
                and relative.as_posix() != LEGACY_EVENT_HOME
            ):
                legacy_event_calls.append(f"{relative.as_posix()}:{node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == JOURNAL_CLASS
                and relative.as_posix() != JOURNAL_OWNER
            ):
                journal_constructions.append(f"{relative.as_posix()}:{node.lineno}")
            for target in targets:
                package = target.split(".")[0]
                if package in top_level and package != owner:
                    edges[(owner, package)].append(f"{relative.as_posix()}:{node.lineno}")

    cycles = []
    for (a, b), sites in sorted(edges.items()):
        if a < b and (b, a) in edges:
            back = sites if len(sites) <= len(edges[(b, a)]) else edges[(b, a)]
            cycles.append({"pair": f"{a} <-> {b}", "back_edges": back})

    functions.sort(reverse=True)
    return {
        "total_lines": sum(lines.values()),
        "lines_by_package": dict(lines.most_common()),
        "package_cycles": cycles,
        "functions_over_150": sum(1 for size, _ in functions if size > LONG_FUNCTION),
        "largest_functions": [{"lines": size, "where": where} for size, where in functions[:15]],
        "governor_mixins": _mixin_coupling(),
        "governor_dynamic_self_lookups": _governor_dynamic_self_lookups(),
        "largest_governor_function": next(
            (
                {"lines": size, "where": where}
                for size, where in functions
                if where.startswith(GOVERNOR_SCOPE)
            ),
            {"lines": 0, "where": ""},
        ),
        "untyped_context_fields": _untyped_context_fields(),
        "legacy_name_refs": {
            "name": LEGACY_NAME,
            "refs": legacy_refs,
            "files": legacy_files,
            "marked_wire_names": legacy_marked,
        },
        "largest_snapshot_provider_file": (
            {"lines": max(provider_files)[0], "where": max(provider_files)[1]}
            if provider_files
            else {"lines": 0, "where": ""}
        ),
        "legacy_event_calls": legacy_event_calls,
        "journal_constructions_outside_owner": journal_constructions,
        "tests_by_area": _tests_by_area(),
        "mypy_ignored_modules": _mypy_ignored_modules(),
        "governor_new_bypass_files": _governor_new_bypass_files(),
        "layer_violations": _layer_violations(edges),
        "unlayered_packages": sorted(top_level - LAYERS.keys()),
    }


def _legacy_refs(text: str, tree: ast.Module | None) -> tuple[int, int]:
    """Occurrences of the legacy name: (unmarked leftovers, marked wire names)."""
    docstring = (ast.get_docstring(tree) or "") if tree is not None else ""
    if LEGACY_MODULE_MARKER in docstring:
        return 0, text.lower().count(LEGACY_NAME)
    unmarked = marked = 0
    for line in text.splitlines():
        count = line.lower().count(LEGACY_NAME)
        if LEGACY_LINE_MARKER in line:
            marked += count
        else:
            unmarked += count
    return unmarked, marked


def _layer_violations(edges: dict[tuple[str, str], list[str]]) -> dict[str, list[str]]:
    """Imports that point to a higher layer, keyed by "source -> target" package."""
    return {
        f"{source} -> {target}": sites
        for (source, target), sites in sorted(edges.items())
        if source in LAYERS and target in LAYERS and LAYERS[target] > LAYERS[source]
    }


def _governor_mixins() -> dict[str, str]:
    """Map each mixin base of ``AlphoraGovernor`` to its module path under ``src``."""

    relative, class_name = GOVERNOR_CLASS
    tree = _parse(SRC / relative)
    if tree is None:
        return {}
    modules = {
        alias.asname or alias.name: node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    bases = next(
        (
            [base.id for base in node.bases if isinstance(base, ast.Name)]
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        [],
    )
    return {
        base: modules[base].replace(".", "/") + ".py"
        for base in bases
        if base.endswith("Mixin") and base in modules
    }


def _mixin_coupling() -> dict:
    mixins = _governor_mixins()
    defined: dict[str, set[str]] = {}
    for name, relative in mixins.items():
        tree = _parse(SRC / relative)
        defined[name] = (
            {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)} if tree else set()
        )
    report = {}
    for name, relative in mixins.items():
        tree = _parse(SRC / relative)
        if tree is None:
            continue
        foreign_calls: Counter[str] = Counter()
        dynamic = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"getattr", "hasattr", "setattr"}:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Name) and first.id == "self":
                    dynamic += 1
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                for other, methods in defined.items():
                    if other != name and func.attr in methods and func.attr not in defined[name]:
                        foreign_calls[other] += 1
        report[name] = {
            "methods": len(defined[name]),
            "calls_into_other_mixins": dict(foreign_calls),
            "dynamic_self_lookups": dynamic,
        }
    return report


def _dynamic_self_lookups(tree: ast.AST) -> int:
    """``getattr``/``hasattr``/``setattr`` calls whose first argument is ``self``."""

    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr", "setattr"}
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "self"
    )


def _governor_dynamic_self_lookups() -> int:
    """Dynamic ``self`` lookups in ``AlphoraGovernor`` and in every mixin it still inherits."""

    modules = {GOVERNOR_CLASS[0], *_governor_mixins().values()}
    total = 0
    for relative in sorted(modules):
        tree = _parse(SRC / relative)
        if tree is not None:
            total += _dynamic_self_lookups(tree)
    return total


def _untyped_context_fields() -> int:
    relative, class_name = EVALUATION_CONTEXT
    tree = _parse(SRC / relative)
    if tree is None:
        return 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sum(
                1
                for item in node.body
                if isinstance(item, ast.AnnAssign)
                and isinstance(item.annotation, ast.Name)
                and item.annotation.id == "Any"
            )
    return 0


def _tests_by_area() -> dict[str, int]:
    counts: Counter[str] = Counter()
    for path in TESTS.rglob("test_*.py"):
        tree = _parse(path)
        if tree is None:
            continue
        area = (
            path.relative_to(TESTS).parts[0] if len(path.relative_to(TESTS).parts) > 1 else "root"
        )
        counts[area] += sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        )
    return dict(counts.most_common())


def _mypy_ignored_modules() -> int:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    sections = text.split("[[tool.mypy.overrides]]")
    return sum(
        len(re.findall(r'"([\w.]+)"', section.split("module =", 1)[1].split("]", 1)[0]))
        for section in sections[1:]
        if "ignore_errors = true" in section and "module =" in section
    )


def _governor_new_bypass_files() -> int:
    count = 0
    for path in TESTS.rglob("test_*.py"):
        tree = _parse(path)
        if tree is None:
            continue
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__new__"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"object", "AlphoraGovernor"}
            and any(isinstance(arg, ast.Name) and arg.id == "AlphoraGovernor" for arg in node.args)
            for node in ast.walk(tree)
        ):
            count += 1
    return count


def _print_report(data: dict) -> None:
    print(f"Linhas de código em src/: {data['total_lines']}")
    print("\nPor pacote:")
    for package, count in data["lines_by_package"].items():
        print(f"  {package:28s} {count:7d}")
    print(f"\nCiclos entre pacotes: {len(data['package_cycles'])}")
    for cycle in data["package_cycles"]:
        print(f"  {cycle['pair']:32s} retorno: {', '.join(cycle['back_edges'][:3])}")
    print(f"\nFunções com mais de {LONG_FUNCTION} linhas: {data['functions_over_150']}")
    for item in data["largest_functions"][:10]:
        print(f"  {item['lines']:5d}  {item['where']}")
    print("\nMixins do Governor:")
    for name, info in data["governor_mixins"].items():
        calls = sum(info["calls_into_other_mixins"].values())
        print(
            f"  {name:20s} métodos={info['methods']:3d} "
            f"chamadas_a_outros_mixins={calls:3d} getattr(self)={info['dynamic_self_lookups']:3d}"
        )
    print(f"getattr(self) no Governor e mixins: {data['governor_dynamic_self_lookups']}")
    largest = data["largest_governor_function"]
    print(f"Maior função do Governor: {largest['lines']} linhas ({largest['where']})")
    print(f"\nCampos Any no SymbolEvaluationContext: {data['untyped_context_fields']}")
    legacy = data["legacy_name_refs"]
    print(
        f"Referências ao nome '{legacy['name']}' fora de wire names marcados (ADR-002): "
        f"{legacy['refs']} em {legacy['files']} arquivos "
        f"(wire names marcados: {legacy['marked_wire_names']})"
    )
    provider = data["largest_snapshot_provider_file"]
    print(
        f"Maior módulo do snapshot provider: {provider['lines']} linhas ({provider['where']}); "
        f"limite {SNAPSHOT_PROVIDER_MAX_FILE_LINES}"
    )
    print("\nTestes por área:")
    for area, count in data["tests_by_area"].items():
        print(f"  {area:20s} {count:5d}")
    print(f"Mypy ignored modules: {data['mypy_ignored_modules']}")
    print(f"Legacy audit_event/audit_error calls: {len(data['legacy_event_calls'])}")
    journals = data["journal_constructions_outside_owner"]
    print(f"TradeJournal built outside the JournalRegistry: {len(journals)}")
    print(f"Governor __new__ bypass test files: {data['governor_new_bypass_files']}")
    print(f"\nLayer violations: {len(data['layer_violations'])}")
    for edge, sites in data["layer_violations"].items():
        print(f"  {edge:32s} {len(sites):3d} imports, e.g. {sites[0]}")
    if data["unlayered_packages"]:
        print(f"Packages without a layer: {', '.join(data['unlayered_packages'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="imprime JSON")
    args = parser.parse_args(argv)
    data = collect()
    if args.json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_report(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
