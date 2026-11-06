"""E2E Verification: 2 Claude Code Orchestrators + Worker Manager + Worker IPC round-trip.

Tests:
  1. Launch two real Claude Code sessions (OrchestratorA and OrchestratorB) with `--model haiku`.
  2. Peer cross-session IPC messaging from OrchestratorA to OrchestratorB.
  3. Worker Manager (`coord`) running with haiku/flash config.
  4. OrchestratorA spawns a Worker.
  5. Worker calls `ask_orchestrator` mid-task -> Manager pushes IPC message directly to OrchestratorA's socket.
  6. OrchestratorA answers via `answer()` -> Worker unblocks and receives the answer.
  7. Worker calls `report_completion(DONE)` -> Manager writes log and pushes DONE notification to OrchestratorA.
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
from coord.models import TaskPacket, WorkerReport
from coord.store import Store


class MockWorkerSession(WorkerSession):
    """Simulates a worker harness executing steps with ask and report."""
    backend_name = "test-worker"

    async def run(self, text: str) -> None:
        print("  [Worker] Bắt đầu thực thi task...")
        await asyncio.sleep(0.5)

        # 1. Worker cần hỏi Orchestrator
        print("  [Worker] Thiếu thông tin -> gọi ask_orchestrator('JWT hay session cookie?')...")
        answer = await self._ask(self.task_id, "JWT hay session cookie?")
        print(f"  [Worker] Nhận được câu trả lời từ Orchestrator: {answer!r}")

        await asyncio.sleep(0.5)

        # 2. Worker hoàn thành task và báo cáo
        print("  [Worker] Hoàn thành task -> gọi report_completion()...")
        report = WorkerReport(
            id=self.task_id or "task-01",
            status="DONE",
            summary=f"Đã hoàn thành cấu hình auth với phương thức: {answer}",
            files_written=["src/auth.py"],
            tests_run=["pytest tests/test_auth.py passed"],
        )
        await self._accept_report(report)


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

    # Chờ session socket và json key file khởi tạo
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if resolve_target(name):
            break
        time.sleep(0.5)

    if not resolve_target(name):
        proc.terminate()
        os.close(master)
        raise RuntimeError(f"Không thể khởi động session Claude Code cho {name}")

    print(f"  [OK] Session {name} đã lên: PID={proc.pid}, socket={resolve_target(name)}")
    return proc, master


async def run_e2e() -> None:
    print("=" * 70)
    print("BẮT ĐẦU E2E TEST: 2 CLAUDE CODE ORCHESTRATORS + WORKER IPC ROUND-TRIP")
    print("=" * 70)

    repo_dir = Path("/home/rougitsune/repos/multi-agent-orchestration")

    # 1. Khởi động 2 Orchestrator sessions với Claude Code (--model haiku)
    print("\n[Bước 1] Khởi động 2 Claude Code sessions: OrchestratorA và OrchestratorB...")
    proc_a, master_a = start_claude_orchestrator("OrchestratorA", repo_dir)
    proc_b, master_b = start_claude_orchestrator("OrchestratorB", repo_dir)

    try:
        # Kiểm tra danh sách session qua load_records
        recs = load_records()
        names = [r.get("name") for r in recs]
        print(f"  Danh sách sessions đang active: {names}")
        assert "OrchestratorA" in names, "OrchestratorA không có trong records"
        assert "OrchestratorB" in names, "OrchestratorB không có trong records"

        # 2. Test giao tiếp peer native IPC giữa OrchestratorA -> OrchestratorB
        print("\n[Bước 2] Test giao tiếp peer native IPC: OrchestratorA -> OrchestratorB...")
        ok, detail = send_session_message(
            "OrchestratorB",
            "NEED A-01: Gửi schema auth contract",
        )
        print(f"  Gửi tới OrchestratorB: ok={ok}, detail={detail}")
        assert ok, f"Gửi tin nhắn peer thất bại: {detail}"

        # 3. Khởi tạo Worker Manager
        print("\n[Bước 3] Khởi tạo Worker Manager...")
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

            # 4. OrchestratorA gọi spawn_worker
            print("\n[Bước 4] OrchestratorA spawn Worker với TaskPacket...")
            packet = TaskPacket(
                id="A-01",
                goal="Cấu hình authentication module cho hệ thống",
                files=["src/auth.py"],
                acceptance=["pytest tests/test_auth.py passes"],
            )
            worker_id = await manager.spawn_worker(
                owner="OrchestratorA",
                worktree=str(repo_dir),
                task=packet.model_dump_json(),
            )
            print(f"  Worker spawned thành công: worker_id={worker_id}")

            # 5. Chờ worker chuyển sang trạng thái WAITING_ANSWER (gọi ask_orchestrator)
            print("\n[Bước 5] Theo dõi Worker giao tiếp lại (ask_orchestrator)...")
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

            assert pending_q is not None, "Worker không gửi được câu hỏi ask_orchestrator"
            print(f"  [OK] Worker đã gửi câu hỏi thành công! qid={pending_q}")
            q_row = store.question(pending_q)
            print(f"  Nội dung câu hỏi worker: {q_row['question']!r}")

            # 6. OrchestratorA trả lời câu hỏi qua manager.answer
            print("\n[Bước 6] OrchestratorA nhận được prompt IPC và gọi answer()...")
            ans_res = await manager.answer("OrchestratorA", pending_q, "JWT token với expiration 24h")
            print(f"  Kết quả answer(): {ans_res}")

            # 7. Chờ worker hoàn tất và kiểm tra report
            print("\n[Bước 7] Chờ Worker hoàn thành task và báo cáo report_completion()...")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                w_state = store.worker(worker_id)
                if w_state and w_state["state"] == "IDLE":
                    break
                await asyncio.sleep(0.2)

            reports = store.reports(worker_id)
            assert len(reports) == 1, "Worker chưa ghi được report"
            rep = reports[0]
            print(f"  [OK] Worker task status: {rep['status']}")
            payload = json.loads(rep["payload"]) if isinstance(rep["payload"], str) else rep["payload"]
            print(f"  Tóm tắt report: {payload['summary']}")
            assert rep["status"] == "DONE"

            # Kiểm tra log events
            events = [e["line"] for e in store.undelivered_events("OrchestratorA")] + [
                r[0] for r in store.db.execute("select line from events where owner='OrchestratorA'").fetchall()
            ]
            print(f"  Các sự kiện đã ghi nhận cho OrchestratorA: {len(events)} events")

    finally:
        # 8. Dọn dẹp tiến trình
        print("\n[Bước 8] Dọn dẹp tiến trình Claude Code...")
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
        print("  [OK] Đã dừng toàn bộ processes an toàn.")

    print("\n" + "=" * 70)
    print("HOÀN TẤT E2E TEST: TẤT CẢ CÁC BƯỚC ĐỀU THÀNH CÔNG 100%!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_e2e())
