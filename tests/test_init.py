"""`coord init` must be an *update* on re-run, not an append or a skip.

Covers the three v0.5 bugs:
* `.agents/skills/worker-protocol/SKILL.md` was never refreshed;
* existing worktrees were skipped entirely (they kept stale rules/config);
* `.mcp.json` used `setdefault`, so a stale `coord` URL survived.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.cli import cmd_init  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


class InitUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "proj"
        self.repo.mkdir()
        self.wt_root = self.root / "wt"

        self.env = mock.patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        self.env.start()

        git(self.repo, "init", "-q")
        (self.repo / "README.md").write_text("# proj\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "initial")

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _init(self) -> str:
        args = argparse.Namespace(
            repo=str(self.repo),
            names=["A"],
            worktree_root=str(self.wt_root),
            branch_prefix="stream-",
            no_commit=False,
            config=None,
            loaded_config=None,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_init(args), 0)
        return out.getvalue()

    def test_worktree_name_contains_project(self) -> None:
        self._init()
        self.assertTrue((self.wt_root / "wt-proj-A").is_dir())
        self.assertFalse((self.wt_root / "wt-A").exists())

    def test_rerun_refreshes_every_bootstrap_file(self) -> None:
        self._init()
        worktree = self.wt_root / "wt-proj-A"

        # Simulate a project that was initialised by an older coord version.
        stale = "# stale rules\n"
        for base in (self.repo, worktree):
            (base / "CLAUDE.md").write_text(stale, encoding="utf-8")
            (base / "AGENTS.md").write_text(stale, encoding="utf-8")
            skill = base / ".agents" / "skills" / "worker-protocol" / "SKILL.md"
            skill.write_text(stale, encoding="utf-8")
        (self.repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"coord": {"type": "http", "url": "http://old/mcp"}}}),
            encoding="utf-8",
        )
        (worktree / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"coord": {"type": "http", "url": "http://old/mcp"}}}),
            encoding="utf-8",
        )

        self._init()

        for base in (self.repo, worktree):
            claude = (base / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("coord:begin", claude)
            self.assertIn("subagents", claude)
            self.assertNotEqual(claude, stale)

            skill = (base / ".agents" / "skills" / "worker-protocol" / "SKILL.md")
            self.assertNotEqual(skill.read_text(encoding="utf-8"), stale)
            self.assertIn("FAILED:", skill.read_text(encoding="utf-8"))

            mcp = json.loads((base / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(mcp["mcpServers"]["coord"]["url"], "http://127.0.0.1:30303/mcp")

    def test_rerun_never_duplicates_the_marked_block(self) -> None:
        self._init()
        self._init()
        text = (self.repo / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- coord:begin -->"), 1)
        self.assertEqual(text.count("<!-- coord:end -->"), 1)

    def test_settings_deny_builtin_subagents(self) -> None:
        self._init()
        settings = json.loads((self.repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Agent", settings["permissions"]["deny"])
        self.assertIn("Task", settings["permissions"]["deny"])
        self.assertIn("mcp__coord__spawn_worker", settings["permissions"]["allow"])


if __name__ == "__main__":
    unittest.main()
