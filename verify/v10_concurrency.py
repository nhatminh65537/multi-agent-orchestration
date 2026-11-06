"""V-10: several workers running concurrently in one process stay healthy.

Spawns N worker sessions at once, each writing a file and reporting. Reports per
worker, total time, and rate-limit errors. Needs COORD_API_KEY.

Run: uv run python verify/v10_concurrency.py [--workers 6]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import time
from pathlib import Path

from coord.backend import WorkerSession, default_model, make_client
from coord.models import WorkerReport


async def run_one(index: int, model: str, client) -> tuple[int, str, str]:
    reports: list[WorkerReport] = []

    async def on_report(report: WorkerReport) -> None:
        reports.append(report)

    async def ask(task_id: str, question: str) -> str:
        return "NO_ANSWER: proceed with a stated assumption."

    with tempfile.TemporaryDirectory() as raw:
        worktree = Path(raw)
        session = WorkerSession(
            worker_id=f"V-9{index:03d}",
            owner="V",
            worktree=worktree,
            model=model,
            client=client,
            ask=ask,
            on_report=on_report,
        )
        task = (
            f'{{"id":"V-{index:02d}","goal":"create file ok-{index}.txt containing \'ok\', '
            'then report DONE","acceptance":["file created","report DONE"]}'
        )
        session.task_id = f"V-{index:02d}"
        try:
            await asyncio.wait_for(session.run(task), timeout=300)
        except asyncio.TimeoutError:
            return index, "FAIL", "timeout 300s"
        except Exception as exc:  # noqa: BLE001
            return index, "FAIL", f"{type(exc).__name__}: {str(exc)[:120]}"

        created = (worktree / f"ok-{index}.txt").is_file()
        if not reports:
            return index, "FAIL", "không report"
        if not created:
            return index, "FAIL", "file không được tạo"
        return index, reports[0].status, reports[0].summary[:60]


async def main(count: int) -> int:
    if not (os.environ.get("COORD_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        print("SKIP V-10: chưa có COORD_API_KEY. Đặt key rồi chạy lại.")
        return 0

    model = default_model()
    client = make_client()
    started = time.monotonic()
    results = await asyncio.gather(*[run_one(i, model, client) for i in range(1, count + 1)])
    elapsed = time.monotonic() - started

    failures = 0
    for index, status, detail in sorted(results):
        print(f"worker {index}: {status:<4} {detail}")
        if status != "DONE":
            failures += 1
    print(f"\n{count - failures}/{count} DONE trong {elapsed:.1f}s (model={model})")

    if failures:
        print("FAIL V-10 — xem lỗi phía trên (429 = rate limit → giảm worker/owner)")
        return 1
    print("PASS V-10 (N worker song song trong một tiến trình)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.workers)))
