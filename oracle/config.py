"""Configuration and path helpers.

The oracle keeps one SQLite database per indexed repository under
``<repo>/.oracle/oracle.db`` by default. Nothing is written outside the
repository unless the user asks for it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_DIR = ".oracle"
DEFAULT_DB_NAME = "oracle.db"

# Directories that never contain hand-written source worth indexing.
SKIP_DIRS = {
    ".git",
    ".oracle",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "bin",
    "tmp",
}

# BYOND build artifacts and generated files.
SKIP_SUFFIXES = {
    ".dmb",
    ".rsc",
    ".lk",
    ".dyn",
    ".int",
    ".sav",
    ".log",
}


@dataclass(frozen=True)
class RepoPaths:
    root: Path
    db_path: Path

    @classmethod
    def discover(cls, root: str | os.PathLike[str], db: str | None = None) -> "RepoPaths":
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise FileNotFoundError(f"repository not found: {root_path}")
        db_path = Path(db).expanduser().resolve() if db else root_path / DEFAULT_DB_DIR / DEFAULT_DB_NAME
        return cls(root=root_path, db_path=db_path)


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".git")


def is_source_file(path: Path) -> bool:
    return path.suffix.lower() == ".dm" and path.suffix.lower() not in SKIP_SUFFIXES


def iter_dm_files(root: Path):
    """Yield every .dm file under *root*, skipping vendor/build directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not should_skip_dir(d))
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if is_source_file(path):
                yield path
