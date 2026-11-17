import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class JournalRotationEntry:
    source_path: str
    archived_path: str | None
    status: str
    bytes: int = 0
    reason: str = ""


@dataclass(frozen=True)
class JournalRotationResult:
    label: str
    archive_dir: str | None
    entries: list[JournalRotationEntry]

    @property
    def moved_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "moved")

    @property
    def missing_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "missing")

    @property
    def empty_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "empty")

    @property
    def skipped_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "skipped")


def rotate_journal_files(
    paths: list[Path],
    *,
    label: str,
    archive_root: Path | str = Path("data/audit"),
    create_empty: bool = True,
    now: datetime | None = None,
) -> JournalRotationResult:
    # Release cached append handles so shutil.move works on Windows and new
    # events land in the recreated files instead of the archived inodes.
    from utils.jsonl_writer import shared_jsonl_writer

    shared_jsonl_writer().close_all()

    timestamp = _rotation_timestamp(now)
    safe_label = _safe_label(label)
    archive_dir = Path(archive_root) / f"{timestamp}_{safe_label}"

    entries: list[JournalRotationEntry] = []
    moved_any = False

    for path in _unique_paths(paths):
        source = Path(path)

        if not source.exists():
            entries.append(
                JournalRotationEntry(
                    source_path=source.as_posix(),
                    archived_path=None,
                    status="missing",
                    reason="Arquivo não existe.",
                )
            )
            continue

        if not source.is_file():
            entries.append(
                JournalRotationEntry(
                    source_path=source.as_posix(),
                    archived_path=None,
                    status="skipped",
                    reason="Caminho não é arquivo.",
                )
            )
            continue

        size = source.stat().st_size
        if size <= 0:
            entries.append(
                JournalRotationEntry(
                    source_path=source.as_posix(),
                    archived_path=None,
                    status="empty",
                    reason="Arquivo vazio; nada para arquivar.",
                )
            )
            continue

        target = archive_dir / _archive_relative_path(source)
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(source), str(target))
        moved_any = True

        if create_empty:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.touch()

        entries.append(
            JournalRotationEntry(
                source_path=source.as_posix(),
                archived_path=target.as_posix(),
                status="moved",
                bytes=size,
            )
        )

    if moved_any:
        _write_manifest(
            archive_dir=archive_dir,
            result=JournalRotationResult(
                label=label,
                archive_dir=archive_dir.as_posix(),
                entries=entries,
            ),
        )

    return JournalRotationResult(
        label=label,
        archive_dir=archive_dir.as_posix() if moved_any else None,
        entries=entries,
    )


def format_journal_rotation_result(result: JournalRotationResult) -> str:
    lines = [
        f"Rotação de journal - {result.label}",
        "",
        f"Arquivos arquivados: {result.moved_count}",
        f"Arquivos vazios ignorados: {result.empty_count}",
        f"Arquivos ausentes: {result.missing_count}",
        f"Arquivos ignorados: {result.skipped_count}",
    ]

    if result.archive_dir:
        lines.append(f"Pasta de auditoria: {result.archive_dir}")
    else:
        lines.append("Pasta de auditoria: nenhuma criada")

    moved_entries = [entry for entry in result.entries if entry.status == "moved"]
    if moved_entries:
        lines.extend(["", "Arquivados:"])
        for entry in moved_entries:
            lines.append(f"- {entry.source_path} -> {entry.archived_path} ({entry.bytes} bytes)")

    if not moved_entries:
        lines.extend(
            [
                "",
                "Nenhum arquivo com conteúdo foi arquivado.",
            ]
        )

    lines.extend(
        [
            "",
            "Interpretação:",
            (
                "Os arquivos antigos foram preservados para auditoria e novos arquivos "
                "vazios foram criados nos caminhos originais."
                if moved_entries
                else "Nada foi rotacionado porque não havia arquivos com conteúdo."
            ),
        ]
    )

    return "\n".join(lines)


def _write_manifest(
    *,
    archive_dir: Path,
    result: JournalRotationResult,
) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rotation_timestamp(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_label(label: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(label or "journal").strip()
    ).strip("_")
    return safe or "journal"


def _archive_relative_path(path: Path) -> Path:
    if path.is_absolute():
        return Path(path.name)

    return path


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()

    for path in paths:
        normalized = Path(path)
        key = normalized.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)

    return unique
