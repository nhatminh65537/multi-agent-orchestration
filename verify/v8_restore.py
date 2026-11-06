#!/usr/bin/env python3
"""V-8: restart giữ `IDLE` worker dùng lại được và dọn sạch phần còn treo.

    uv run python verify/v8_restore.py

Không cần API key, không cần network: kiểm tra đường khôi phục bằng backend giả.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.backend import WorkerSession  # noqa: E402
from coord.manager import Manager  # noqa: E402
from coord.models import WorkerReport  # noqa: E402
from coord.store import Store  # noqa: E402


class _Stub(WorkerSession):
    """Chỉ để `_session_for` dựng lại session; không chạy lượt nào."""

    backend_name = "stub"

    async def run(self, text: str) -> None:  # pragma: no cover - không dùng
        raise AssertionError("stub session không chạy")


def stub_factory(
    backend: str,
    *,
    worker_id: str,
    owner: str,
    worktree: Path,
    model: str,
    packet: Any,
    ask: Callable[[str, str], Awaitable[str]],
    on_report: Callable[[WorkerReport], Awaitable[None]],
    session_id: str | None = None,
) -> WorkerSession:
    return _Stub(
        worker_id=worker_id,
        owner=owner,
        worktree=worktree,
        model=model,
        packet=packet,
        ask=ask,
        on_report=on_report,
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
        store.add_question("q1", "A-wait", "A-03", "JWT hay cookie?")

        Manager(store, backend="agy-sdk", session_factory=stub_factory, restore=False)
        # tiến trình "chết" ở đây: không còn session nào trong RAM

        second = Manager(store, backend="agy-sdk", session_factory=stub_factory, restore=True)

        checks = {
            "IDLE giữ nguyên": store.worker("A-idle")["state"] == "IDLE",
            "RUNNING -> FAILED": store.worker("A-run")["state"] == "FAILED",
            "WAITING_ANSWER -> FAILED": store.worker("A-wait")["state"] == "FAILED",
            "câu hỏi treo -> SERVER_RESTARTED": store.question("q1")["answer"]
            == "SERVER_RESTARTED",
            "status không còn pending": all(
                not row["pending_questions"] for row in second.status(owner="A")
            ),
        }

        session = second._session_for("A-idle")  # noqa: SLF001 - chính là hành vi cần kiểm
        checks["IDLE dựng lại được session"] = session.sdk_session_id == "sess-1"
        checks["session gắn đúng worker"] = session.worker_id == "A-idle"

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
