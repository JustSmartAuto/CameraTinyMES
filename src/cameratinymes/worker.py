"""相机采集 Worker：每台相机一个 asyncio 任务，掉线自动重连，互不影响。

事件采样：订阅 resultChanged，从 HmiResult.cells 提取配置单元格并落库，随后 ready。
轮询采样：按固定周期 queryCellResults。
"""

from __future__ import annotations

import asyncio
import logging
import queue
from datetime import datetime

from .cogsocket import CogSocketClient, CogSocketError, parse_cells
from .config import CameraConfig, CellConfig
from .images import (compose_render, fetch_image, fetch_json, image_source_size,
                     resolve_sources, scaled_url, to_jpeg)
from .storage import Storage

log = logging.getLogger(__name__)


def judge(value, cell: CellConfig) -> bool | None:
    """阈值判定：返回 True=OK False=NG None=无阈值或无法判定。

    simple 记录模式：仅记录数据，不做阈值判定，始终返回 None。
    advanced 模式：上下限仅对数值类型（int/float/auto）生效；
    string/bool/other 不做判定；auto 运行时尝试按数值解析。
    """
    if cell.is_simple_record:
        return None
    if not cell.is_numeric:
        return None
    if cell.lower is None and cell.upper is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if cell.lower is not None and v < cell.lower:
        return False
    if cell.upper is not None and v > cell.upper:
        return False
    return True


class CameraWorker:
    """驱动单台相机的连接-订阅-落库循环。"""

    def __init__(self, cfg: CameraConfig, storage: Storage, events: queue.Queue):
        self.cfg = cfg
        self.storage = storage
        self.events = events  # UI 侧队列，消息为 (camera, kind, payload)
        self.client = CogSocketClient(cfg.host, cfg.port, cfg.username, cfg.password)
        self._task: asyncio.Task | None = None
        self._poll_tasks: list[asyncio.Task] = []
        self._image_tasks: list[asyncio.Task] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        for task in self._poll_tasks:
            task.cancel()
        self._poll_tasks.clear()
        for task in self._image_tasks:
            task.cancel()
        self._image_tasks.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.client.close()

    def _push(self, kind: str, payload=None) -> None:
        try:
            self.events.put_nowait((self.cfg.name, kind, payload))
        except queue.Full:
            pass

    async def _run(self) -> None:
        while self._running:
            try:
                await self.client.connect()
                self._push("connected")
                await self._open_and_collect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("camera %s error: %s", self.cfg.name, exc)
                self._push("error", str(exc))
            await self.client.close()
            if self._running:
                self._push("disconnected")
                await asyncio.sleep(self.cfg.reconnect_interval)

    async def _open_and_collect(self) -> None:
        cell_names = [c.name for c in self.cfg.cells]
        if not cell_names:
            return
        sid = await self.client.open_session(cell_names)
        self._push("session", sid)

        # 图像随结果事件一起到达：图像单元格一律按事件采样（poll 会重复下载同一张图）
        event_cells = [c for c in self.cfg.cells if c.sample_mode == "event" or c.is_image]
        poll_cells = [c for c in self.cfg.cells
                      if c.sample_mode == "poll" and not c.is_image]

        if event_cells:
            done = asyncio.Event()
            image_names = {c.name for c in event_cells if c.is_image}

            def on_result(hmi_result):
                try:
                    values = parse_cells(hmi_result or {})
                    ts = datetime.now()
                    for cell in event_cells:
                        if cell.is_image:
                            self._store_image(ts, cell, hmi_result)
                            continue
                        if cell.name in values:
                            try:
                                self._store(ts, cell, values[cell.name])
                            except Exception as exc:
                                log.warning("store event cell %s failed: %s", cell.name, exc)
                    self._push("result", {k: v for k, v in values.items()
                                          if k not in image_names})
                finally:
                    # 必须 ready，相机才会推送下一帧
                    asyncio.ensure_future(self._safe_ready())
                    done.set()

            await self.client.listen(f"{sid}/resultChanged", on_result)
            await self.client.ready()

        for cell in poll_cells:
            self._poll_tasks.append(asyncio.create_task(self._poll_loop(sid, cell)))

        # 保持任务存活，由外层重连循环管理生命周期
        await asyncio.Future()

    async def _safe_ready(self) -> None:
        try:
            await self.client.ready()
        except Exception as exc:
            log.debug("ready failed: %s", exc)

    async def _poll_loop(self, sid: str, cell: CellConfig) -> None:
        try:
            while self._running:
                try:
                    results = await self.client.post(f"{sid}/queryCellResults", [[cell.name]])
                    values = {}
                    for item in results or []:
                        loc = item.get("location") or item.get("name")
                        if loc:
                            values[loc] = item.get("data")
                        name = item.get("name")
                        if name:
                            values[name] = item.get("data")
                    if cell.name in values:
                        self._store(datetime.now(), cell, values[cell.name])
                        self._push("result", {cell.name: values[cell.name]})
                except Exception as exc:
                    log.warning("poll %s iteration failed: %s", cell.name, exc)
                await asyncio.sleep(cell.poll_interval)
        except asyncio.CancelledError:
            pass

    def _store(self, ts: datetime, cell: CellConfig, value) -> None:
        try:
            ok = judge(value, cell)
            self.storage.insert_sample(ts, self.cfg.name, cell.name, value, ok)
        except Exception as exc:
            log.warning("store cell %s failed: %s", cell.name, exc)

    def _store_image(self, ts: datetime, cell: CellConfig, hmi_result: dict) -> None:
        """解析图像地址并异步下载落库（下载不阻塞事件循环与其他相机）。"""
        try:
            sources = resolve_sources(hmi_result, cell.name)
            size = image_source_size(hmi_result)
        except Exception as exc:
            log.warning("resolve image for %s failed: %s", cell.name, exc)
            return
        if not sources:
            log.debug("no image source for %s", cell.name)
            return
        task = asyncio.ensure_future(self._download_images(ts, cell, sources, size))
        self._image_tasks.append(task)
        task.add_done_callback(lambda t: self._image_tasks.remove(t)
                               if t in self._image_tasks else None)

    async def _download_images(self, ts: datetime, cell: CellConfig,
                               sources: dict[str, str], src_size) -> None:
        host, port = self.client.host, self.client.port
        raw_url = sources.get("image")
        if not raw_url:
            return
        src_w, src_h = src_size if src_size else (None, None)
        gfx_url = sources.get("graphics")
        # 图像与图形并行下载：视图记录（view id）会被相机快速轮换，串行下载时图形常已过期
        fetches = [asyncio.to_thread(fetch_image, host, port,
                                     scaled_url(raw_url, src_w, src_h))]
        if gfx_url:
            fetches.append(asyncio.to_thread(fetch_json, host, port, gfx_url))
        results = await asyncio.gather(*fetches, return_exceptions=True)

        data = results[0]
        if isinstance(data, Exception):
            log.warning("download image for %s failed: %s", cell.name, data)
            return
        graphics = None
        if gfx_url:
            graphics = results[1]
            if isinstance(graphics, Exception):
                log.warning("download graphics for %s failed: %s", cell.name, graphics)
                graphics = None
        data = data[0]  # fetch_image 返回 (bytes, mime)

        try:
            raw_jpeg, width, height = await asyncio.to_thread(to_jpeg, data)
            self.storage.insert_image(ts, self.cfg.name, cell.name, "image",
                                      raw_jpeg, "image/jpeg")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("store image for %s failed: %s", cell.name, exc)
            return

        render_ok = False
        if graphics:
            try:
                render_jpeg, width, height = await asyncio.to_thread(
                    compose_render, data, graphics, src_w)
                self.storage.insert_image(ts, self.cfg.name, cell.name, "render",
                                          render_jpeg, "image/jpeg")
                render_ok = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("render image for %s failed: %s", cell.name, exc)

        size = f"{width}×{height}"
        try:
            # 同时记一条样本，让图像单元格出现在合表中
            self.storage.insert_sample(ts, self.cfg.name, cell.name,
                                       f"[图像 {size}]", None)
        except Exception as exc:
            log.warning("store image sample %s failed: %s", cell.name, exc)
        self._push("image", {cell.name: f"图像+渲染图 {size}" if render_ok
                             else f"图像 {size}"})


class Monitor:
    """管理所有相机 Worker 与事件总线。"""

    def __init__(self, storage: Storage):
        self.storage = storage
        self.events: queue.Queue = queue.Queue(maxsize=1000)
        self.workers: dict[str, CameraWorker] = {}

    async def start_camera(self, cfg: CameraConfig) -> None:
        worker = self.workers.get(cfg.name)
        if worker and worker.running:
            return
        worker = CameraWorker(cfg, self.storage, self.events)
        self.workers[cfg.name] = worker
        await worker.start()

    async def stop_camera(self, name: str) -> None:
        worker = self.workers.pop(name, None)
        if worker:
            await worker.stop()
        try:
            self.events.put_nowait((name, "disconnected", None))
        except queue.Full:
            pass

    async def stop_all(self) -> None:
        for name in list(self.workers):
            await self.stop_camera(name)
