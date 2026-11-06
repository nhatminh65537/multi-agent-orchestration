"""Configuration loader for coord.

Loads key-value environment variables from a .env or config file without external dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path | None = None) -> Path | None:
    """Load key-value pairs from an env/config file into os.environ.

    - If path is provided: resolves and loads it. Raises FileNotFoundError if missing.
    - If path is None: looks for .env in current working directory. If found, loads it.
    - Existing environment variables are NOT overwritten, allowing shell overrides.

    Returns:
        Path of the loaded file, or None if no file was loaded.
    """
    target: Path | None = None
    if path:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(f"Không tìm thấy file config: {target}")
    else:
        candidate = Path(".env").resolve()
        if candidate.is_file():
            target = candidate

    if not target or not target.is_file():
        return None

    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Strip matching outer quotes
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        # Only set non-empty values if not already set in environment
        if key and val and key not in os.environ:
            os.environ[key] = val

    return target
