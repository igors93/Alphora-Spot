from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from desktop.intelligent_events import (
    build_intelligent_event_paths,
    load_intelligent_events_from_paths,
)
from desktop.interface_process_review import (
    interface_journal_paths,
    interface_state_paths,
)
from desktop.journal_rotation import (
    JournalRotationResult,
    format_journal_rotation_result,
    rotate_journal_files,
)
from utils.jsonl import parse_json_object_line, tail_lines
from utils.privacy import sanitize_for_audit

SNAPSHOT_SECTIONS = frozenset({"events", "journals", "scanner", "state", "sources"})

# The interface refreshes every few seconds and journal tails mostly repeat between
# refreshes; sanitizing is deterministic, so rendered lines are memoized.
_RENDER_CACHE_SIZE = 4096


@dataclass(frozen=True, slots=True)
class ObservabilitySource:
    kind: str
    display_name: str
    path: str
    exists: bool
    size_bytes: int
    modified_at: str

    def as_qml_map(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "displayName": self.display_name,
            "path": self.path,
            "exists": self.exists,
            "sizeBytes": self.size_bytes,
            "sizeLabel": format_size(self.size_bytes),
            "modifiedAt": self.modified_at,
        }


@dataclass(frozen=True, slots=True)
class ObservabilitySnapshot:
    events: tuple[dict[str, Any], ...]
    journal_text: str
    scanner_text: str
    state_text: str
    sources: tuple[ObservabilitySource, ...]
    journal_paths: tuple[str, ...]
    state_paths: tuple[str, ...]
    updated_at: str

    @property
    def existing_sources(self) -> int:
        return sum(1 for source in self.sources if source.exists)


@dataclass(frozen=True, slots=True)
class RotationSummary:
    kind: str
    message: str
    archive_directory: str
    moved_count: int


def build_observability_snapshot(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root_path: str | Path,
    event_limit: int = 100,
    journal_tail_lines: int = 500,
    scanner_tail_lines: int = 500,
    state_max_bytes: int = 2_000_000,
    sections: Collection[str] | None = None,
) -> ObservabilitySnapshot:
    """Build the requested snapshot sections; ``None`` builds all of them."""
    wanted = SNAPSHOT_SECTIONS if sections is None else SNAPSHOT_SECTIONS.intersection(sections)
    root = Path(root_path).resolve()
    journal_paths = interface_journal_paths(
        settings=settings,
        symbol_profiles=symbol_profiles,
    )
    state_paths = interface_state_paths(
        settings=settings,
        symbol_profiles=symbol_profiles,
    )
    intelligent_paths = build_intelligent_event_paths(
        settings=settings,
        symbol_profiles=symbol_profiles,
    )

    event_rows: tuple[dict[str, Any], ...] = ()
    if "events" in wanted:
        resolved_intelligent = [_resolve_under_root(root, path) for path in intelligent_paths]
        events = load_intelligent_events_from_paths(
            resolved_intelligent,
            limit=max(1, int(event_limit)),
            tail_limit=max(100, int(journal_tail_lines)),
        )
        event_rows = tuple(_event_row(event) for event in events)

    journal_text = ""
    if "journals" in wanted:
        journal_text = _render_jsonl_sources(
            root=root,
            paths=[path for path in journal_paths if path.name != "scanner_scores.jsonl"],
            limit=max(1, int(journal_tail_lines)),
            missing_message="Nenhum journal operacional foi encontrado.",
        )
    scanner_text = ""
    if "scanner" in wanted:
        scanner_text = _render_jsonl_sources(
            root=root,
            paths=[Path("data/scanner_scores.jsonl")],
            limit=max(1, int(scanner_tail_lines)),
            missing_message=(
                "Scanner scores não encontrado. Quando o Governor calcular os scores, "
                "as linhas recentes serão exibidas aqui."
            ),
        )
    state_text = ""
    if "state" in wanted:
        state_text = _render_state_sources(
            root=root,
            paths=state_paths,
            max_bytes=max(1024, int(state_max_bytes)),
        )

    sources: tuple[ObservabilitySource, ...] = ()
    if "sources" in wanted:
        source_pairs: list[tuple[str, Path]] = []
        source_pairs.extend(("journal", path) for path in journal_paths)
        source_pairs.extend(("state", path) for path in state_paths)
        source_pairs.extend(("lock", path) for path in _runtime_lock_paths(root))
        sources = tuple(
            _source_summary(root, kind, path) for kind, path in _unique_pairs(source_pairs)
        )

    return ObservabilitySnapshot(
        events=event_rows,
        journal_text=journal_text,
        scanner_text=scanner_text,
        state_text=state_text,
        sources=sources,
        journal_paths=tuple(path.as_posix() for path in journal_paths),
        state_paths=tuple(path.as_posix() for path in state_paths),
        updated_at=datetime.now().astimezone().strftime("%d/%m/%Y %H:%M:%S"),
    )


def rotate_observability_journals(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root_path: str | Path,
    kind: str,
) -> RotationSummary:
    root = Path(root_path).resolve()
    normalized_kind = str(kind or "journals").strip().lower()
    if normalized_kind == "intelligent":
        relative_paths = build_intelligent_event_paths(
            settings=settings,
            symbol_profiles=symbol_profiles,
        )
        label = "intelligent_events"
    elif normalized_kind == "journals":
        relative_paths = interface_journal_paths(
            settings=settings,
            symbol_profiles=symbol_profiles,
        )
        label = "journals"
    else:
        raise ValueError("Tipo de rotação inválido.")

    absolute_paths = [_resolve_under_root(root, path) for path in relative_paths]
    result = rotate_journal_files(
        absolute_paths,
        label=label,
        archive_root=root / "data" / "audit",
    )
    return _rotation_summary(normalized_kind, result)


def first_existing_source(
    *,
    root_path: str | Path,
    paths: tuple[str, ...] | list[str],
) -> Path | None:
    root = Path(root_path).resolve()
    for value in paths:
        candidate = _resolve_under_root(root, Path(value))
        if candidate.exists():
            return candidate
    return None


def resolve_project_path(root_path: str | Path, value: str | Path) -> Path:
    return _resolve_under_root(Path(root_path).resolve(), Path(value))


def format_size(size_bytes: int) -> str:
    size = max(0, int(size_bytes))
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _event_row(event: Any) -> dict[str, Any]:
    values = asdict(event)
    details = values.get("details", {})
    try:
        safe_details_json = _sanitized_json(json.dumps(details, sort_keys=True))
    except (TypeError, ValueError):
        safe_details_json = json.dumps(
            sanitize_for_audit(details), ensure_ascii=False, sort_keys=True
        )
    return {
        "timestamp": str(values.get("timestamp", "")),
        "severity": str(values.get("severity", "INFO")),
        "title": str(values.get("title", values.get("event_type", "Evento"))),
        "symbol": str(values.get("symbol", "")),
        "eventType": str(values.get("event_type", "")),
        "status": str(values.get("status", "")),
        "action": str(values.get("action", "")),
        "reason": str(values.get("reason", "")),
        "details": safe_details_json,
    }


@lru_cache(maxsize=_RENDER_CACHE_SIZE)
def _sanitized_json(raw_json: str) -> str:
    """Sanitized, key-sorted JSON for a JSON document (memoized by its text)."""
    return json.dumps(sanitize_for_audit(json.loads(raw_json)), ensure_ascii=False, sort_keys=True)


@lru_cache(maxsize=_RENDER_CACHE_SIZE)
def _render_journal_line(line: str) -> str:
    """One journal line as shown in the interface: sanitized JSON, or sanitized text."""
    payload = parse_json_object_line(line)
    if payload is None:
        return str(sanitize_for_audit(line))
    return json.dumps(sanitize_for_audit(payload), ensure_ascii=False, sort_keys=True)


def _render_jsonl_sources(
    *,
    root: Path,
    paths: list[Path],
    limit: int,
    missing_message: str,
) -> str:
    sections: list[str] = []
    for relative in _unique_paths(paths):
        path = _resolve_under_root(root, relative)
        if not path.is_file():
            continue
        rendered = [_render_journal_line(line) for line in tail_lines(path, limit)]
        body = "\n".join(rendered) if rendered else "(arquivo vazio)"
        sections.append(f"{_display_path(root, path)}\n{body}")
    return "\n\n".join(sections) if sections else missing_message


def _render_state_sources(*, root: Path, paths: list[Path], max_bytes: int) -> str:
    sections: list[str] = []
    for relative in _unique_paths(paths):
        path = _resolve_under_root(root, relative)
        if not path.is_file():
            continue
        sections.append(f"{_display_path(root, path)}\n{_read_state_file(path, max_bytes)}")

    locks = _runtime_lock_paths(root)
    if locks:
        lock_sections = []
        for path in locks:
            content = _read_text_limited(path, min(max_bytes, 64_000))
            lock_sections.append(f"{_display_path(root, path)}\n{sanitize_for_audit(content)}")
        sections.append("Locks de runtime\n" + "\n\n".join(lock_sections))
    else:
        sections.append("Locks de runtime\nNenhum lock encontrado.")

    if len(sections) == 1 and not locks:
        sections.insert(0, "Nenhum arquivo de estado foi encontrado.")
    return "\n\n".join(sections)


def _read_state_file(path: Path, max_bytes: int) -> str:
    raw = _read_text_limited(path, max_bytes)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return str(sanitize_for_audit(raw))
    safe_payload = sanitize_for_audit(payload)
    return json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True)


def _read_text_limited(path: Path, max_bytes: int) -> str:
    with path.open("rb") as file:
        payload = file.read(max_bytes + 1)
    truncated = len(payload) > max_bytes
    text = payload[:max_bytes].decode("utf-8", errors="replace")
    return text + ("\n\n[conteúdo truncado]" if truncated else "")


def _source_summary(root: Path, kind: str, relative: Path) -> ObservabilitySource:
    path = _resolve_under_root(root, relative)
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    modified = (
        datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime("%d/%m/%Y %H:%M:%S")
        if exists
        else "—"
    )
    return ObservabilitySource(
        kind=kind,
        display_name=path.name,
        path=_display_path(root, path),
        exists=exists,
        size_bytes=size,
        modified_at=modified,
    )


def _rotation_summary(kind: str, result: JournalRotationResult) -> RotationSummary:
    archive = str(result.archive_dir or "")
    return RotationSummary(
        kind=kind,
        message=format_journal_rotation_result(result),
        archive_directory=archive,
        moved_count=result.moved_count,
    )


def _runtime_lock_paths(root: Path) -> list[Path]:
    runtime = root / "runtime"
    if not runtime.is_dir():
        return []
    return sorted(runtime.glob("*.lock"), key=lambda path: path.name.lower())


def _resolve_under_root(root: Path, value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("Caminho fora da raiz do projeto.")
    return resolved


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _unique_pairs(pairs: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    unique: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for kind, path in pairs:
        key = (kind, path.as_posix())
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, path))
    return unique
