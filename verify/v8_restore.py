#!/usr/bin/env python3
"""V-8: a restart keeps `IDLE` workers reusable and cleans up the dangling state.

    uv run python verify/v8_restore.py

No API key, no network: the restore path is exercised through a stub backend.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.backend import WorkerSession  # noqa: E402
from coord.manager import Manager  # noqa: E402
from coord.store import Store  # noqa: E402


class _Stub(WorkerSession):
    """Only used so `_session_for` can rebuild a session; it never runs a turn."""

    backend_name = "stub"

    async def _run_turn(self, text: str, model: str) -> str:  # pragma: no cover - unused
        raise AssertionError("stub session does not run")


def stub_factory(
    backend: str,
    *,
    worker_id: str,
    owner: str,
    worktree: Path,
    model: str,
    packet: Any,
    ask: Callable[[str], Awaitable[str]],
    notify: Callable[[str], None] | None = None,
    session_id: str | None = None,
) -> WorkerSession:
    return _Stub(
        worker_id=worker_id,
        owner=owner,
        worktree=worktree,
        model=model,
        packet=packet,
        ask=ask,
        notify=notify,
        session_id=session_id,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wt = root / "wt-A"
        wt.mkdir()
        store = Store(root / "coord.db")

        store.add_worker(
            "A-idle", "A", str(root), str(wt), "A-01", backend="agy-sdk", session_id="sess-1"
        )
        store.set_state("A-idle", "IDLE")
        store.add_worker("A-run", "A", str(root), str(wt), "A-02", backend="agy-sdk")
        store.add_worker("A-wait", "A", str(root), str(wt), "A-03", backend="agy-sdk")
        store.set_state("A-wait", "WAITING_ANSWER")
        store.add_question("q1", "A-wait", "A-03", "JWT or cookie?")

        Manager(store, backend="agy-sdk", session_factory=stub_factory, restore=False)
        # the process "dies" here: no session left in RAM

        second = Manager(store, backend="agy-sdk", session_factory=stub_factory, restore=True)

        checks = {
            "IDLE stays IDLE": store.worker("A-idle")["state"] == "IDLE",
            "RUNNING -> FAILED": store.worker("A-run")["state"] == "FAILED",
            "WAITING_ANSWER -> FAILED": store.worker("A-wait")["state"] == "FAILED",
            "dangling question -> SERVER_RESTARTED": store.question("q1")["answer"]
            == "SERVER_RESTARTED",
            "status has no pending questions": all(
                not row["pending_questions"] for row in second.status(owner="A")
            ),
        }

        session = second._session_for("A-idle")  # noqa: SLF001 - exactly the behaviour under test
        checks["IDLE worker rebuilds its session"] = session.sdk_session_id == "sess-1"
        checks["session is bound to the worker"] = session.worker_id == "A-idle"

        width = max(len(label) for label in checks)
        for label, ok in checks.items():
            print(f"{label.ljust(width)}  {'PASS' if ok else 'FAIL'}")

        failed = [label for label, ok in checks.items() if not ok]
        print()
        print("V-8:", "PASS" if not failed else f"FAIL ({', '.join(failed)})")
        store.close()
        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
