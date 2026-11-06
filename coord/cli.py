"""`coord` command line: serve | watch | init | doctor | status."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import PROTOCOL_VERSION, __version__
from .backend import TEMPLATES
from .manager import DEFAULT_HOST, DEFAULT_PORT, build_app, build_manager, coord_home

BEGIN = "<!-- coord:begin -->"
END = "<!-- coord:end -->"
MCP_URL = "http://127.0.0.1:8765/mcp"

ALLOWED_TOOLS = [
    "mcp__coord__spawn_worker",
    "mcp__coord__fork_worker",
    "mcp__coord__send",
    "mcp__coord__answer",
    "mcp__coord__status",
    "Bash(coord:*)",
    "Bash(git diff:*)",
    "Bash(git worktree:*)",
    "Bash(git merge stream-*)",
]


# ------------------------------------------------------------------- helpers
def run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"{cmd[0]} thất bại")
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
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        stop = text.index(END) + len(END)
        updated = text[:start] + block + text[stop:]
        if updated == text:
            return "unchanged"
        path.write_text(updated, encoding="utf-8")
        return "updated"

    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n" + block + "\n", encoding="utf-8")
    return "appended"


def merge_settings(path: Path) -> str:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return f"bỏ qua (JSON lỗi: {exc})"

    before = json.dumps(data, sort_keys=True)
    data.setdefault("crossSessionInbound", "accept")
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    for item in ALLOWED_TOOLS:
        if item not in allow:
            allow.append(item)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "unchanged" if before == json.dumps(data, sort_keys=True) else "updated"


def merge_mcp_json(path: Path) -> str:
    """Register the coord server project-locally (works without the plugin)."""
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return f"bỏ qua (JSON lỗi: {exc})"

    before = json.dumps(data, sort_keys=True)
    servers = data.setdefault("mcpServers", {})
    servers.setdefault("coord", {"type": "http", "url": MCP_URL})
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
    }
    print(f"  = .gitignore: {ensure_gitignore(repo)}")
    print(f"  = CLAUDE.md: {upsert_block(written['claude'], template('CLAUDE.md'))}")
    print(f"  = AGENTS.md: {upsert_block(written['agents'], template('AGENTS.md'))}")
    print(f"  = .claude/settings.json: {merge_settings(written['settings'])}")
    print(f"  = .mcp.json: {merge_mcp_json(written['mcp'])}")

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
                ],
            )
        except RuntimeError as exc:
            commit_state = f"FAILED: {exc}"
            print(f"  ! bootstrap commit thất bại: {exc}")
            print("    đặt `git config user.name` / `user.email` rồi chạy lại;")
            print("    worktree sẽ được tạo nhưng thiếu file bootstrap (sẽ copy bù).")
    print(f"  = bootstrap commit: {commit_state}")

    # 3. Worktrees, now based on the commit above.
    for name in names:
        branch = f"{args.branch_prefix}{name}"
        path = root / f"wt-{name}"
        if path.exists():
            print(f"  = wt-{name} đã tồn tại, bỏ qua")
            continue
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
            print(f"  + wt-{name} ({branch})")
        except RuntimeError as exc:
            print(f"  ! wt-{name}: {exc}")
            continue

        missing = [
            rel
            for rel in ("CLAUDE.md", "AGENTS.md", ".claude/settings.json", ".mcp.json")
            if not (path / rel).exists()
        ]
        if missing:
            for rel in missing:
                src = repo / rel
                if not src.exists():
                    continue
                (path / rel).parent.mkdir(parents=True, exist_ok=True)
                (path / rel).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"    (copy bù vào worktree: {', '.join(missing)})")

    print(
        "\nTiếp theo:\n"
        "  1. coord serve            # Worker Manager (một lần cho cả máy)\n"
        f"  2. mở {len(names)} session Claude Code tại "
        + ", ".join(f"{root / f'wt-{n}'}" for n in names)
        + "\n  3. trong mỗi session: /rename <tên>, rồi gửi bootstrap prompt\n"
        "     (skill orchestrator, mục A)"
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    manager = build_manager(model=args.model)
    app = build_app(manager, host=args.host, port=args.port)
    print(
        f"coord {__version__} (protocol v{PROTOCOL_VERSION})\n"
        f"  mcp:      http://{args.host}:{args.port}/mcp\n"
        f"  events:   http://{args.host}:{args.port}/events/<owner>\n"
        f"  model:    {manager.model}\n"
        f"  state:    {coord_home() / 'coord.db'}\n"
        f"  backend:  {'có API key' if os.environ.get('COORD_API_KEY') or os.environ.get('OPENAI_API_KEY') else 'CHƯA có COORD_API_KEY'}"
    )
    try:
        app.run(transport="streamable-http")
    except KeyboardInterrupt:
        pass
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    url = f"{server_url(args.host, args.port)}/events/{args.to}"
    print(f"# watch {args.to} → {url} (Ctrl-C để dừng)", flush=True)
    while True:
        try:
            with httpx.stream("GET", url, timeout=None) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        print(line, flush=True)
        except KeyboardInterrupt:
            return 0
        except Exception:  # noqa: BLE001 - manager restarting / not up yet
            time.sleep(3)


def cmd_status(args: argparse.Namespace) -> int:
    params = {k: v for k, v in (("owner", args.owner), ("project", args.project)) if v}
    try:
        rows = httpx.get(
            f"{server_url(args.host, args.port)}/api/status", params=params, timeout=10
        ).json()
    except Exception as exc:  # noqa: BLE001
        print(f"không gọi được Worker Manager tại {server_url(args.host, args.port)}: {exc}")
        print("chạy `coord serve` trước.")
        return 1

    if not rows:
        print("(không có worker)")
        return 0
    header = ("worker", "owner", "state", "task", "parent", "asks")
    table = [
        (
            r["worker_id"],
            r["owner"],
            r["state"],
            r["task_id"] or "-",
            r["parent"] or "-",
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
    check("git", shutil.which("git") is not None, shutil.which("git") or "không thấy")
    check("uv", shutil.which("uv") is not None, shutil.which("uv") or "không thấy", warn=True)

    key = os.environ.get("COORD_API_KEY") or os.environ.get("OPENAI_API_KEY")
    check("api key", bool(key), "đã đặt" if key else "đặt COORD_API_KEY (hoặc OPENAI_API_KEY)")
    check(
        "base url",
        True,
        os.environ.get("COORD_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "mặc định (Gemini OpenAI-compatible)",
    )
    check("model", True, os.environ.get("COORD_MODEL") or "gemini-3.8-flash")
    check("coord home", True, str(coord_home()))

    try:
        health = httpx.get(f"{server_url(args.host, args.port)}/health", timeout=5)
        check("server", health.status_code == 200, f"{server_url(args.host, args.port)}")
    except Exception as exc:  # noqa: BLE001
        check("server", False, f"chưa chạy `coord serve` ({type(exc).__name__})", warn=True)

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


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coord",
        description="Worker Manager + peer-group orchestration kit (protocol v0.3).",
    )
    parser.add_argument("--version", action="version", version=f"coord {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="chạy Worker Manager (MCP streamable-http)")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--model", default=None)

    watch = sub.add_parser("watch", help="in event của một owner ra stdout (nguồn cho Monitor)")
    watch.add_argument("--to", required=True, help="tên orchestrator, ví dụ A")
    watch.add_argument("--host", default=DEFAULT_HOST)
    watch.add_argument("--port", type=int, default=DEFAULT_PORT)

    init = sub.add_parser("init", help="tạo worktree + .coord/ + template trong repo")
    init.add_argument("names", nargs="*", help="tên orchestrator (mặc định: A B C)")
    init.add_argument("--repo", default=".", help="repo đích (mặc định: thư mục hiện tại)")
    init.add_argument("--worktree-root", default="..", help="thư mục chứa wt-<name> (mặc định: ..)")
    init.add_argument("--branch-prefix", default="stream-")
    init.add_argument(
        "--no-commit",
        action="store_true",
        help="chỉ ghi file, không commit bootstrap (worktree sẽ thiếu file, có copy bù)",
    )

    status = sub.add_parser("status", help="liệt kê worker từ server đang chạy")
    status.add_argument("--owner", default=None)
    status.add_argument("--project", default=None)
    status.add_argument("--host", default=DEFAULT_HOST)
    status.add_argument("--port", type=int, default=DEFAULT_PORT)

    doctor = sub.add_parser("doctor", help="kiểm tra môi trường và server")
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--host", default=DEFAULT_HOST)
    doctor.add_argument("--port", type=int, default=DEFAULT_PORT)

    return parser


COMMANDS = {
    "init": cmd_init,
    "serve": cmd_serve,
    "watch": cmd_watch,
    "status": cmd_status,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
