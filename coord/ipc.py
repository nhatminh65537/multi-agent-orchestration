"""Claude Code IPC messaging protocol.

Allows sending user/system messages directly into a running Claude Code session
via its local messaging socket (Unix domain socket or Windows named pipe).

Protocol:
  1. Lookup target's messaging socket path (by session name, PID, session ID, or socket path)
     from ~/.claude/sessions/*.json.
  2. Compute canonical endpoint hash: sha256(canonical(endpoint)) -> <pid>.<hash>.key
  3. Read peerToken from ~/.claude/sessions/<pid>.<hash>.key.
  4. Send wire payload:
       authFrame = JSON({"type": "auth", "token": <peerToken>}) + "\\n"
       bodyFrame = JSON({
           "msgV": 1,
           "msg_id": <uuid4>,
           "type": "user",
           "message": {"role": "user", "content": <text>},
           "priority": <priority>,
           ...
       }) + "\\n"
       wire = (authFrame + bodyFrame).encode("utf-8")
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IS_WIN = os.name == "nt"

if IS_WIN:
    import ctypes
    import ctypes.wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = w.HANDLE
    k32.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    k32.WriteFile.argtypes = [
        w.HANDLE,
        w.LPVOID,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
        w.LPVOID,
    ]
    k32.CloseHandle.argtypes = [w.HANDLE]
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def sessions_dir() -> Path:
    """Return path to ~/.claude/sessions (or $CLAUDE_CONFIG_DIR/sessions)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env) / "sessions"
    return Path.home() / ".claude" / "sessions"


def canonical(endpoint: str) -> str:
    """Mimic TO() from Claude Code bundle.

    Windows: \\\\.\\pipe\\LOCAL\\x -> \\\\.\\pipe\\local\\x
    POSIX:   path.resolve(x)
    """
    if not IS_WIN:
        return os.path.abspath(endpoint)
    p = endpoint
    if p.lower().startswith("\\\\?\\pipe\\"):
        p = "\\\\.\\pipe\\" + p[9:]
    return p.lower()


def key_file_for(endpoint: str) -> Path | None:
    """Find the <pid>.<hash>.key file matching the canonical endpoint hash."""
    h = hashlib.sha256(canonical(endpoint).encode("utf-8")).hexdigest()
    d = sessions_dir()
    if not d.is_dir():
        return None
    try:
        for fn in os.listdir(d):
            if fn.endswith(".key"):
                parts = fn.split(".")
                if len(parts) >= 3 and parts[1] == h:
                    return d / fn
    except OSError:
        return None
    return None


def load_records() -> list[dict[str, Any]]:
    """Return all readable Claude Code session records as dicts."""
    d = sessions_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        filenames = os.listdir(d)
    except OSError:
        return []
    for fn in filenames:
        if not fn.endswith(".json"):
            continue
        path = d / fn
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if isinstance(rec, dict):
                out.append(rec)
        except (OSError, ValueError):
            continue
    return out


def resolve_target(arg: str) -> str | None:
    """Resolve an explicit endpoint, PID, session name, or session ID.

    Name matching is **exact** (case-insensitive) on purpose: the old substring
    fallback sent stream A's messages to any session whose name merely contained
    "a" (e.g. "chat", "main").
    """
    a = arg.strip()
    if not a:
        return None

    # Explicit endpoint
    if a.startswith("uds:"):
        return a[4:]
    if IS_WIN and a.startswith("\\\\"):
        return a
    if not IS_WIN and (a.startswith("/") or a.endswith(".sock")):
        return a

    recs = load_records()

    # PID lookup
    if a.isdigit():
        target_pid = int(a)
        for r in recs:
            if r.get("pid") == target_pid:
                return r.get("messagingSocketPath")
        return None

    # Exact session name, or session id (exact or unambiguous prefix)
    low = a.lower()
    hits = [
        r
        for r in recs
        if str(r.get("name", "")).lower() == low
        or str(r.get("sessionId", "")).lower() == low
        or str(r.get("sessionId", "")).lower().startswith(low)
    ]

    if len(hits) == 1:
        return hits[0].get("messagingSocketPath")
    if len(hits) > 1:
        exact = [r for r in hits if str(r.get("name", "")).lower() == low]
        if len(exact) == 1:
            return exact[0].get("messagingSocketPath")
        logger.warning("Ambiguous Claude session %r; %d candidates found", a, len(hits))
        return None
    return None


def send_bytes(endpoint: str, data: bytes, timeout: float = 5.0) -> int:
    """Send raw bytes into the endpoint socket."""
    if not IS_WIN:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect(endpoint)
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
        finally:
            s.close()
        return len(data)

    h = k32.CreateFileW(endpoint, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed", endpoint)
    try:
        n = w.DWORD(0)
        if not k32.WriteFile(h, data, len(data), ctypes.byref(n), None):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        return n.value
    finally:
        k32.CloseHandle(h)


def send_session_message(
    target: str,
    text: str,
    *,
    priority: str = "next",
    own_socket: str | None = None,
) -> tuple[bool, str]:
    """Send text to target Claude Code session by name, PID, or socket path.

    Returns:
        (True, "ok") on success, or (False, error_message) on failure.
    """
    endpoint = resolve_target(target)
    if not endpoint:
        return False, f"No live Claude session matches {target!r}"

    kf = key_file_for(endpoint)
    if not kf or not kf.is_file():
        return False, f"No key file found for endpoint: {endpoint!r}"

    try:
        with open(kf, "r", encoding="utf-8") as f:
            token = json.load(f).get("peerToken")
    except Exception as exc:
        return False, f"Failed to read key file {kf}: {exc}"

    if not token:
        return False, f"Key file {kf} has no peerToken"

    own = own_socket or os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    auth = json.dumps({"type": "auth", "token": token}) + "\n"
    body_dict: dict[str, Any] = {
        "msgV": 1,
        "msg_id": str(uuid.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": text},
        "priority": priority,
    }
    if own:
        body_dict["from"] = own
    body = json.dumps(body_dict) + "\n"
    wire = (auth + body).encode("utf-8")

    try:
        send_bytes(endpoint, wire)
        return True, "ok"
    except Exception as exc:
        return False, f"Failed to send to {endpoint}: {exc}"


async def async_send_session_message(
    target: str,
    text: str,
    *,
    priority: str = "next",
) -> tuple[bool, str]:
    """Async wrapper for non-blocking send_session_message."""
    return await asyncio.to_thread(send_session_message, target, text, priority=priority)
