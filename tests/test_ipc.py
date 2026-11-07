import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coord.ipc import (
    async_send_session_message,
    canonical,
    key_file_for,
    load_records,
    resolve_target,
    send_session_message,
)
from coord.manager import Manager
from coord.store import Store


class TestIPC(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions_dir = Path(self.tmp.name) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.env_patcher = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name})
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tmp.cleanup()

    def _create_fake_session(
        self, pid: int, name: str, session_id: str, socket_path: str, token: str = "secret-token"
    ) -> None:
        rec = {
            "pid": pid,
            "name": name,
            "sessionId": session_id,
            "status": "idle",
            "messagingSocketPath": socket_path,
        }
        (self.sessions_dir / f"{session_id}.json").write_text(json.dumps(rec), encoding="utf-8")
        # key file format: <pid>.<sha256(canonical(socket_path))>.key
        import hashlib

        h = hashlib.sha256(canonical(socket_path).encode("utf-8")).hexdigest()
        key_data = {"peerToken": token}
        (self.sessions_dir / f"{pid}.{h}.key").write_text(json.dumps(key_data), encoding="utf-8")

    def test_load_records_and_resolve(self) -> None:
        self._create_fake_session(1234, "Orchestrator-A", "sess-uuid-1", "/tmp/sock-a.sock")
        self._create_fake_session(5678, "Orchestrator-B", "sess-uuid-2", "/tmp/sock-b.sock")

        records = load_records()
        self.assertEqual(len(records), 2)

        # Resolve by PID
        self.assertEqual(resolve_target("1234"), "/tmp/sock-a.sock")
        # Resolve by exact name
        self.assertEqual(resolve_target("Orchestrator-A"), "/tmp/sock-a.sock")
        # Resolve case-insensitive
        self.assertEqual(resolve_target("orchestrator-b"), "/tmp/sock-b.sock")
        # Resolve by prefix / substring
        self.assertEqual(resolve_target("sess-uuid-1"), "/tmp/sock-a.sock")
        # Resolve explicit socket
        self.assertEqual(resolve_target("/tmp/custom.sock"), "/tmp/custom.sock")

    def test_key_file_for(self) -> None:
        sock_path = "/tmp/sock-test.sock"
        self._create_fake_session(9999, "TestSession", "sess-test", sock_path, token="tok-9999")
        kf = key_file_for(sock_path)
        self.assertIsNotNone(kf)
        assert kf is not None
        data = json.loads(kf.read_text(encoding="utf-8"))
        self.assertEqual(data["peerToken"], "tok-9999")

    def test_send_session_message_wire_format(self) -> None:
        sock_path = "/tmp/sock-wire.sock"
        self._create_fake_session(4321, "TargetAgent", "sess-wire", sock_path, token="auth-token-123")

        sent_payloads: list[bytes] = []

        def fake_send_bytes(endpoint: str, data: bytes, timeout: float = 5.0) -> int:
            self.assertEqual(endpoint, sock_path)
            sent_payloads.append(data)
            return len(data)

        with patch("coord.ipc.send_bytes", side_effect=fake_send_bytes):
            ok, msg = send_session_message("TargetAgent", "Hello from worker!")
            self.assertTrue(ok)
            self.assertEqual(msg, "ok")

        self.assertEqual(len(sent_payloads), 1)
        wire_lines = sent_payloads[0].decode("utf-8").strip().split("\n")
        self.assertEqual(len(wire_lines), 2)

        auth_frame = json.loads(wire_lines[0])
        self.assertEqual(auth_frame, {"type": "auth", "token": "auth-token-123"})

        body_frame = json.loads(wire_lines[1])
        self.assertEqual(body_frame["type"], "user")
        self.assertEqual(body_frame["msgV"], 1)
        self.assertEqual(body_frame["priority"], "next")
        self.assertEqual(body_frame["message"], {"role": "user", "content": "Hello from worker!"})


class TestManagerIPCDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "coord.db"
        self.store = Store(db_path)
        self.manager = Manager(self.store, backend="test")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_emit_dispatches_ipc_notification(self) -> None:
        dispatched: list[tuple[str, str]] = []

        async def fake_async_send(target: str, text: str, priority: str = "next") -> tuple[bool, str]:
            dispatched.append((target, text))
            return True, "ok"

        with patch("coord.manager.async_send_session_message", side_effect=fake_async_send):
            async def run_scenario() -> None:
                self.manager.emit("OwnerA", "log-line-1", push_message="push-prompt-1")
                # Wait briefly for loop task
                await asyncio.sleep(0.01)

            asyncio.run(run_scenario())

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0], ("OwnerA", "push-prompt-1"))
