"""完整走一遍 CameraWorker._store_image 链路，找出哪步失败。"""
from __future__ import annotations
import sys, asyncio, logging, queue
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from datetime import datetime
from cameratinymes.config import CameraConfig, CellConfig
from cameratinymes.storage import open_storage
from cameratinymes.worker import CameraWorker

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main():
    data_dir = Path.home() / "AppData" / "CameraTinyMES"
    storage = open_storage(data_dir)
    events: queue.Queue = queue.Queue(maxsize=1000)

    cfg = CameraConfig(
        name="DEBUG-123",
        host="192.168.10.123",
        port=80,
        username="admin",
        password="",
        cells=[
            CellConfig(name="A0", data_type="image", record_mode="simple"),
        ],
    )
    worker = CameraWorker(cfg, storage, events, writer=None)  # writer=None → 直接逐张 JPEG
    await worker.start()

    # 跑 15 秒看看
    print("\n=== Worker 运行中（15 秒）===\n")
    for _ in range(15):
        await asyncio.sleep(1)
        # 排空事件队列
        while not events.empty():
            try:
                name, kind, payload = events.get_nowait()
                print(f"  event: [{name}] {kind} → {payload}")
            except Exception:
                break

    await worker.stop()
    storage.close()

    # 查数据库看看有没有新图
    from cameratinymes.storage import SQLiteStorage
    import pymysql
    try:
        conn = pymysql.connect(host="192.168.10.64", port=8306, user="justsmart",
                               password="justsmart", database="camera_tiny_mes",
                               charset="utf8mb4", connect_timeout=3)
        cur = conn.cursor()
        cur.execute("""
            SELECT camera, cell, kind, COUNT(*), 
                   ROUND(AVG(LENGTH(data))/1024, 1),
                   MAX(ts)
            FROM images
            WHERE ts > NOW() - INTERVAL 1 MINUTE
            GROUP BY camera, cell, kind
        """)
        rows = cur.fetchall()
        print(f"\n=== 最近 1 分钟 images 表 ===")
        for r in rows:
            print(f"  {r}")
        cur.execute("SELECT COUNT(*) FROM images WHERE camera='DEBUG-123'")
        print(f"\nDEBUG-123 相机 images 总数: {cur.fetchone()[0]}")
        conn.close()
    except Exception as exc:
        print(f"查 DB 失败: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
