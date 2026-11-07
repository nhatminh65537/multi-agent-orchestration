"""Unit + smoke tests for coord v0.6.

Run: uv run python -m unittest discover -s tests -v
No API key, no network, no SDK binary needed: the backend is faked through
`Manager(session_factory=...)`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Bug B-7 (0.3): extend sys.path BEFORE importing the package so the tests run
# from a clean clone without `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.backend import (  # noqa: E402
    BackendUnavailable,
    WorkerAPIError,
    WorkerSession,
    build_instructions,
    build_session,
    classify_error,
    parse_retry_after,
    project_context,
)
from coord.config import default_backend  # noqa: E402
from coord.manager import Manager  # noqa: E402
from coord.models import TaskPacket  # noqa: E402
from coord.policy import check_tool_policy  # noqa: E402
from coord.store import Store  # noqa: E402


# ------------------------------------------------------------------ fakes
class FakeSession(WorkerSession):
    """Fake backend: records each turn and returns the final answer."""

    backend_name = "fake"

    def __init__(
        self,
        *,
        response: str = "DONE: ok",
        gate: asyncio.Event | None = None,
        **kwargs,  # noqa: ANN003
    ) -> None:
        self.initial_session_id = kwargs.get("session_id")
        super().__init__(**kwargs)
        self._response = response
        self._gate = gate
        self.turns: list[str] = []
        self.started = asyncio.Event()

    async def _run_turn(self, text: str, model: str) -> str:
        self.turns.append(text)
        self.sdk_session_id = f"sess-{self.worker_id}"
        if self._gate is not None:
            self.started.set()
            await self._gate.wait()
        return self._response


class AskingSession(FakeSession):
    """Worker that asks the orchestrator one question before answering."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.answers: list[str] = []

    async def _run_turn(self, text: str, model: str) -> str:
        self.turns.append(text)
        self.sdk_session_id = f"sess-{self.worker_id}"
        self.started.set()
        answer = await self._ask("tokens or cookie?")
        self.answers.append(answer)
        return f"DONE: using {answer}"


class FlakySession(WorkerSession):
    """The first `failures` turns raise, then the session succeeds."""

    backend_name = "flaky"

    def __init__(
        self,
        *,
        failures: int = 1,
        message: str = "429 Resource Exhausted",
        **kwargs,  # noqa: ANN003
    ) -> None:
        super().__init__(**kwargs)
        self.attempts: list[str] = []
        self._failures = failures
        self._message = message

    async def _run_turn(self, text: str, model: str) -> str:
        self.attempts.append(model)
        if len(self.attempts) <= self._failures:
            raise RuntimeError(self._message)
        return f"DONE: ran on {model}"


class FailingSession(WorkerSession):
    """Every turn raises, so the manager has to report the provider error."""

    backend_name = "failing"

    async def _run_turn(self, text: str, model: str) -> str:
        raise RuntimeError("429 RESOURCE_EXHAUSTED: You exceeded your current quota")


def fake_factory(response: str = "DONE: ok", gate: asyncio.Event | None = None):
    def make(backend: str, *, packet: TaskPacket | None, **kwargs):  # noqa: ANN001, ANN003
        return FakeSession(response=response, gate=gate, packet=packet, **kwargs)

    return make


def asking_factory(backend: str, *, packet: TaskPacket | None, **kwargs):  # noqa: ANN001, ANN003
    return AskingSession(packet=packet, **kwargs)


async def _no_ask(question: str) -> str:
    return "x"


def _packet(task_id: str, files: list[str] | None = None, **extra) -> str:
    body = {
        "id": task_id,
        "goal": "do the thing",
        "acceptance": ["pytest -q"],
        "files": files or [],
        **extra,
    }
    return json.dumps(body)


async def _settle(manager: Manager, worker_id: str) -> None:
    task = manager.tasks.get(worker_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)


async def _wait_state(manager: Manager, worker_id: str, state: str, tries: int = 300) -> None:
    for _ in range(tries):
        if manager.store.worker(worker_id)["state"] == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{worker_id} never reached {state}")


# ------------------------------------------------------------------ models
class ModelTests(unittest.TestCase):
    def test_task_packet_validates_id(self) -> None:
        packet = TaskPacket.model_validate_json(_packet("A-07"))
        self.assertFalse(packet.read_only)
        with self.assertRaises(ValueError):
            TaskPacket.model_validate_json(_packet("nope"))
        with self.assertRaises(ValueError):
            TaskPacket.model_validate_json(_packet("A-07-b c"))

    def test_task_id_accepts_retry_codes(self) -> None:
        for task_id in ("B-42b", "M-5r", "A-07", "A-1.2"):
            packet = TaskPacket.model_validate_json(_packet(task_id))
            self.assertEqual(packet.id, task_id)

    def test_read_only_is_explicit_only(self) -> None:
        packet = TaskPacket.model_validate_json(
            _packet("A-08", constraints=["Read-only: analysis only"])
        )
        self.assertFalse(packet.read_only)


# ------------------------------------------------------------------- policy
class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt-A"
        self.wt.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_allows_write_inside_worktree(self) -> None:
        ok, reason = check_tool_policy("Write", {"file_path": str(self.wt / "a.py")}, self.wt)
        self.assertTrue(ok, reason)

    def test_denies_absolute_path_outside(self) -> None:
        outside = Path(self.tmp.name) / "wt-B" / "a.py"
        ok, reason = check_tool_policy("Write", {"file_path": str(outside)}, self.wt)
        self.assertFalse(ok)
        self.assertIn("outside worktree", reason)

    def test_denies_traversal(self) -> None:
        ok, _ = check_tool_policy("Edit", {"file_path": "../wt-B/src/api.py"}, self.wt)
        self.assertFalse(ok)

    def test_denies_git_verbs(self) -> None:
        for command in ("git commit -m x", "git merge stream-B", "git push"):
            ok, reason = check_tool_policy("Bash", {"command": command}, self.wt)
            self.assertFalse(ok, command)
            self.assertIn("git", reason)

    def test_allows_git_status(self) -> None:
        ok, reason = check_tool_policy("Bash", {"command": "git status --short"}, self.wt)
        self.assertTrue(ok, reason)

    def test_denies_needs_approval(self) -> None:
        ok, reason = check_tool_policy(
            "Bash", {"command": "pip install requests"}, self.wt, ["pip install"]
        )
        self.assertFalse(ok)
        self.assertIn("needs_approval", reason)

    def test_denies_coord_mcp(self) -> None:
        ok, reason = check_tool_policy("mcp__coord__spawn_worker", {"owner": "A"}, self.wt)
        self.assertFalse(ok)
        self.assertIn("recursive spawn_worker", reason)

    def test_denies_subagent(self) -> None:
        ok, _ = check_tool_policy("Task", {"prompt": "x"}, self.wt)
        self.assertFalse(ok)

    def test_read_only_blocks_every_write_tool(self) -> None:
        for tool in (
            "Write", "Edit", "MultiEdit", "create_file", "edit_file",
            "write_to_file", "replace_file_content",
        ):
            ok, reason = check_tool_policy(
                tool, {"file_path": str(self.wt / "a.py"), "path": str(self.wt / "a.py")},
                self.wt, read_only=True,
            )
            self.assertFalse(ok, tool)
            self.assertIn("read_only", reason)
        ok, reason = check_tool_policy(
            "Read", {"file_path": str(self.wt / "a.py")}, self.wt, read_only=True
        )
        self.assertTrue(ok, reason)

    def test_antigravity_tool_names(self) -> None:
        ok, _ = check_tool_policy("create_file", {"path": str(self.wt / "b.py")}, self.wt)
        self.assertTrue(ok)
        ok, _ = check_tool_policy("run_command", {"CommandLine": "git reset --hard"}, self.wt)
        self.assertFalse(ok)

    def test_antigravity_harness_path_keys(self) -> None:
        """agy-sdk tools send PascalCase keys (`TargetFile`), not just `path`."""
        inside = str(self.wt / "a.py")
        for key in ("TargetFile", "target_file", "Path", "path", "file_path", "filePath"):
            ok, reason = check_tool_policy("write_to_file", {key: inside}, self.wt)
            self.assertTrue(ok, f"{key}: {reason}")

        outside = str(Path(self.tmp.name) / "wt-B" / "a.py")
        for key in ("TargetFile", "target_file"):
            ok, reason = check_tool_policy("replace_file_content", {key: outside}, self.wt)
            self.assertFalse(ok, key)
            self.assertIn("outside worktree", reason)

    def test_antigravity_read_tools_use_pascal_case_keys(self) -> None:
        outside = str(Path(self.tmp.name) / "wt-B" / "a.py")
        payload = {"AbsolutePath": outside, "DirectoryPath": outside, "SearchPath": outside}
        for tool in ("view_file", "list_dir", "grep_search"):
            ok, reason = check_tool_policy(tool, payload, self.wt)
            self.assertFalse(ok, tool)
            self.assertIn("outside worktree", reason)

    def test_write_without_any_path_is_denied(self) -> None:
        ok, reason = check_tool_policy("write_to_file", {"CodeContent": "line1"}, self.wt)
        self.assertFalse(ok)
        self.assertIn("no path", reason)

    def test_file_uri_is_checked_on_the_real_path(self) -> None:
        inside = (self.wt / "a.py").as_uri()
        ok, reason = check_tool_policy("write_to_file", {"TargetFile": inside}, self.wt)
        self.assertTrue(ok, reason)
        outside = (Path(self.tmp.name) / "wt-B" / "a.py").as_uri()
        ok, reason = check_tool_policy("write_to_file", {"TargetFile": outside}, self.wt)
        self.assertFalse(ok)
        # An unknown scheme cannot be verified, so it stays denied.
        ok, _ = check_tool_policy("write_to_file", {"TargetFile": "cns://blob/abc"}, self.wt)
        self.assertFalse(ok)


# -------------------------------------------------------------------- store
class StoreTests(unittest.TestCase):
    def test_worker_and_question_lifecycle(self) -> None:
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.add_worker("A-1a2b", "A", "/repo", "/repo/wt-A", "A-01")
        self.assertEqual(store.worker("A-1a2b")["state"], "RUNNING")
        store.set_state("A-1a2b", "WAITING_ANSWER")
        store.add_question("q1", "A-1a2b", "A-01", "which shape?")
        self.assertEqual(store.pending_questions("A-1a2b"), ["q1"])
        store.answer_question("q1", "use token/exp")
        self.assertEqual(store.pending_questions("A-1a2b"), [])
        self.assertEqual(len(store.list_workers(owner="A")), 1)
        self.assertEqual(store.list_workers(owner="B"), [])

    def test_migrates_v04_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "coord.db"
            raw = sqlite3.connect(db)
            raw.executescript(
                """
                create table workers (id text primary key, owner text not null, project text not null,
                    worktree text not null, task_id text, state text not null, parent text,
                    created_at text not null, updated_at text not null);
                """
            )
            raw.execute(
                "insert into workers values('A-1','A','/p','/p/wt-A','A-01','IDLE',null,'t','t')"
            )
            raw.commit()
            raw.close()

            store = Store(db)
            self.addCleanup(store.close)
            row = store.worker("A-1")
            self.assertEqual(row["state"], "IDLE")
            self.assertEqual(row["backend"], "agy-sdk")  # new column takes the current default
            self.assertEqual(json.loads(row["files_json"]), [])

    def test_set_task_binds_files_and_read_only(self) -> None:
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.add_worker("A-1", "A", "/repo", "/repo/wt-A", "A-01")
        store.set_task("A-1", "A-02", files=["src/api.py"], read_only=True)
        row = store.worker("A-1")
        self.assertEqual(row["task_id"], "A-02")
        self.assertEqual(json.loads(row["files_json"]), ["src/api.py"])
        self.assertEqual(row["read_only"], 1)

    def test_event_logging(self) -> None:
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.add_event("A", "one")
        store.add_event("A", "two")
        rows = store.db.execute("select line from events where owner='A' order by id").fetchall()
        self.assertEqual([r[0] for r in rows], ["one", "two"])


# ------------------------------------------------------------------ backend
class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)
        (self.wt / "README.md").write_text("# demo\n", encoding="utf-8")
        (self.wt / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_backend_is_gemini(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(default_backend(), "agy-sdk")

    def test_project_context_mentions_worktree_and_branch(self) -> None:
        text = project_context(self.wt, "A")
        self.assertIn(str(self.wt), text)
        self.assertIn("Branch:", text)
        self.assertIn("README.md", text)

    def test_skill_body_is_optional(self) -> None:
        without = build_instructions(self.wt, owner="A", with_skill_body=False)
        self.assertIn("# rules", without)
        self.assertNotIn("name: worker-protocol", without)

        with_body = build_instructions(self.wt, owner="A", with_skill_body=True)
        self.assertIn("name: worker-protocol", with_body)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(BackendUnavailable):
            build_session(
                "legacy-loop",
                worker_id="A-1",
                owner="A",
                worktree=self.wt,
                model="m",
                packet=None,
                ask=_no_ask,
            )

    def test_fallback_model_takes_over_on_api_error(self) -> None:
        session = FlakySession(
            worker_id="A-1", owner="A", worktree=self.wt, model="primary", ask=_no_ask
        )
        with mock.patch.dict(os.environ, {"WORKER_FALLBACK_MODEL": "backup1, backup2"}):
            result = asyncio.run(session.run("go"))
        self.assertEqual(result, "DONE: ran on backup1")
        self.assertEqual(session.attempts, ["primary", "backup1"])
        self.assertEqual(session.model, "backup1")

    def test_fallback_is_skipped_when_no_fallback_configured(self) -> None:
        session = FlakySession(
            worker_id="A-1", owner="A", worktree=self.wt, model="primary", ask=_no_ask
        )
        with mock.patch.dict(os.environ, {"WORKER_QUOTA_WAIT_SECONDS": "0"}, clear=True):
            with self.assertRaises(RuntimeError):
                asyncio.run(session.run("go"))
        self.assertEqual(session.attempts, ["primary"])

    def test_deepcopy_of_a_live_session_is_a_noop(self) -> None:
        session = FlakySession(
            worker_id="A-1", owner="A", worktree=self.wt, model="m", ask=_no_ask
        )
        session._agent = threading.RLock()  # type: ignore[attr-defined]
        self.assertIs(copy.deepcopy(session), session)

    def test_agent_config_deepcopy_does_not_pickle_the_session(self) -> None:
        """Regression for `cannot pickle '_thread.RLock' object` on the second turn."""
        try:
            from google.antigravity import LocalAgentConfig
        except Exception:  # noqa: BLE001 - optional SDK
            self.skipTest("google-antigravity is not installed")

        from coord.backend import AntigravityWorkerSession

        session = AntigravityWorkerSession(
            worker_id="A-1", owner="A", worktree=self.wt, model="m", ask=_no_ask
        )
        session._agent = threading.RLock()  # type: ignore[attr-defined]
        config = LocalAgentConfig(
            system_instructions="x", tools=[session._tool_ask], api_key="k"
        )
        copied = config.model_copy(deep=True)
        self.assertIsNotNone(copied)


# ------------------------------------------------------------------ manager
class QuotaTests(unittest.TestCase):
    """Provider quota/rate-limit errors must be classified, waited out and reported."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_retry_after(self) -> None:
        self.assertEqual(parse_retry_after('retryDelay: "37s"'), 37.0)
        self.assertEqual(parse_retry_after("Please retry in 12.5s."), 12.5)
        self.assertEqual(parse_retry_after("retry-after: 30"), 30.0)
        self.assertIsNone(parse_retry_after("model not found"))

    def test_classify_error_flags_quota_and_overload(self) -> None:
        quota = classify_error(RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded"))
        self.assertTrue(quota.retryable)
        self.assertIn("429", str(quota))
        self.assertTrue(classify_error(RuntimeError("503 Service Unavailable")).retryable)
        self.assertFalse(classify_error(RuntimeError("model not found")).retryable)
        self.assertFalse(classify_error(ValueError("bad request")).retryable)

    def test_worker_waits_for_quota_then_succeeds(self) -> None:
        notices: list[str] = []
        session = FlakySession(
            worker_id="A-1",
            owner="A",
            worktree=self.wt,
            model="primary",
            ask=_no_ask,
            notify=notices.append,
            failures=1,
            message='429 RESOURCE_EXHAUSTED: quota exceeded, retryDelay: "1s"',
        )
        with mock.patch.dict(os.environ, {"WORKER_QUOTA_WAIT_SECONDS": "30"}, clear=False):
            result = asyncio.run(session.run("go"))
        self.assertEqual(result, "DONE: ran on primary")
        self.assertEqual(session.attempts, ["primary", "primary"])
        self.assertTrue(any("Waiting" in line for line in notices), notices)

    def test_worker_gives_up_when_quota_budget_is_zero(self) -> None:
        notices: list[str] = []
        session = FlakySession(
            worker_id="A-1",
            owner="A",
            worktree=self.wt,
            model="primary",
            ask=_no_ask,
            notify=notices.append,
            failures=5,
        )
        with mock.patch.dict(os.environ, {"WORKER_QUOTA_WAIT_SECONDS": "0"}, clear=False):
            with self.assertRaises(WorkerAPIError) as ctx:
                asyncio.run(session.run("go"))
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(notices, [])

    def test_non_retryable_error_never_waits(self) -> None:
        session = FlakySession(
            worker_id="A-1",
            owner="A",
            worktree=self.wt,
            model="primary",
            ask=_no_ask,
            failures=5,
            message="404 model not found",
        )
        with mock.patch.dict(os.environ, {"WORKER_QUOTA_WAIT_SECONDS": "300"}, clear=False):
            with self.assertRaises(WorkerAPIError):
                asyncio.run(session.run("go"))
        self.assertEqual(session.attempts, ["primary"])


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt-A"
        self.wt.mkdir()
        self.store = Store(":memory:")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _manager(self, response: str = "DONE: ok", restore: bool = True) -> Manager:
        return Manager(
            self.store,
            backend="agy-sdk",
            model="fake",
            restore=restore,
            session_factory=fake_factory(response),
        )

    def test_spawn_response_and_log(self) -> None:
        manager = self._manager()

        async def scenario() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(scenario())
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")
        log = self.wt / ".coord" / "logs" / "A-01.md"
        self.assertTrue(log.is_file())
        body = log.read_text(encoding="utf-8")
        self.assertIn("## response", body)
        self.assertIn("DONE: ok", body)
        status = manager.status(owner="A")[0]
        self.assertEqual(status["backend"], "agy-sdk")
        self.assertEqual(status["pending_questions"], [])
        self.assertNotIn("parent", status)
        self.assertIn("updated_at", status)

    def test_failed_response_marks_failed(self) -> None:
        manager = self._manager(response="FAILED: not allowed to change the schema")

        async def scenario() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(scenario())
        self.assertEqual(self.store.worker(wid)["state"], "FAILED")

    def test_empty_response_marks_failed(self) -> None:
        manager = self._manager(response="   ")

        async def scenario() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(scenario())
        self.assertEqual(self.store.worker(wid)["state"], "FAILED")
        log = (self.wt / ".coord" / "logs" / "A-01.md").read_text(encoding="utf-8")
        self.assertIn("without returning any answer", log)

    def test_overlap_rejected_and_read_only_allowed(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            first = await manager.spawn_worker(
                "A", str(self.wt), _packet("A-01", files=["src/api.py"])
            )
            with self.assertRaises(ValueError):
                await manager.spawn_worker(
                    "A", str(self.wt), _packet("A-02", files=["src/api.py"])
                )
            await manager.spawn_worker(
                "A", str(self.wt), _packet("A-03", files=["src/api.py"], read_only=True)
            )
            # let it finish so no task leaks
            await _settle(manager, first)

        asyncio.run(scenario())

    def test_no_overlap_on_different_worktrees(self) -> None:
        other = Path(self.tmp.name) / "wt-B"
        other.mkdir()
        manager = self._manager()

        async def scenario() -> None:
            a = await manager.spawn_worker("A", str(self.wt), _packet("A-01", files=["x.py"]))
            b = await manager.spawn_worker("B", str(other), _packet("B-01", files=["x.py"]))
            await _settle(manager, a)
            await _settle(manager, b)

        asyncio.run(scenario())

    def test_restart_marks_running_failed_and_keeps_idle(self) -> None:
        self.store.add_worker(
            "A-idle", "A", "/p", str(self.wt), "A-01", backend="agy-sdk", session_id="s1"
        )
        self.store.set_state("A-idle", "IDLE")
        self.store.add_worker("A-run", "A", "/p", str(self.wt), "A-02", backend="agy-sdk")
        self.store.add_worker("A-wait", "A", "/p", str(self.wt), "A-03", backend="agy-sdk")
        self.store.set_state("A-wait", "WAITING_ANSWER")
        self.store.add_question("q1", "A-wait", "A-03", "?")

        manager = Manager(
            self.store, backend="agy-sdk", model="fake", session_factory=fake_factory()
        )

        self.assertEqual(self.store.worker("A-idle")["state"], "IDLE")
        self.assertEqual(self.store.worker("A-run")["state"], "FAILED")
        self.assertEqual(self.store.worker("A-wait")["state"], "FAILED")
        self.assertEqual(self.store.question("q1")["answer"], "SERVER_RESTARTED")
        rows = manager.status(owner="A", include_all=True)
        self.assertTrue(all(not row["pending_questions"] for row in rows))
        # FAILED/KILLED rows of old tasks are hidden by default.
        self.assertEqual([r["worker_id"] for r in manager.status(owner="A")], ["A-idle"])

    def test_send_after_restart_reuses_session_id(self) -> None:
        manager = self._manager()

        async def first() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(first())
        self.assertEqual(self.store.worker(wid)["session_id"], f"sess-{wid}")

        restarted = self._manager()
        self.assertNotIn(wid, restarted.sessions)

        async def second() -> None:
            await restarted.send("A", wid, _packet("A-02"))
            await _settle(restarted, wid)

        asyncio.run(second())
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")
        self.assertEqual(restarted.sessions[wid].initial_session_id, f"sess-{wid}")
        self.assertEqual(self.store.worker(wid)["task_id"], "A-02")

    def test_send_to_running_worker_queues_another_turn(self) -> None:
        gate = asyncio.Event()

        async def scenario() -> tuple[str, FakeSession, str]:
            manager = Manager(
                self.store,
                backend="agy-sdk",
                model="fake",
                restore=False,
                session_factory=fake_factory(gate=gate),
            )
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            session = manager.sessions[wid]
            await asyncio.wait_for(session.started.wait(), timeout=2)
            queued = await manager.send("A", wid, "also update the docstring")
            gate.set()
            await _settle(manager, wid)
            return wid, session, queued

        wid, session, queued = asyncio.run(scenario())
        self.assertIn("queued", queued)
        self.assertEqual(session.turns, [_packet("A-01"), "also update the docstring"])
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")

    def test_send_to_waiting_worker_answers_question(self) -> None:
        async def scenario() -> tuple[str, AskingSession, str]:
            manager = Manager(
                self.store,
                backend="agy-sdk",
                model="fake",
                restore=False,
                session_factory=asking_factory,
            )
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            session = manager.sessions[wid]
            await asyncio.wait_for(session.started.wait(), timeout=2)
            await _wait_state(manager, wid, "WAITING_ANSWER")
            result = await manager.send("A", wid, "use tokens")
            await _settle(manager, wid)
            return wid, session, result

        wid, session, result = asyncio.run(scenario())
        self.assertEqual(result, "ok")
        self.assertEqual(session.answers, ["use tokens"])
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")

    def test_touched_files_outside_packet_are_reported(self) -> None:
        sent: list[str] = []

        async def fake_async_send(target: str, text: str, priority: str = "next"):
            sent.append(text)
            return True, "ok"

        async def scenario() -> None:
            manager = self._manager()
            wid = await manager.spawn_worker(
                "A", str(self.wt), _packet("A-01", files=["src/api.py"])
            )
            session = manager.sessions[wid]
            session.touched_files.update(
                {str(self.wt / "src/api.py"), str(self.wt / "extra.txt")}
            )
            await _settle(manager, wid)

        with mock.patch("coord.manager.async_send_session_message", side_effect=fake_async_send):
            asyncio.run(scenario())

        self.assertTrue(any("Modified files outside TaskPacket.files" in s for s in sent))
        self.assertTrue(any("extra.txt" in s for s in sent))

    def test_apply_packet_clears_previous_touched_files(self) -> None:
        session = FakeSession(
            worker_id="A-1",
            owner="A",
            worktree=self.wt,
            model="fake",
            packet=TaskPacket.model_validate_json(_packet("A-01", files=["src/a.py"])),
            ask=_no_ask,
        )
        session.touched_files.add(str(self.wt / "src/a.py"))
        session.apply_packet(TaskPacket.model_validate_json(_packet("A-02", files=["src/b.py"])))
        self.assertEqual(session.touched_files, set())
        self.assertEqual(session.packet_files, ["src/b.py"])

    def test_provider_error_is_forwarded_to_orchestrator(self) -> None:
        sent: list[str] = []

        async def fake_async_send(target: str, text: str, priority: str = "next"):
            sent.append(text)
            return True, "ok"

        async def scenario() -> str:
            manager = Manager(
                self.store,
                backend="agy-sdk",
                model="fake",
                restore=False,
                session_factory=lambda backend, **kwargs: FailingSession(**kwargs),
            )
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        with mock.patch.dict(os.environ, {"WORKER_QUOTA_WAIT_SECONDS": "0"}, clear=False), \
                mock.patch("coord.manager.async_send_session_message", side_effect=fake_async_send):
            wid = asyncio.run(scenario())

        self.assertEqual(self.store.worker(wid)["state"], "FAILED")
        self.assertTrue(any("RESOURCE_EXHAUSTED" in line for line in sent), sent)
        log = (self.wt / ".coord" / "logs" / "A-01.md").read_text(encoding="utf-8")
        self.assertIn("RESOURCE_EXHAUSTED", log)
        self.assertIn("## error", log)

    def test_events_recorded(self) -> None:
        manager = Manager(
            self.store, backend="agy-sdk", model="fake", restore=False,
            session_factory=fake_factory(),
        )
        manager.emit("A", "event-1")
        manager.emit("A", "event-2")
        rows = self.store.db.execute("select line from events where owner='A' order by id").fetchall()
        self.assertEqual([r[0] for r in rows], ["event-1", "event-2"])

    def test_kill_while_waiting_does_not_resurrect(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            self.store.add_worker(
                "A-stuck", "A", "/p", str(self.wt), "A-01", backend="agy-sdk"
            )
            self.store.set_state("A-stuck", "WAITING_ANSWER")
            self.store.add_question("q1", "A-stuck", "A-01", "?")
            await manager.kill("A", "A-stuck")
            self.assertEqual(self.store.worker("A-stuck")["state"], "KILLED")
            self.assertEqual(self.store.question("q1")["answer"], "KILLED")

        asyncio.run(scenario())

    def test_answer_unknown_question_raises(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            with self.assertRaises(ValueError):
                await manager.answer("A", "q-missing", "x")

        asyncio.run(scenario())

    def test_owner_isolation(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            with self.assertRaises(ValueError):
                await manager.send("B", wid, "hi")
            await _settle(manager, wid)

        asyncio.run(scenario())

    def test_antigravity_session_config_missing_key(self) -> None:
        from coord.backend import AntigravityWorkerSession, BackendUnavailable

        session = AntigravityWorkerSession(
            worker_id="w-test",
            owner="A",
            worktree=self.wt,
            model="gemini-3.8-flash-high",
            packet=TaskPacket.model_validate_json(_packet("A-01")),
            ask=mock.AsyncMock(),
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(BackendUnavailable):
                session._config("gemini-3.8-flash-high")

    def test_antigravity_session_config_with_base_url(self) -> None:
        from coord.backend import AntigravityWorkerSession

        session = AntigravityWorkerSession(
            worker_id="w-test",
            owner="A",
            worktree=self.wt,
            model="gemini-3.8-flash-high",
            packet=TaskPacket.model_validate_json(_packet("A-01")),
            ask=mock.AsyncMock(),
        )
        env_vars = {
            "WORKER_API_KEY": "test-key-123",
            "WORKER_BASE_URL": "https://llm.example.com/v1",
        }
        with mock.patch.dict(os.environ, env_vars, clear=True):
            cfg = session._config("gemini-3.8-flash-high")
            self.assertIsNotNone(cfg.models)
            self.assertEqual(len(cfg.models), 2)
            self.assertEqual(cfg.models[0].name, "gemini-3.8-flash-high")
            self.assertEqual(cfg.models[0].endpoint.base_url, "https://llm.example.com")
            self.assertEqual(cfg.models[0].endpoint.api_key, "test-key-123")
            self.assertEqual(cfg.models[1].endpoint.base_url, "https://llm.example.com")

    def test_antigravity_policies_block_read_only_and_missing_paths(self) -> None:
        try:
            from google.antigravity import BuiltinTools  # noqa: F401
        except Exception:  # noqa: BLE001
            self.skipTest("google-antigravity is not installed")

        from coord.backend import AntigravityWorkerSession

        session = AntigravityWorkerSession(
            worker_id="w-test",
            owner="A",
            worktree=self.wt,
            model="gemini-3.8-flash-high",
            packet=TaskPacket.model_validate_json(_packet("A-01", read_only=True)),
            ask=mock.AsyncMock(),
        )
        policies = {p.name: p for p in session._policies() if getattr(p, "name", None)}
        write_rule = policies["coord-worktree-write"]
        self.assertTrue(write_rule.when({"Path": str(self.wt / "a.py")}))
        self.assertTrue(write_rule.when({}))

    def test_antigravity_policies_understand_harness_arguments(self) -> None:
        try:
            from google.antigravity import BuiltinTools  # noqa: F401
        except Exception:  # noqa: BLE001
            self.skipTest("google-antigravity is not installed")

        from coord.backend import AntigravityWorkerSession

        session = AntigravityWorkerSession(
            worker_id="w-test",
            owner="A",
            worktree=self.wt,
            model="gemini-3.8-flash-high",
            packet=TaskPacket.model_validate_json(_packet("A-01")),
            ask=mock.AsyncMock(),
        )
        rules = {p.name: p for p in session._policies() if getattr(p, "name", None)}
        write_rule = rules["coord-worktree-write"]
        inside = str(self.wt / "hook-test.txt")
        outside = str(self.wt.parent / "elsewhere" / "hook-test.txt")

        # `write_to_file` sends `TargetFile`; the old code only looked for
        # `path`/`file_path` and therefore denied every write.
        self.assertFalse(write_rule.when({"TargetFile": inside}))
        self.assertTrue(write_rule.when({"TargetFile": outside}))
        # No path at all stays fail-closed.
        self.assertTrue(write_rule.when({"CodeContent": "line1"}))

        # A successful check counts progress; denials keep their real reason
        # for the manager log (the model only sees the policy name).
        self.assertEqual(session.steps, 1)
        self.assertTrue(any("outside worktree" in line for line in session.denials))



if __name__ == "__main__":
    unittest.main()
