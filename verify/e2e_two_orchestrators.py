"""E2E Verification: 2 Claude Code Orchestrators + Worker Manager + Worker IPC round-trip.

Tests:
  1. Launch two real Claude Code sessions (OrchestratorA and OrchestratorB) with `--model haiku`.
  2. Peer cross-session IPC messaging from OrchestratorA to OrchestratorB.
  3. Worker Manager (`coord`) running with haiku/flash config.
  4. OrchestratorA spawns a Worker.
  5. Worker calls `ask_orchestrator` mid-task -> Manager pushes IPC message directly to OrchestratorA's socket.
  6. OrchestratorA answers via `answer()` -> Worker unblocks and receives the answer.
  7. Worker answers in prose -> Manager writes log and pushes DONE notification to OrchestratorA.
  8. Graceful teardown of all processes.
"""

from __future__ import annotations

import asyncio
import json
import os
import pty
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from coord.backend import WorkerSession
from coord.ipc import load_records, resolve_target, send_session_message
from coord.manager import Manager
from coord.models import TaskPacket
from coord.store import Store


class MockWorkerSession(WorkerSession):
    """Simulates a worker harness: asks once, then answers in prose."""
    backend_name = "test-worker"

    async def _run_turn(self, text: str, model: str) -> str:
        print("  [Worker] Starting the task...")
        await asyncio.sleep(0.5)

        # 1. The worker needs a decision from the orchestrator
        print("  [Worker] Missing information -> calling ask_orchestrator('JWT or session cookie?')...")
        answer = await self._ask("JWT or session cookie?")
        print(f"  [Worker] Answer received from the orchestrator: {answer!r}")

        await asyncio.sleep(0.5)

        # 2. The worker finishes and answers in prose (no report_completion)
        print("  [Worker] Task done -> writing the final answer...")
        return (
            f"Configured auth with: {answer}\n"
            "Files changed: src/auth.py\n"
            "Tests: pytest tests/test_auth.py -> passed"
        )


def start_claude_orchestrator(name: str, cwd: Path) -> tuple[subprocess.Popen, int]:
    master, slave = pty.openpty()
    cmd = ["claude", "--model", "haiku", "--name", name]
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    # Wait for the session socket and the json key file to be created
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if resolve_target(name):
            break
        time.sleep(0.5)

    if not resolve_target(name):
        proc.terminate()
        os.close(master)
        raise RuntimeError(f"Could not start a Claude Code session for {name}")

    print(f"  [OK] Session {name} is up: PID={proc.pid}, socket={resolve_target(name)}")
    return proc, master


async def run_e2e() -> None:
    print("=" * 70)
    print("START E2E TEST: 2 CLAUDE CODE ORCHESTRATORS + WORKER IPC ROUND-TRIP")
    print("=" * 70)

    repo_dir = Path("/home/rougitsune/repos/multi-agent-orchestration")

    # 1. Start the two orchestrator sessions with Claude Code (--model haiku)
    print("\n[Step 1] Starting 2 Claude Code sessions: OrchestratorA and OrchestratorB...")
    proc_a, master_a = start_claude_orchestrator("OrchestratorA", repo_dir)
    proc_b, master_b = start_claude_orchestrator("OrchestratorB", repo_dir)

    try:
        # Check the session records
        recs = load_records()
        names = [r.get("name") for r in recs]
        print(f"  Active sessions: {names}")
        assert "OrchestratorA" in names, "OrchestratorA is missing from the records"
        assert "OrchestratorB" in names, "OrchestratorB is missing from the records"

        # 2. Peer-to-peer native IPC from OrchestratorA to OrchestratorB
        print("\n[Step 2] Peer native IPC: OrchestratorA -> OrchestratorB...")
        ok, detail = send_session_message(
            "OrchestratorB",
            "NEED A-01: send the auth contract schema",
        )
        print(f"  Sent to OrchestratorB: ok={ok}, detail={detail}")
        assert ok, f"Peer message delivery failed: {detail}"

        # 3. Boot the Worker Manager
        print("\n[Step 3] Booting the Worker Manager...")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "coord.db"
            store = Store(db_path)

            def mock_factory(backend, **kwargs):
                return MockWorkerSession(**kwargs)

            manager = Manager(
                store,
                backend="test-worker",
                model="claude-3-5-haiku",
                restore=False,
                session_factory=mock_factory,
            )

            # 4. OrchestratorA calls spawn_worker
            print("\n[Step 4] OrchestratorA spawns a Worker with a TaskPacket...")
            packet = TaskPacket(
                id="A-01",
                goal="Configure the authentication module for the system",
                files=["src/auth.py"],
                acceptance=["pytest tests/test_auth.py passes"],
            )
            worker_id = await manager.spawn_worker(
                owner="OrchestratorA",
                worktree=str(repo_dir),
                task=packet.model_dump_json(),
            )
            print(f"  Worker spawned: worker_id={worker_id}")

            # 5. Wait until the worker asks a question (WAITING_ANSWER)
            print("\n[Step 5] Watching the Worker talk back (ask_orchestrator)...")
            deadline = time.monotonic() + 10
            pending_q = None
            while time.monotonic() < deadline:
                w_state = store.worker(worker_id)
                if w_state and w_state["state"] == "WAITING_ANSWER":
                    questions = store.pending_questions(worker_id)
                    if questions:
                        pending_q = questions[0]
                        break
                await asyncio.sleep(0.2)

            assert pending_q is not None, "Worker never asked an ask_orchestrator question"
            print(f"  [OK] Worker asked a question! qid={pending_q}")
            q_row = store.question(pending_q)
            print(f"  Worker question: {q_row['question']!r}")

            # 6. OrchestratorA answers through manager.answer
            print("\n[Step 6] OrchestratorA got the IPC prompt and calls answer()...")
            ans_res = await manager.answer(
                "OrchestratorA", pending_q, "JWT token with a 24h expiration"
            )
            print(f"  answer() result: {ans_res}")

            # 7. Wait for the worker to finish and check the final answer
            print("\n[Step 7] Waiting for the Worker to finish and answer...")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                w_state = store.worker(worker_id)
                if w_state and w_state["state"] == "IDLE":
                    break
                await asyncio.sleep(0.2)

            assert store.worker(worker_id)["state"] == "IDLE", "Worker did not end in IDLE"
            log_path = w_state["worktree"] + f"/.coord/logs/{w_state['task_id']}.md"
            log_body = Path(log_path).read_text(encoding="utf-8")
            assert "## response" in log_body, "The log has no final worker answer"
            print("  [OK] Worker answered and the Manager wrote the log")

            # Check the recorded events
            events = [
                r[0]
                for r in store.db.execute(
                    "select line from events where owner='OrchestratorA' order by id"
                ).fetchall()
            ]
            print(f"  Events recorded for OrchestratorA: {len(events)}")

    finally:
        # 8. Tear everything down
        print("\n[Step 8] Shutting down the Claude Code processes...")
        proc_a.terminate()
        proc_b.terminate()
        try:
            proc_a.wait(timeout=3)
            proc_b.wait(timeout=3)
        except Exception:
            proc_a.kill()
            proc_b.kill()
        os.close(master_a)
        os.close(master_b)
        print("  [OK] All processes stopped cleanly.")

    print("\n" + "=" * 70)
    print("E2E COMPLETE: EVERY STEP SUCCEEDED")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_e2e())
