from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APPLICATION_DIRECTORY_NAME = "Alphora"
WORKSPACE_DIRECTORIES = (
    "config",
    "data",
    "data/audit",
    "data/market",
    "logs",
    "models/ai/global",
    "models/ai/refined",
    "reports",
    "runtime",
)


def discover_source_root(start: str | Path | None = None) -> Path | None:
    candidate = Path(start or __file__).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent

    for directory in (candidate, *candidate.parents):
        if (directory / "main.py").is_file() and (directory / "pyproject.toml").is_file():
            return directory
    return None


def resolve_workspace_root(
    *,
    source_hint: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: str | Path | None = None,
) -> Path:
    """Workspace holding ``config/``, ``data/``, ``runtime/`` and friends.

    Priority: ``ALPHORA_PROJECT_ROOT``, then the source checkout that contains the running
    code, then a per-user application directory (an installed wheel).
    """
    environment = os.environ if environ is None else environ
    explicit = str(environment.get("ALPHORA_PROJECT_ROOT", "")).strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    if source_hint is not None:
        source_root = discover_source_root(source_hint)
        if source_root is not None:
            return source_root

    user_home = Path(home or Path.home()).expanduser()
    platform = (platform_name or sys.platform).lower()
    if platform.startswith("win"):
        base = Path(
            environment.get("LOCALAPPDATA")
            or environment.get("APPDATA")
            or user_home / "AppData" / "Local"
        )
    elif platform == "darwin":
        base = user_home / "Library" / "Application Support"
    else:
        base = Path(environment.get("XDG_DATA_HOME") or user_home / ".local" / "share")

    return (base / APPLICATION_DIRECTORY_NAME).resolve()


def prepare_workspace(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative in WORKSPACE_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root
