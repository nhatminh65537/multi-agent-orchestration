"""`coord` command line: serve | up | down | msg | init | doctor | status."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import PROTOCOL_VERSION, __version__
from .backend import backend_status
from .config import (
    api_key,
    base_url,
    default_backend,
    default_model,
    quota_wait_seconds,
    thinking,
)
from .manager import DEFAULT_HOST, DEFAULT_PORT, build_app, build_manager
from .paths import TEMPLATES, coord_home

BEGIN = "<!-- coord:begin -->"
END = "<!-- coord:end -->"
MCP_URL = f"http://127.0.0.1:{DEFAULT_PORT}/mcp"

ALLOWED_TOOLS = [
    "mcp__coord__spawn_worker",
    "mcp__coord__send",
    "mcp__coord__answer",
    "mcp__coord__status",
    "mcp__coord__message_session",
    "Bash(coord:*)",
    "Bash(git diff:*)",
    "Bash(git worktree:*)",
    "Bash(git merge stream-*)",
]

# Orchestrators delegate through `coord` MCP tools, never through Claude Code's
# built-in subagents (that would bypass the worker model and burn the session quota).
DENIED_TOOLS = ["Agent", "Task"]

BOOTSTRAP_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    ".claude/settings.json",
    ".mcp.json",
    ".agents/skills/worker-protocol/SKILL.md",
)


# ------------------------------------------------------------------- helpers
def run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"{cmd[0]} failed")
    return result.stdout.strip()


def git_repo_root(path: Path) -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"], cwd=path))


def template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def upsert_block(path: Path, body: str) -> str:
    """Create/refresh a marked section without touching the rest of the file."""
    block = f"{BEGIN}\n{body.strip()}\n{END}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n", encoding="utf-8")
        return "created"

    text = path.read_text(encoding="utf-8")
    start = text.find(BEGIN)
    stop = text.find(END)
    if start != -1 and stop > start:
        updated = text[:start] + block + text[stop + len(END) :]
        if updated == text:
            return "unchanged"
        path.write_text(updated, encoding="utf-8")
        return "updated"

    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n" + block + "\n", encoding="utf-8")
    return "appended"


def write_text_file(path: Path, body: str) -> str:
    """Write a whole bootstrap file; `created` / `updated` / `unchanged`."""
    if path.is_file():
        if path.read_text(encoding="utf-8") == body:
            return "unchanged"
        path.write_text(body, encoding="utf-8")
        return "updated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return "created"


def sync_bootstrap(source: Path, target: Path) -> list[str]:
    """Copy the five bootstrap files from `source` into an existing worktree."""
    changed: list[str] = []
    for rel in BOOTSTRAP_FILES:
        src = source / rel
        if not src.is_file():
            continue
        body = src.read_text(encoding="utf-8")
        dst = target / rel
        if dst.is_file() and dst.read_text(encoding="utf-8") == body:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(body, encoding="utf-8")
        changed.append(rel)
    return changed


def merge_settings(path: Path) -> str:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return f"skipped (invalid JSON: {exc})"

    before = json.dumps(data, sort_keys=True)
    data.setdefault("crossSessionInbound", "accept")
    data.setdefault("effort", "medium")
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    for item in ALLOWED_TOOLS:
        if item not in allow:
            allow.append(item)
    deny = permissions.setdefault("deny", [])
    for item in DENIED_TOOLS:
        if item not in deny:
            deny.append(item)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "unchanged" if before == json.dumps(data, sort_keys=True) else "updated"


def merge_mcp_json(path: Path) -> str:
    """Register/replace the coord server project-locally (works without the plugin)."""
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return f"skipped (invalid JSON: {exc})"

    before = json.dumps(data, sort_keys=True)
    servers = data.setdefault("mcpServers", {})
    # Overwrite on purpose: re-running `coord init` must repair a stale URL.
    servers["coord"] = {"type": "http", "url": MCP_URL}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "unchanged" if before == json.dumps(data, sort_keys=True) else "updated"


def commit_bootstrap(repo: Path, paths: list[Path]) -> str:
    """Commit only the files `init` touches, so worktrees inherit them."""
    relatives = [str(p.relative_to(repo)) for p in paths if p.exists()]
    if not relatives:
        return "nothing to commit"
    run(["git", "add", "--", *relatives], cwd=repo)
    staged = run(["git", "diff", "--cached", "--name-only", "--", *relatives], cwd=repo)
    if not staged:
        return "nothing to commit"
    run(["git", "commit", "-q", "-m", "coord: bootstrap orchestration files", "--", *relatives], cwd=repo)
    return "committed"


def ensure_gitignore(repo: Path) -> str:
    path = repo / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = {line.strip() for line in text.splitlines()}
    if "/.coord/" in lines:
        return "unchanged"
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "/.coord/\n", encoding="utf-8")
    return "updated"


def server_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    repo = git_repo_root(Path(args.repo).resolve())
    names = args.names or ["A", "B", "C"]
    root = Path(args.worktree_root).expanduser()
    if not root.is_absolute():
        root = (repo / root).resolve()

    print(f"repo:      {repo}")
    print(f"worktrees: {root}")

    # 1. Project files first, so the bootstrap commit can carry them into worktrees.
    coord = repo / ".coord"
    (coord / "logs").mkdir(parents=True, exist_ok=True)
    state = coord / "STATE.md"
    if not state.exists():
        state.write_text(template("STATE.md"), encoding="utf-8")
        print("  + .coord/STATE.md")

    written = {
        "gitignore": repo / ".gitignore",
        "claude": repo / "CLAUDE.md",
        "agents": repo / "AGENTS.md",
        "settings": repo / ".claude" / "settings.json",
        "mcp": repo / ".mcp.json",
        "worker_skill": repo / ".agents" / "skills" / "worker-protocol" / "SKILL.md",
    }
    print(f"  = .gitignore: {ensure_gitignore(repo)}")
    print(f"  = CLAUDE.md: {upsert_block(written['claude'], template('CLAUDE.md'))}")
    print(f"  = AGENTS.md: {upsert_block(written['agents'], template('AGENTS.md'))}")
    print(f"  = .claude/settings.json: {merge_settings(written['settings'])}")
    print(f"  = .mcp.json: {merge_mcp_json(written['mcp'])}")
    print(
        "  = .agents/skills/worker-protocol/SKILL.md: "
        + write_text_file(
            written["worker_skill"],
            template("agents/skills/worker-protocol/SKILL.md"),
        )
    )

    # 2. Commit those files (only those) so every worktree gets them.
    commit_state = "skipped (--no-commit)"
    if not args.no_commit:
        try:
            commit_state = commit_bootstrap(
                repo,
                [
                    written["gitignore"],
                    written["claude"],
                    written["agents"],
                    written["settings"],
                    written["mcp"],
                    written["worker_skill"],
                ],
            )
        except RuntimeError as exc:
            commit_state = f"FAILED: {exc}"
            print(f"  ! bootstrap commit failed: {exc}")
            print("    set `git config user.name` / `user.email` and re-run;")
            print("    the worktrees are created but miss the bootstrap files (copied below).")
    print(f"  = bootstrap commit: {commit_state}")

    # 3. Worktrees: `wt-<project>-<name>` so two projects never collide, and
    #    re-running `coord init` re-syncs the bootstrap in *existing* worktrees.
    report = repo.name
    for name in names:
        branch = f"{args.branch_prefix}{name}"
        path = root / f"wt-{report}-{name}"
        if path.exists():
            print(f"  = {path.name} already exists, keeping its worktree/branch")
        else:
            has_branch = False
            try:
                run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
                has_branch = True
            except RuntimeError:
                has_branch = False
            cmd = ["git", "worktree", "add", "-q", str(path)]
            cmd += [branch] if has_branch else ["-b", branch]
            try:
                run(cmd, cwd=repo)
                print(f"  + {path.name} ({branch})")
            except RuntimeError as exc:
                print(f"  ! {path.name}: {exc}")
                continue

        changed = sync_bootstrap(repo, path)
        if changed:
            print(f"    (synced into {path.name}: {', '.join(changed)})")

    print(
        "\nNext:\n"
        "  1. coord up               # Worker Manager (once per machine)\n"
        f"  2. open {len(names)} Claude Code session(s) in "
        + ", ".join(f"{root / f'wt-{report}-{n}'}" for n in names)
        + "\n  3. in each session: /rename <name>, then send the bootstrap prompt\n"
        "     (orchestrator skill, section A)"
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    manager = build_manager(model=args.model, backend=args.backend)
    app = build_app(manager, host=args.host, port=args.port)
    print(
        f"coord {__version__} (protocol v{PROTOCOL_VERSION})\n"
        f"  mcp:      http://{args.host}:{args.port}/mcp\n"
        f"  backend:  {manager.backend}\n"
        f"  model:    {manager.model}\n"
        f"  thinking: {thinking()}\n"
        f"  state:    {coord_home() / 'coord.db'}\n"
        f"  api key:  {'set' if api_key() else 'MISSING WORKER_API_KEY'}"
    )
    try:
        app.run(transport="streamable-http")
    except KeyboardInterrupt:
        pass
    return 0


def cmd_msg(args: argparse.Namespace) -> int:
    from .ipc import load_records, send_session_message

    if args.list:
        records = sorted(load_records(), key=lambda x: x.get("pid", 0))
        if not records:
            print("(no Claude Code session found)")
            return 0
        print("%-7s %-32s %-10s %s" % ("PID", "NAME", "STATUS", "SOCKET"))
        for r in records:
            print(
                "%-7s %-32s %-10s %s"
                % (
                    r.get("pid", "?"),
                    r.get("name", "?"),
                    r.get("status", "?"),
                    r.get("messagingSocketPath", "-"),
                )
            )
        return 0

    if not args.target or not args.text:
        print("error: target and text are required (or use --list)", file=sys.stderr)
        return 2

    ok, detail = send_session_message(args.target, args.text)
    if ok:
        print(f"sent to {args.target}")
        return 0
    print(f"delivery failed: {detail}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    params = {k: v for k, v in (("owner", args.owner), ("project", args.project)) if v}
    if args.all:
        params["include_all"] = "true"
    try:
        rows = httpx.get(
            f"{server_url(args.host, args.port)}/api/status", params=params, timeout=10
        ).json()
    except Exception as exc:  # noqa: BLE001
        print(f"cannot reach the Worker Manager at {server_url(args.host, args.port)}: {exc}")
        print("run `coord serve` first.")
        return 1

    if not rows:
        print("(no workers)")
        return 0
    header = ("worker", "owner", "state", "task", "backend", "steps", "last", "asks")
    table = [
        (
            r["worker_id"],
            r["owner"],
            r["state"],
            r["task_id"] or "-",
            r.get("backend") or "-",
            str(r.get("steps") or 0),
            (r.get("last_tool") or "-")[:16],
            ",".join(r["pending_questions"]) or "-",
        )
        for r in rows
    ]
    widths = [max(len(str(row[i])) for row in (table + [header])) for i in range(len(header))]
    for row in [header, *table]:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    rows: list[tuple[str, str, str]] = []

    def check(label: str, ok: bool, detail: str, warn: bool = False) -> None:
        rows.append((label, "OK" if ok else ("WARN" if warn else "FAIL"), detail))

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    check("git", shutil.which("git") is not None, shutil.which("git") or "not found")
    check("uv", shutil.which("uv") is not None, shutil.which("uv") or "not found", warn=True)

    backend = default_backend()
    check("backend", True, backend)
    for name, available, detail in backend_status():
        check(f"sdk:{name}", available, detail, warn=name != backend)
    check("api key", bool(api_key()), "set" if api_key() else "set WORKER_API_KEY in .env")
    check("base url", True, base_url() or "backend default")
    check("model", True, default_model(backend))
    check("thinking", True, thinking())
    check("quota wait", True, f"{quota_wait_seconds():.0f}s")
    check("coord home", True, str(coord_home()))
    cfg = getattr(args, "loaded_config", None)
    if cfg and Path(cfg).is_file():
        check("config file", True, str(Path(cfg).resolve()))
    else:
        check("config file", False, "not configured (use --config or create .env)", warn=True)

    try:
        health = httpx.get(f"{server_url(args.host, args.port)}/health", timeout=5)
        check("server", health.status_code == 200, f"{server_url(args.host, args.port)}")
    except Exception as exc:  # noqa: BLE001
        check("server", False, f"`coord serve` not running ({type(exc).__name__})", warn=True)

    try:
        repo = git_repo_root(Path(args.repo).resolve())
        check("repo", True, str(repo))
        check("ledger", (repo / ".coord").is_dir(), str(repo / ".coord"), warn=True)
        worktrees = run(["git", "worktree", "list", "--porcelain"], cwd=repo)
        count = sum(1 for line in worktrees.splitlines() if line.startswith("worktree "))
        check("worktrees", count > 1, f"{count} worktree", warn=True)
    except Exception as exc:  # noqa: BLE001
        check("repo", False, str(exc))

    check("templates", (TEMPLATES / "CLAUDE.md").is_file(), str(TEMPLATES))

    width = max(len(r[0]) for r in rows)
    for label, verdict, detail in rows:
        print(f"{label.ljust(width)}  {verdict}  {detail}")
    return 1 if any(r[1] == "FAIL" for r in rows) else 0


def _health(host: str, port: int) -> bool:
    try:
        return httpx.get(f"http://{host}:{port}/health", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def cmd_up(args: argparse.Namespace) -> int:
    """Idempotent: start `coord serve` in the background if needed, then wait for /health."""
    url = server_url(args.host, args.port)
    if _health(args.host, args.port):
        print(f"coord is already running: {url}")
        print(f"  mcp:    {url}/mcp")
        return 0

    home = coord_home()
    home.mkdir(parents=True, exist_ok=True)
    log_path = home / "coord.log"
    pid_path = home / "coord.pid"

    cmd = [
        sys.executable,
        "-m",
        "coord.cli",
        "serve",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.backend:
        cmd += ["--backend", args.backend]
    if args.model:
        cmd += ["--model", args.model]
    if getattr(args, "config", None):
        cmd += ["--config", str(Path(args.config).resolve())]

    child_env = os.environ.copy()
    # Windows consoles default to cp1252; force UTF-8 so the log never raises UnicodeEncodeError.
    child_env["PYTHONUTF8"] = "1"
    with log_path.open("ab") as log:
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, env=child_env, **kwargs
        )
    pid_path.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _health(args.host, args.port):
            print(f"coord is up: {url}  (pid {proc.pid})")
            print(f"  mcp:    {url}/mcp")
            print(f"  log:    {log_path}")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    print(f"coord up failed (see {log_path})")
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
        print(f"  {line}")
    return 1


def cmd_down(args: argparse.Namespace) -> int:
    """Stop the process started by `coord up`."""
    pid_path = coord_home() / "coord.pid"
    if not pid_path.is_file():
        print("no PID file (was `coord up` ever run?)")
        return 1
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        pid_path.unlink(missing_ok=True)
        print("invalid PID; removed the file")
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        print("process is no longer running")
        return 0

    for _ in range(20):
        if not _health(args.host, args.port):
            break
        time.sleep(0.25)
    else:
        # Windows has no signal.SIGKILL; SIGTERM is the strongest signal available there.
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))

    pid_path.unlink(missing_ok=True)
    print("coordinator stopped")
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "--config",
        "-c",
        default=argparse.SUPPRESS,
        help="path to the config file (.env)",
    )

    parser = argparse.ArgumentParser(
        prog="coord",
        description=f"Worker Manager + peer-group orchestration kit (protocol v{PROTOCOL_VERSION}).",
        parents=[config_parent],
    )
    parser.add_argument("--version", action="version", version=f"coord {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve", parents=[config_parent], help="run the Worker Manager (MCP streamable-http)"
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--model", default=None)
    serve.add_argument(
        "--backend",
        default=None,
        choices=["agy-sdk", "claude-sdk"],
        help="worker backend (default: $WORKER_BACKEND or agy-sdk)",
    )

    up = sub.add_parser(
        "up",
        parents=[config_parent],
        help="start the Worker Manager in the background if needed, then check /health",
    )
    up.add_argument("--host", default=DEFAULT_HOST)
    up.add_argument("--port", type=int, default=DEFAULT_PORT)
    up.add_argument("--backend", default=None, choices=["agy-sdk", "claude-sdk"])
    up.add_argument("--model", default=None)

    down = sub.add_parser(
        "down",
        parents=[config_parent],
        help="stop the Worker Manager started by `coord up`",
    )
    down.add_argument("--host", default=DEFAULT_HOST)
    down.add_argument("--port", type=int, default=DEFAULT_PORT)

    msg = sub.add_parser(
        "msg",
        parents=[config_parent],
        help="send a message straight into a Claude Code session over its IPC socket",
    )
    msg.add_argument("target", nargs="?", help="session name, session id, PID, or socket path")
    msg.add_argument("text", nargs="?", help="message body")
    msg.add_argument(
        "-l", "--list", action="store_true", help="list the running Claude Code sessions"
    )

    init = sub.add_parser(
        "init",
        parents=[config_parent],
        help="create worktrees + .coord/ + the bootstrap templates",
    )
    init.add_argument("names", nargs="*", help="orchestrator names (default: A B C)")
    init.add_argument("--repo", default=".", help="target repo (default: current directory)")
    init.add_argument(
        "--worktree-root",
        default="..",
        help="directory that holds wt-<project>-<name> (default: ..)",
    )
    init.add_argument("--branch-prefix", default="stream-")
    init.add_argument(
        "--no-commit",
        action="store_true",
        help="write the files but skip the bootstrap commit (worktrees get a plain copy)",
    )

    status = sub.add_parser(
        "status", parents=[config_parent], help="list workers from the running server"
    )
    status.add_argument("--owner", default=None)
    status.add_argument("--project", default=None)
    status.add_argument(
        "--all", action="store_true", help="also show old FAILED/KILLED workers"
    )
    status.add_argument("--host", default=DEFAULT_HOST)
    status.add_argument("--port", type=int, default=DEFAULT_PORT)

    doctor = sub.add_parser(
        "doctor", parents=[config_parent], help="check the environment and the server"
    )
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--host", default=DEFAULT_HOST)
    doctor.add_argument("--port", type=int, default=DEFAULT_PORT)

    return parser


COMMANDS = {
    "init": cmd_init,
    "serve": cmd_serve,
    "up": cmd_up,
    "down": cmd_down,
    "msg": cmd_msg,
    "status": cmd_status,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; force UTF-8 so output never crashes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    from .config import load_env_file

    args.loaded_config = load_env_file(getattr(args, "config", None))
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
