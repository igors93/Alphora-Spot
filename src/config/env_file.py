"""Locate and load the ``.env`` file from an explicit, predictable place.

``load_dotenv()`` without a path searches upward from the calling module's
directory, which ties the result to where the code is installed: an installed
wheel never sees the project's ``.env``. Here the candidates are fixed and the
loaded path is returned so callers can report it.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE_VARIABLE = "ALPHORA_ENV_FILE"


def load_env_file() -> str:
    """Load the first ``.env`` candidate and return its resolved path ("" if none).

    Candidates, in order: ``ALPHORA_ENV_FILE`` (must exist), then ``.env`` in
    ``ALPHORA_PROJECT_ROOT`` (the desktop workspace), the current working directory
    and the source checkout containing this code. Only one file is loaded, and
    variables already set in the process always win over the file.
    """

    explicit = os.getenv(ENV_FILE_VARIABLE, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ValueError(f"{ENV_FILE_VARIABLE} aponta para um arquivo inexistente: {path}")
        candidates = [path]
    else:
        candidates = [directory / ".env" for directory in _env_file_directories()]

    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate)
            return str(candidate.resolve())
    return ""


def _env_file_directories() -> list[Path]:
    directories = []
    project_root = os.getenv("ALPHORA_PROJECT_ROOT", "").strip()
    if project_root:
        directories.append(Path(project_root).expanduser())
    directories.append(Path.cwd())
    checkout = _source_checkout_root()
    if checkout is not None:
        directories.append(checkout)
    return directories


def _source_checkout_root() -> Path | None:
    for directory in Path(__file__).resolve().parents:
        if (directory / "main.py").is_file() and (directory / "pyproject.toml").is_file():
            return directory
    return None
