"""Test running real agy-sdk worker on a separate project directory."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Make sure `coord` is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.config import load_env_file
from coord.manager import Manager
from coord.models import TaskPacket
from coord.store import Store


async def main() -> int:
    # 1. Load the config from the repo's .env
    repo_root = Path(__file__).resolve().parents[1]
    env_file = repo_root / ".env"
    loaded = load_env_file(env_file)
    print(f"[1] Config loaded from: {loaded}")
    print(f"    WORKER_BACKEND: {os.environ.get('WORKER_BACKEND')}")
    print(f"    WORKER_MODEL: {os.environ.get('WORKER_MODEL')}")
    print(f"    WORKER_BASE_URL: {os.environ.get('WORKER_BASE_URL')}")

    # 2. Create a standalone test project in /tmp/test-coord-subproject
    test_proj = Path("/tmp/test-coord-subproject")
    if test_proj.exists():
        shutil.rmtree(test_proj)
    test_proj.mkdir(parents=True)

    subprocess.run(["git", "init"], cwd=str(test_proj), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(test_proj), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(test_proj), check=True)
    (test_proj / "README.md").write_text("# Test Subproject\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(test_proj), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(test_proj), check=True, capture_output=True)
    print(f"[2] Subproject initialised at: {test_proj}")

    # 3. Create an isolated Store & Manager
    test_home = Path("/tmp/test-coord-home")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True)
    os.environ["COORD_HOME"] = str(test_home)

    store = Store(test_home / "coord.db")
    manager = Manager(store, backend="agy-sdk")
    print(f"[3] Manager created with backend: {manager.backend}, model: {manager.model}")

    # 4. Build a TaskPacket and spawn the worker
    task = TaskPacket(
        id="SUB-01",
        goal="Create `hello_worker.txt` containing `SUCCESS_FROM_WORKER`, then answer in prose (state the file you created and its content).",
        acceptance=["cat hello_worker.txt | grep SUCCESS_FROM_WORKER"],
        files=["hello_worker.txt"],
    )

    print(f"[4] Spawning a worker for task: {task.id}...")
    wid = await manager.spawn_worker("tester", str(test_proj), task.model_dump_json())
    print(f"    Worker ID: {wid}")

    # 5. Wait for the worker to finish
    print("[5] Waiting for the worker to finish through the LLM proxy...")
    worker_task = manager.tasks.get(wid)
    if worker_task:
        try:
            await asyncio.wait_for(worker_task, timeout=120)
        except asyncio.TimeoutError:
            print("ERROR: the worker timed out after 120s!")
            return 1

    # 6. Check the result
    worker_row = store.worker(wid)
    print(f"[6] Worker state in the DB: {worker_row['state']}")

    out_file = test_proj / "hello_worker.txt"
    if out_file.is_file():
        content = out_file.read_text(encoding="utf-8").strip()
        print(f"    Content of {out_file.name}: {content!r}")
    else:
        print(f"    WARNING: {out_file.name} was not created!")

    log_file = test_proj / ".coord" / "logs" / "SUB-01.md"
    response_body = ""
    if log_file.is_file():
        response_body = log_file.read_text(encoding="utf-8")
        print(f"    Log file: {log_file} (exists, size={log_file.stat().st_size} bytes)")
        marker = "## response"
        if marker in response_body:
            print("    Tail of the worker answer:")
            print("      " + response_body.split(marker, 1)[1].strip()[:400].replace("\n", "\n      "))

    if worker_row["state"] == "IDLE" and response_body:
        print("\nPASS: the agy-sdk worker ran cleanly on a subproject behind the custom proxy!")
        return 0
    else:
        print("\nFAIL: the worker did not end in the expected DONE state.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
