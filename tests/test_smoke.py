"""Smoke tests: models, store, worker loop (fake client), init idempotency.

Run: uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coord import cli
from coord.backend import WorkerSession
from coord.manager import Manager
from coord.models import TaskPacket, WorkerReport
from coord.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ------------------------------------------------------------------ fakes
class _Function:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index: int, name: str, arguments: str) -> None:
        self.id = f"call_{index}"
        self.function = _Function(name, arguments)


class _Message:
    def __init__(self, content: str | None = None, tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none: bool = False) -> dict:
        data: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in self.tool_calls
            ]
        elif not exclude_none:
            data["tool_calls"] = None
        return data


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


class _Completions:
    def __init__(self, script: list[_Response]) -> None:
        self.script = script
        self.calls: list[dict] = []

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("model được gọi quá số lần kịch bản")
        return self.script.pop(0)


class _FakeClient:
    def __init__(self, script: list[_Response]) -> None:
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def _call(index: int, name: str, args: str) -> _Message:
    return _Message(tool_calls=[_ToolCall(index, name, args)])


# ------------------------------------------------------------------ tests
class ModelTests(unittest.TestCase):
    def test_task_packet_validates_id(self) -> None:
        packet = TaskPacket.model_validate_json(
            '{"id":"A-07","goal":"g","acceptance":["a"]}'
        )
        self.assertEqual(packet.max_rounds, 3)
        with self.assertRaises(ValueError):
            TaskPacket.model_validate_json('{"id":"nope","goal":"g","acceptance":["a"]}')

    def test_worker_report_requires_summary(self) -> None:
        with self.assertRaises(ValueError):
            WorkerReport.model_validate({"id": "A-07", "status": "DONE"})
        report = WorkerReport.model_validate({"id": "A-07", "status": "DONE", "summary": "ok"})
        self.assertEqual(report.files_changed, [])


class StoreTests(unittest.TestCase):
    def test_worker_and_question_lifecycle(self) -> None:
        store = Store(":memory:")
        store.add_worker("A-1a2b", "A", "/repo", "/repo/wt-A", "A-01")
        self.assertEqual(store.worker("A-1a2b")["state"], "RUNNING")
        store.set_state("A-1a2b", "WAITING_ANSWER")
        store.add_question("q1", "A-1a2b", "A-01", "which shape?")
        self.assertEqual(store.pending_questions("A-1a2b"), ["q1"])
        store.answer_question("q1", "use token/exp")
        self.assertEqual(store.pending_questions("A-1a2b"), [])
        self.assertEqual(len(store.list_workers(owner="A")), 1)
        self.assertEqual(store.list_workers(owner="B"), [])


class WorkerLoopTests(unittest.TestCase):
    def _session(self, tmp: Path, script: list[_Response], reports: list[WorkerReport]):
        async def on_report(report: WorkerReport) -> None:
            reports.append(report)

        async def ask(task_id: str, question: str) -> str:
            return "use 42"

        return WorkerSession(
            worker_id="A-0001",
            owner="A",
            worktree=tmp,
            model="fake",
            client=_FakeClient(script),  # type: ignore[arg-type]
            ask=ask,
            on_report=on_report,
        )

    def test_write_then_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            reports: list[WorkerReport] = []
            script = [
                _Response(_call(1, "write_file", '{"path":"out.txt","content":"hello"}')),
                _Response(
                    _call(
                        2,
                        "report_completion",
                        '{"report":{"id":"A-01","status":"DONE","summary":"wrote file"}}',
                    )
                ),
            ]
            session = self._session(tmp, script, reports)
            asyncio.run(session.run("write out.txt"))
            self.assertEqual((tmp / "out.txt").read_text(), "hello")
            self.assertEqual(len(reports), 1)
            self.assertEqual(session.report.status, "DONE")  # type: ignore[union-attr]

    def test_missing_report_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            script = [_Response(_Message(content="done!")) for _ in range(3)]
            session = self._session(tmp, script, [])
            with self.assertRaises(RuntimeError):
                asyncio.run(session.run("do nothing"))

    def test_ask_orchestrator_returns_answer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            reports: list[WorkerReport] = []
            script = [
                _Response(_call(1, "ask_orchestrator", '{"task_id":"A-01","question":"value?"}')),
                _Response(
                    _call(
                        2,
                        "report_completion",
                        '{"report":{"id":"A-01","status":"DONE","summary":"used answer"}}',
                    )
                ),
            ]
            session = self._session(tmp, script, reports)
            asyncio.run(session.run("ask first"))
            tool_messages = [m for m in session.messages if m.get("role") == "tool"]
            self.assertEqual(tool_messages[0]["content"], "use 42")


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.add_worker("A-1", "A", "/p", "/p/wt-A", "A-01")
        self.manager = Manager(self.store, ask_timeout=5)

    async def test_ask_emits_event_and_answer_unblocks(self) -> None:
        pending = asyncio.create_task(
            self.manager._ask("A-1", "A", "A-01", "which response shape?")
        )
        await asyncio.sleep(0.05)
        line = self.manager.queue("A").get_nowait()
        self.assertRegex(line, r"^\[A-1\] ASK q-[0-9a-f]+ A-01: which response shape\?")
        self.assertEqual(self.store.worker("A-1")["state"], "WAITING_ANSWER")

        qid = self.store.pending_questions("A-1")[0]
        self.assertEqual(await self.manager.answer("A", f"q-{qid}", "token/exp"), "ok")
        self.assertEqual(await pending, "token/exp")
        self.assertEqual(self.store.worker("A-1")["state"], "RUNNING")

    async def test_owner_mismatch_is_rejected(self) -> None:
        self.store.add_question("abc123", "A-1", "A-01", "?")
        with self.assertRaises(ValueError):
            await self.manager.answer("B", "abc123", "nope")
        with self.assertRaises(ValueError):
            await self.manager.kill("B", "A-1")

    async def test_fork_requires_idle_worker(self) -> None:
        packet = '{"id":"A-02","goal":"g","acceptance":["a"]}'
        with self.assertRaises(ValueError):
            await self.manager.fork_worker("A", "A-1", packet)
        self.store.set_state("A-1", "IDLE")
        with self.assertRaises(ValueError):
            await self.manager.fork_worker("A", "A-1", packet)  # no conversation in memory

    async def test_kill_cancels_worker_and_questions(self) -> None:
        self.store.set_state("A-1", "WAITING_ANSWER")
        self.store.add_question("dead01", "A-1", "A-01", "?")
        future = asyncio.get_running_loop().create_future()
        self.manager.pending["dead01"] = future
        self.assertEqual(await self.manager.kill("A", "A-1"), "ok")
        self.assertEqual(self.store.worker("A-1")["state"], "KILLED")
        self.assertTrue(future.cancelled())
        self.assertEqual(self.store.pending_questions("A-1"), [])

    async def test_status_reports_parent_and_task(self) -> None:
        rows = self.manager.status(owner="A")
        self.assertEqual(rows[0]["worker_id"], "A-1")
        self.assertEqual(rows[0]["task_id"], "A-01")
        self.assertIsNone(rows[0]["parent"])


class InitTests(unittest.TestCase):
    def test_init_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = tmp / "proj"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
            (repo / "README.md").write_text("x")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

            args = cli.build_parser().parse_args(
                ["init", "A", "B", "--repo", str(repo), "--worktree-root", str(tmp / "wts")]
            )
            self.assertEqual(cli.cmd_init(args), 0)
            self.assertTrue((repo / ".coord" / "STATE.md").is_file())
            self.assertTrue((tmp / "wts" / "wt-A").is_dir())
            self.assertIn("/.coord/", (repo / ".gitignore").read_text())
            claude = (repo / "CLAUDE.md").read_text()
            self.assertEqual(claude.count(cli.BEGIN), 1)

            self.assertEqual(cli.cmd_init(args), 0)
            self.assertEqual((repo / "CLAUDE.md").read_text().count(cli.BEGIN), 1)
            self.assertEqual((repo / ".gitignore").read_text().count("/.coord/"), 1)


if __name__ == "__main__":
    unittest.main()
