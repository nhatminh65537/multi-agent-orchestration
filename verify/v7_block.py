"""V-7: a worker tool may block for a long time without the turn being cut off.

The worker must call `ask_orchestrator`, which blocks here for N seconds, and then
continue to `report_completion`. Default N=60; use `--seconds 1800` for the full
30-minute check from doc 5. Needs COORD_API_KEY.

Run: uv run python verify/v7_block.py [--seconds 60]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import time
from pathlib import Path

from coord.backend import OpenAILoopSession as WorkerSession, default_model, make_client
from coord.models import WorkerReport


async def main(seconds: int) -> int:
    if not (os.environ.get("COORD_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        print("SKIP V-7: chưa có COORD_API_KEY. Đặt key rồi chạy lại.")
        return 0

    reports: list[WorkerReport] = []
    asked: list[str] = []

    async def on_report(report: WorkerReport) -> None:
        reports.append(report)

    async def ask(task_id: str, question: str) -> str:
        asked.append(question)
        print(f"  ask_orchestrator blocked for {seconds}s …", flush=True)
        await asyncio.sleep(seconds)
        return "ANSWER FROM ORCHESTRATOR: proceed."

    with tempfile.TemporaryDirectory() as raw:
        session = WorkerSession(
            worker_id="V-7000",
            owner="V",
            worktree=Path(raw),
            model=default_model(),
            client=make_client(),
            ask=ask,
            on_report=on_report,
        )
        session.task_id = "V-07"
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                session.run(
                    '{"id":"V-07","goal":"call ask_orchestrator once with question \'ping\', '
                    'then report DONE with summary containing the answer",'
                    '"acceptance":["ask_orchestrator called once","report DONE"]}'
                ),
                timeout=seconds + 180,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL V-7: {type(exc).__name__}: {exc}")
            return 1
        elapsed = time.monotonic() - started

    if not asked:
        print("FAIL V-7: model không gọi ask_orchestrator")
        return 1
    if not reports:
        print("FAIL V-7: không có report_completion sau khi tool trả về")
        return 1

    print(f"  blocked {elapsed:.1f}s, status={reports[0].status}, summary={reports[0].summary[:80]!r}")
    if elapsed < seconds:
        print(f"FAIL V-7: chỉ chờ {elapsed:.1f}s < {seconds}s")
        return 1
    print("PASS V-7 (tool block dài, turn vẫn tiếp tục)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.seconds)))
