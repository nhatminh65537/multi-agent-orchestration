#!/usr/bin/env python3
"""V-8: prove a restart keeps `IDLE` workers resumable and cleans up the rest.

    uv run python verify/v8_restore.py

No API key, no network: the check inspects the restore path itself.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.manager import Manager  # noqa: E402
from coord.store import Store  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wt = root / "wt-A"
        wt.mkdir()
        db = root / "coord.db"

        store = Store(db)
        store.add_worker(
            "A-idle", "A", str(root), str(wt), "A-01", backend="openai-loop", session_id="sess-1"
        )
        store.set_state("A-idle", "IDLE")
        store.add_worker("A-run", "A", str(root), str(wt), "A-02", backend="openai-loop")
        store.add_worker("A-wait", "A", str(root), str(wt), "A-03", backend="openai-loop")
        store.set_state("A-wait", "WAITING_ANSWER")
        store.add_question("q1", "A-wait", "A-03", "JWT hay cookie?")

        first = Manager(store, backend="openai-loop", restore=False)
        del first  # simulate the process dying

        second = Manager(store, backend="openai-loop", restore=True)

        checks = {
            "IDLE giữ nguyên": store.worker("A-idle")["state"] == "IDLE",
            "RUNNING -> FAILED": store.worker("A-run")["state"] == "FAILED",
            "WAITING_ANSWER -> FAILED": store.worker("A-wait")["state"] == "FAILED",
            "câu hỏi treo -> SERVER_RESTARTED": store.question("q1")["answer"] == "SERVER_RESTARTED",
            "status không còn pending": all(
                not row["pending_questions"] for row in second.status(owner="A")
            ),
        }

        session = second._session_for("A-idle")
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
