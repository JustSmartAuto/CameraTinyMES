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
from .config import CameraConfig, CellConfig, image_cell
from .images import (cell_is_image, compose_render, fetch_image, fetch_json,
                     fetch_text, image_source_size, resolve_sources, scaled_url,
                     to_jpeg, view_size)
from .pointcloud import (POINTCLOUD_KIND, POINTCLOUD_MIME, cell_is_pointcloud,
                         fetch_pointcloud, parse_cpcd, pointcloud_source)
from .storage import StorageBase

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

    def __init__(self, cfg: CameraConfig, storage: StorageBase, events: queue.Queue,
                 writer: ImageStreamWriter | None = None):
        self.cfg = cfg
        self.storage = storage
        self.writer = writer      # AV1 帧序列写入器；None 时逐张 JPEG 入库
        self.events = events  # UI 侧队列，消息为 (camera, kind, payload)
        self.client = CogSocketClient(cfg.host, cfg.port, cfg.username, cfg.password)
        self.job: str | None = None  # 相机当前作业名（jobx 配方），随采样一起落库
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

    @staticmethod
    def _detect_media(hmi_result: dict,
                      cells: list[CellConfig]) -> tuple[set[str], set[str]]:
        """按单元格自身的值标记识别 (图像单元格, 点云单元格) 集合。"""
        images: set[str] = set()
        pointclouds: set[str] = set()
        for cell in cells:
            if cell_is_pointcloud(hmi_result, cell.name):
                pointclouds.add(cell.name)
            elif cell_is_image(hmi_result, cell.name):
                images.add(cell.name)
        return images, pointclouds

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
        await self._refresh_job()

        # auto 单元格运行时自动识别：依据单元格自身的值是否为图像标记（❒Image）
        # 或点云标记（❒PointCloud），而非 acqImageView——后者是整次拍照的采集视图，
        # 任何单元格的结果都带，用它判断会把所有 auto 单元格误判为图像单元格。
        auto_cells = [c for c in self.cfg.cells if c.data_type == "auto"
                      and not c.is_media]
        auto_image_names: set[str] = set()
        auto_pointcloud_names: set[str] = set()
        if auto_cells:
            try:
                # GET {sid}/result 返回完整 HmiResult（含 cells 各单元格的值）
                first = await self.client.get(f"{sid}/result")
                auto_image_names, auto_pointcloud_names = self._detect_media(
                    first or {}, auto_cells)
                if auto_image_names:
                    log.info("auto 单元格探测为图像单元格: %s（值为图像标记 ❒Image）",
                             ", ".join(sorted(auto_image_names)))
                if auto_pointcloud_names:
                    log.info("auto 单元格探测为点云单元格: %s（值为点云标记 ❒PointCloud）",
                             ", ".join(sorted(auto_pointcloud_names)))
            except Exception as exc:
                log.debug("auto 单元格预探测 GET %s/result 失败: %s", sid, exc)

        # 图像 / 点云随结果事件一起到达：媒体单元格一律按事件采样（轮询会重复下载同一张）
        event_cells = [c for c in self.cfg.cells
                       if c.sample_mode == "event"
                       or c.is_media
                       or c.name in auto_image_names
                       or c.name in auto_pointcloud_names]
        poll_cells = [c for c in self.cfg.cells
                      if c.sample_mode == "poll"
                      and not c.is_media
                      and c.name not in auto_image_names
                      and c.name not in auto_pointcloud_names]

        if event_cells:
            done = asyncio.Event()
            media_names = ({c.name for c in event_cells if c.is_media}
                           | auto_image_names | auto_pointcloud_names)
            # 缓存探测结果，首次 on_result 前可能已在预探测阶段得到
            detected = set(auto_image_names)
            detected_pc = set(auto_pointcloud_names)

            def on_result(hmi_result):
                nonlocal detected, detected_pc
                try:
                    values = parse_cells(hmi_result or {})
                    ts = datetime.now()

                    # 兜底：如果预探测漏了，首次事件时再补一次
                    if (not detected or not detected_pc) and auto_cells:
                        found_img, found_pc = self._detect_media(
                            hmi_result or {}, auto_cells)
                        if found_img - detected:
                            detected |= found_img
                            log.info("auto 单元格事件触发探测为图像单元格: %s",
                                     ", ".join(sorted(found_img)))
                        if found_pc - detected_pc:
                            detected_pc |= found_pc
                            log.info("auto 单元格事件触发探测为点云单元格: %s",
                                     ", ".join(sorted(found_pc)))

                    for cell in event_cells:
                        if cell.is_pointcloud or cell.name in detected_pc:
                            self._store_pointcloud(ts, cell, hmi_result)
                            continue
                        if cell.is_image or cell.role == "image" or cell.name in detected:
                            self._store_image(ts, cell, hmi_result)
                            continue
                        if cell.name in values:
                            try:
                                self._store(ts, cell, values[cell.name])
                            except Exception as exc:
                                log.warning("store event cell %s failed: %s", cell.name, exc)
                    self._push("result", {k: v for k, v in values.items()
                                          if k not in media_names})
                finally:
                    # 必须 ready，相机才会推送下一帧
                    asyncio.ensure_future(self._safe_ready())
                    done.set()

            await self.client.listen(f"{sid}/resultChanged", on_result)
            await self.client.ready()

        for cell in poll_cells:
            self._poll_tasks.append(asyncio.create_task(self._poll_loop(sid, cell)))

        # 操作员随时可能换配方，定期刷新作业名；保持任务存活，由外层重连循环管理生命周期
        job_task = asyncio.create_task(self._job_loop())
        try:
            await asyncio.Future()
        finally:
            job_task.cancel()

    async def _job_loop(self, interval: float = 10.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await self._refresh_job()
        except asyncio.CancelledError:
            pass

    async def _refresh_job(self) -> None:
        """读取当前作业名；换作业时封掉旧配方的 AV1 分片，避免一个分片混两种配方。"""
        try:
            name = await self.client.job_name()
        except Exception as exc:
            log.debug("读取作业名失败 %s: %s", self.cfg.name, exc)
            return
        if not name or name == self.job:
            return
        previous, self.job = self.job, name
        log.info("相机 %s 当前作业：%s", self.cfg.name, name)
        self._push("job", name)
        if previous is not None and self.writer is not None:
            await asyncio.to_thread(self.writer.flush_camera, self.cfg.name)

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
            # 快照判定阈值：仅 advanced + 数值类型才有判定依据
            lower = upper = None
            if not cell.is_simple_record and cell.is_numeric:
                lower, upper = cell.lower, cell.upper
            self.storage.insert_sample(ts, self.cfg.name, cell.name, value, ok,
                                       lower_limit=lower, upper_limit=upper,
                                       job=self.job)
        except Exception as exc:
            log.warning("store cell %s failed: %s", cell.name, exc)

    def _store_pointcloud(self, ts: datetime, cell: CellConfig, hmi_result: dict) -> None:
        """解析点云地址并异步下载落库（CPCD 原样存储，不做转码）。"""
        try:
            source = pointcloud_source(hmi_result)
        except Exception as exc:
            log.warning("resolve pointcloud for %s failed: %s", cell.name, exc)
            return
        if not source:
            log.debug("no pointcloud source for %s", cell.name)
            return
        task = asyncio.ensure_future(self._download_pointcloud(ts, cell, source))
        self._image_tasks.append(task)
        task.add_done_callback(lambda t: self._image_tasks.remove(t)
                               if t in self._image_tasks else None)

    async def _download_pointcloud(self, ts: datetime, cell: CellConfig,
                                   source: dict) -> None:
        host, port = self.client.host, self.client.port
        try:
            data = await asyncio.to_thread(fetch_pointcloud, host, port, source["url"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("download pointcloud for %s failed: %s", cell.name, exc)
            return
        count = parse_cpcd(data)
        try:
            self.storage.insert_image(ts, self.cfg.name, cell.name, POINTCLOUD_KIND,
                                      data, POINTCLOUD_MIME, job=self.job)
        except Exception as exc:
            log.warning("store pointcloud for %s failed: %s", cell.name, exc)
            return
        size = f"{len(data) / 1024 / 1024:.1f} MB"
        summary = f"[点云 {count} 点 {size}]" if count else f"[点云 {size}]"
        try:
            # 同时记一条样本，让点云单元格出现在合表中
            self.storage.insert_sample(ts, self.cfg.name, cell.name, summary, None,
                                       job=self.job)
        except Exception as exc:
            log.warning("store pointcloud sample %s failed: %s", cell.name, exc)
        self._push("image", {cell.name: summary})

    def _store_image(self, ts: datetime, cell: CellConfig, hmi_result: dict) -> None:
        """解析图像地址并异步下载落库（下载不阻塞事件循环与其他相机）。"""
        target = image_cell(self.cfg)
        with_render = target is not None and target.name == cell.name
        try:
            sources = resolve_sources(hmi_result, cell.name, with_render=with_render)
            size = image_source_size(hmi_result)
            viewport = view_size(hmi_result)
        except Exception as exc:
            log.warning("resolve image for %s failed: %s", cell.name, exc)
            return
        if not sources:
            log.debug("no image source for %s", cell.name)
            return
        task = asyncio.ensure_future(
            self._download_images(ts, cell, sources, size, viewport))
        self._image_tasks.append(task)
        task.add_done_callback(lambda t: self._image_tasks.remove(t)
                               if t in self._image_tasks else None)

    async def _download_images(self, ts: datetime, cell: CellConfig,
                               sources: dict[str, str], src_size,
                               viewport=None) -> None:
        host, port = self.client.host, self.client.port
        raw_url = sources.get("image")
        if not raw_url:
            return
        src_w, src_h = src_size if src_size else (None, None)
        # 图像与图形并行下载：视图记录（view id）会被相机快速轮换，串行下载时图形常已过期
        specs = [("image", fetch_image, scaled_url(raw_url, src_w, src_h))]
        if sources.get("graphics"):
            specs.append(("graphics", fetch_json, sources["graphics"]))
        if sources.get("svg"):
            specs.append(("svg", fetch_text, sources["svg"]))
        results = await asyncio.gather(
            *(asyncio.to_thread(fn, host, port, url) for _, fn, url in specs),
            return_exceptions=True)
        got: dict[str, object] = {}
        for (kind, _, _), result in zip(specs, results):
            if isinstance(result, Exception):
                if kind == "image":
                    log.warning("download image for %s failed: %s", cell.name, result)
                    return
                log.warning("download %s for %s failed: %s", kind, cell.name, result)
                continue
            got[kind] = result
        data = got["image"][0]  # fetch_image 返回 (bytes, mime)
        graphics = got.get("graphics")
        svg_text = got.get("svg")

        try:
            raw_jpeg, width, height = await asyncio.to_thread(to_jpeg, data)
            if self.writer is None or not self.writer.add_frame(
                    ts, self.cfg.name, cell.name, "image", raw_jpeg, width, height,
                    job=self.job):
                self.storage.insert_image(ts, self.cfg.name, cell.name, "image",
                                          raw_jpeg, "image/jpeg", job=self.job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("store image for %s failed: %s", cell.name, exc)
            return

        render_ok = False
        if graphics or svg_text:
            try:
                render_jpeg, width, height = await asyncio.to_thread(
                    compose_render, data, graphics, viewport, src_size, svg_text)
                if self.writer is None or not self.writer.add_frame(
                        ts, self.cfg.name, cell.name, "render", render_jpeg, width, height,
                        job=self.job):
                    self.storage.insert_image(ts, self.cfg.name, cell.name, "render",
                                              render_jpeg, "image/jpeg", job=self.job)
                render_ok = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("render image for %s failed: %s", cell.name, exc)

        size = f"{width}×{height}"
        try:
            # 同时记一条样本，让图像单元格出现在合表中
            self.storage.insert_sample(ts, self.cfg.name, cell.name,
                                       f"[图像 {size}]", None, job=self.job)
        except Exception as exc:
            log.warning("store image sample %s failed: %s", cell.name, exc)
        self._push("image", {cell.name: f"图像+渲染图 {size}" if render_ok
                             else f"图像 {size}"})


class Monitor:
    """管理所有相机 Worker 与事件总线。"""

    def __init__(self, storage: StorageBase):
        from .videocodec import BackgroundImageCompressor
        self.storage = storage
        # 实时分片已改为后台批量压缩（BackgroundImageCompressor），
        # worker 直接写 images 表，compressor 攒够 30 帧再统一编码
        self.writer = None
        self.compressor = BackgroundImageCompressor(storage)
        self.events: queue.Queue = queue.Queue(maxsize=1000)
        self.workers: dict[str, CameraWorker] = {}

    async def start_camera(self, cfg: CameraConfig) -> None:
        worker = self.workers.get(cfg.name)
        if worker and worker.running:
            return
        worker = CameraWorker(cfg, self.storage, self.events, self.writer)
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

    def start_compressor(self) -> None:
        """启动后台 AV1 压缩守护线程（在相机启动之后调用）。"""
        self.compressor.start()

    def stop_compressor(self) -> None:
        """停止后台 AV1 压缩守护线程（在相机停止之后调用）。"""
        self.compressor.stop()
