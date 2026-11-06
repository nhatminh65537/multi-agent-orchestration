"""Test running real agy-sdk worker on a separate project directory."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Đảm bảo coord import được
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.config import load_env_file
from coord.manager import Manager
from coord.models import TaskPacket
from coord.store import Store


async def main() -> int:
    # 1. Load config từ .env của repo cha
    repo_root = Path(__file__).resolve().parents[1]
    env_file = repo_root / ".env"
    loaded = load_env_file(env_file)
    print(f"[1] Đã nạp config từ: {loaded}")
    print(f"    WORKER_BACKEND: {os.environ.get('WORKER_BACKEND')}")
    print(f"    WORKER_MODEL: {os.environ.get('WORKER_MODEL')}")
    print(f"    WORKER_BASE_URL: {os.environ.get('WORKER_BASE_URL')}")

    # 2. Tạo một test project riêng biệt ở /tmp/test-coord-subproject
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
    print(f"[2] Đã khởi tạo subproject tại: {test_proj}")

    # 3. Tạo isolated Store & Manager
    test_home = Path("/tmp/test-coord-home")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True)
    os.environ["COORD_HOME"] = str(test_home)

    store = Store(test_home / "coord.db")
    manager = Manager(store, backend="agy-sdk")
    print(f"[3] Manager khởi tạo thành công với backend: {manager.backend}, model: {manager.model}")

    # 4. Tạo TaskPacket và spawn worker
    task = TaskPacket(
        id="SUB-01",
        goal="Tạo file `hello_worker.txt` với nội dung `SUCCESS_FROM_WORKER`. Sau đó gọi `report_completion` với status='DONE'.",
        acceptance=["cat hello_worker.txt | grep SUCCESS_FROM_WORKER"],
        files=["hello_worker.txt"],
    )

    print(f"[4] Spawning worker cho task: {task.id}...")
    wid = await manager.spawn_worker("tester", str(test_proj), task.model_dump_json())
    print(f"    Worker ID: {wid}")

    # 5. Chờ worker chạy xong
    print("[5] Đang chờ worker hoàn thành task qua proxy LLM...")
    worker_task = manager.tasks.get(wid)
    if worker_task:
        try:
            await asyncio.wait_for(worker_task, timeout=120)
        except asyncio.TimeoutError:
            print("❌ LỖI: Worker timeout sau 120s!")
            return 1

    # 6. Kiểm tra kết quả
    worker_row = store.worker(wid)
    print(f"[6] Trạng thái worker trong DB: {worker_row['state']}")

    reports = store.reports(wid)
    print(f"    Số lượng reports: {len(reports)}")
    if reports:
        rep = reports[-1]
        print(f"    Report status: {rep['status']}")
        payload = json.loads(rep.get("payload") or "{}")
        print(f"    Report summary: {payload.get('summary')}")
        print(f"    Files changed: {payload.get('files_changed')}")

    out_file = test_proj / "hello_worker.txt"
    if out_file.is_file():
        content = out_file.read_text(encoding="utf-8").strip()
        print(f"    Nội dung file {out_file.name}: {content!r}")
    else:
        print(f"    ⚠️ File {out_file.name} chưa được tạo!")

    log_file = test_proj / ".coord" / "logs" / "SUB-01.md"
    if log_file.is_file():
        print(f"    Log file: {log_file} (tồn tại, size={log_file.stat().st_size} bytes)")

    if worker_row["state"] == "IDLE" and reports and reports[-1]["status"] == "DONE":
        print("\n✅ TEST THÀNH CÔNG: Worker agy-sdk chạy mượt mà trên subproject với custom proxy!")
        return 0
    else:
        print("\n❌ TEST THẤT BẠI: Worker không hoàn thành đúng trạng thái DONE.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
