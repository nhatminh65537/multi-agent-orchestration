#!/usr/bin/env python3
"""V-9: prove the policy hook blocks the four forbidden classes (no API key needed).

    uv run python verify/v9_policy.py

Exit code 0 = every case behaved as required.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.policy import check_tool_policy  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wt = root / "wt-A"
        wt.mkdir()
        outside = root / "wt-B" / "src" / "api.py"

        cases: list[tuple[str, str, dict, tuple[str, ...], bool]] = [
            # (label, tool, input, needs_approval, expected_allowed)
            ("write inside worktree", "Write", {"file_path": str(wt / "a.py")}, (), True),
            ("write outside (absolute)", "Write", {"file_path": str(outside)}, (), False),
            ("write outside (traversal)", "Edit", {"file_path": "../wt-B/src/api.py"}, (), False),
            ("create_file outside (agy)", "create_file", {"path": str(outside)}, (), False),
            ("git commit", "Bash", {"command": "git commit -am wip"}, (), False),
            ("git merge", "Bash", {"command": "git merge stream-B"}, (), False),
            ("git push", "Bash", {"command": "git push origin main"}, (), False),
            ("git reset --hard", "run_command", {"CommandLine": "git reset --hard"}, (), False),
            ("git status (allowed)", "Bash", {"command": "git status --short"}, (), True),
            ("needs_approval", "Bash", {"command": "pip install requests"}, ("pip install",), False),
            ("coord MCP recursion", "mcp__coord__spawn_worker", {"owner": "A"}, (), False),
            ("worker-manager recursion", "mcp__worker-manager__spawn_worker", {}, (), False),
            ("subagent", "Task", {"prompt": "go"}, (), False),
            ("subagent (agy)", "invoke_subagent", {"name": "x"}, (), False),
        ]

        failures = 0
        width = max(len(label) for label, *_ in cases)
        for label, tool, payload, approvals, expected in cases:
            allowed, reason = check_tool_policy(tool, payload, wt, approvals)
            verdict = "PASS" if allowed == expected else "FAIL"
            if verdict == "FAIL":
                failures += 1
            detail = "allowed" if allowed else f"denied: {reason}"
            print(f"{label.ljust(width)}  {verdict}  {detail}")

        print()
        print("V-9:", "PASS" if failures == 0 else f"FAIL ({failures} case)")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
