"""Configuration loader — the single home of the `WORKER_*` settings.

`coord` reads configuration from exactly one namespace: `WORKER_*` keys in `.env`
or in the environment. There are no legacy `COORD_*` / `GEMINI_*` /
`ANTHROPIC_*` aliases any more; `COORD_HOME` / `COORD_DIR` stay internal.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_THINKING = "medium"
DEFAULT_BACKENDS = ("agy-sdk", "claude-sdk")
DEFAULT_MODELS = {
    "agy-sdk": "gemini-3.8-flash-medium",
    "claude-sdk": "claude-sonnet-4-5",
}
#: How long a worker may wait for the provider's quota to recover (15 minutes).
DEFAULT_QUOTA_WAIT = 900.0


def load_env_file(path: str | Path | None = None) -> Path | None:
    """Load `KEY=VALUE` pairs into `os.environ` without external dependencies.

    With no `path`: try `./.env`, then the main repo's `.env` (so a worktree
    inherits the main checkout's configuration). Existing variables win, so the
    shell can always override the file.
    """
    if path:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(f"Config file not found: {target}")
        candidates = [target]
    else:
        from .paths import project_root

        candidates = [Path(".env").resolve(), project_root(Path.cwd()) / ".env"]

    for candidate in candidates:
        if candidate.is_file():
            _apply(candidate)
            return candidate
    return None


def _apply(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value


# ------------------------------------------------------------------- settings
def default_backend() -> str:
    return (os.environ.get("WORKER_BACKEND") or "agy-sdk").strip().lower()


def default_model(backend: str | None = None) -> str:
    kind = (backend or default_backend()).lower()
    return os.environ.get("WORKER_MODEL") or DEFAULT_MODELS.get(kind, DEFAULT_MODELS["agy-sdk"])


def fallback_models() -> list[str]:
    """`WORKER_FALLBACK_MODEL=a,b,c` -> `["a", "b", "c"]` (order preserved)."""
    raw = os.environ.get("WORKER_FALLBACK_MODEL") or ""
    return [model.strip() for model in raw.split(",") if model.strip()]


def thinking() -> str:
    return os.environ.get("WORKER_THINKING") or DEFAULT_THINKING


def quota_wait_seconds() -> float:
    """Seconds a worker may wait for quota to recover; `0` disables waiting."""
    raw = os.environ.get("WORKER_QUOTA_WAIT_SECONDS") or ""
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_QUOTA_WAIT


def api_key() -> str | None:
    return os.environ.get("WORKER_API_KEY") or None


def base_url() -> str | None:
    return os.environ.get("WORKER_BASE_URL") or None
