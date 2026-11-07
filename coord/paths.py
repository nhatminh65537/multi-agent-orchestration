"""Filesystem layout helpers shared by every module.

`.coord/` lives in the **main** repository (all worktrees share one git common
dir). `coord.db` lives in `COORD_HOME`, one server per machine.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

TEMPLATES = Path(__file__).parent / "templates"


def git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def project_root(worktree: Path) -> Path:
    """Main repo that owns `worktree` (shared git common dir). Falls back to it."""
    try:
        common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(common).parent
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return Path(worktree).resolve()


def coord_dir(worktree: Path) -> Path:
    override = os.environ.get("COORD_DIR")
    if override:
        return Path(override)
    return project_root(Path(worktree)) / ".coord"


def coord_home() -> Path:
    override = os.environ.get("COORD_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "coord"

