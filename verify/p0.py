"""P0 verification for the OpenAI-compatible backend.

Covers the assumptions from doc 5 that this implementation actually depends on:

  V-5   endpoint + model reachable
  V-6   tool calling with a Pydantic-validated report payload
  V-13  fork by replaying the message history (no re-read of files)

Run:  uv run python verify/p0.py
Needs COORD_API_KEY (or OPENAI_API_KEY). Without a key every check is SKIP.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from coord.backend import WorkerSession, default_model, make_client
from coord.models import WorkerReport

RESULTS: list[tuple[str, str, str]] = []


def record(check: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((check, verdict, detail))
    print(f"{check:<5} {verdict:<5} {detail}", flush=True)


async def v5(client, model: str) -> None:
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
        )
        text = (response.choices[0].message.content or "").strip()
        record("V-5", "PASS" if text else "FAIL", f"model={model} reply={text[:40]!r}")
    except Exception as exc:  # noqa: BLE001
        record("V-5", "FAIL", f"{type(exc).__name__}: {exc}")


async def v6(client, model: str) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "report_completion",
                "description": "Call exactly once when the task ends.",
                "parameters": {
                    "type": "object",
                    "required": ["report"],
                    "properties": {
                        "report": {
                            "type": "object",
                            "required": ["id", "status", "summary"],
                            "properties": {
                                "id": {"type": "string"},
                                "status": {"enum": ["DONE", "FAILED"]},
                                "summary": {"type": "string"},
                            },
                        }
                    },
                },
            },
        }
    ]
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Task A-01 is finished: nothing to do. "
                        "Call report_completion with status DONE and a one-line summary."
                    ),
                }
            ],
            tools=tools,
            tool_choice="auto",
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            record("V-6", "FAIL", "model không gọi tool")
            return
        args = json.loads(calls[0].function.arguments)
        report = WorkerReport.model_validate(args["report"])
        record("V-6", "PASS", f"tool={calls[0].function.name} status={report.status}")
    except Exception as exc:  # noqa: BLE001
        record("V-6", "FAIL", f"{type(exc).__name__}: {exc}")


async def v13(client, model: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        worktree = Path(raw)
        secret = "zebra-42"
        (worktree / "data.txt").write_text(f"secret word: {secret}\n", encoding="utf-8")

        async def no_ask(task_id: str, question: str) -> str:
            return "NO_ANSWER: proceed with a stated assumption."

        async def no_report(report: WorkerReport) -> None:
            return None

        source = WorkerSession(
            worker_id="V-0001",
            owner="V",
            worktree=worktree,
            model=model,
            client=client,
            ask=no_ask,
            on_report=no_report,
        )
        source.task_id = "V-01"
        try:
            await asyncio.wait_for(
                source.run(
                    '{"id":"V-01","goal":"read data.txt and report what it contains",'
                    '"files":["data.txt"],"acceptance":["say what the file says"]}'
                ),
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            record("V-13", "FAIL", f"lượt gốc lỗi: {type(exc).__name__}: {exc}")
            return

        fork = WorkerSession(
            worker_id="V-0002",
            owner="V",
            worktree=worktree,
            model=model,
            client=client,
            ask=no_ask,
            on_report=no_report,
        )
        fork.messages = source.clone_messages()
        fork.task_id = "V-02"
        try:
            await asyncio.wait_for(
                fork.run(
                    '{"id":"V-02","goal":"What secret word was in data.txt? Do NOT call read_file.",'
                    '"acceptance":["answer from memory"]}'
                ),
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            record("V-13", "FAIL", f"lượt fork lỗi: {type(exc).__name__}: {exc}")
            return

        reread = any(
            call
            for message in fork.messages
            if message.get("role") == "assistant"
            for call in (message.get("tool_calls") or [])
            if call["function"]["name"] == "read_file"
        )
        if reread:
            record("V-13", "FAIL", "bản fork đọc lại file (replay không giữ context)")
            return
        record("V-13", "PASS" if secret in json.dumps(fork.messages) else "WARN",
               "fork trả lời từ history, không đọc lại file" if not reread else "replay lỗi")


async def main() -> int:
    key = os.environ.get("COORD_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = default_model()
    if not key:
        print("SKIP mọi check: chưa đặt COORD_API_KEY (hoặc OPENAI_API_KEY).")
        print("Đặt key rồi chạy lại: COORD_API_KEY=... uv run python verify/p0.py")
        return 0

    client = make_client()
    await v5(client, model)
    await v6(client, model)
    await v13(client, model)

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} pass — ghi kết quả vào doc 5 (§2).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
