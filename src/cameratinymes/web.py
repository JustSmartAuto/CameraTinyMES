"""改善报告本地 Web 工作台：HTTP 服务、标注/草稿/tag/工单接口、报告生成任务。

设计见 `docs/当日改善报告功能设计.md` §4.1~§4.9、§6.5：
- 标准库 `ThreadingHTTPServer`，监听 `0.0.0.0`（可由 `settings.web_host` 改回仅本机）；
- 所有耗时操作（生成、落盘）只投递 `TaskManager`，HTTP 线程立即返回 `202 {task_id}`；
- 敏感操作（弹日期对话框、打开文件、删除存档）仅回环地址可用；
- 请求体上限 1MB，POST 校验 `Origin`，可选口令 `web_token`；
- 端口被占用时 +1 顺延（最多 20 次），实际端口写入 `settings.json` 与 `reports/.web_port`。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import (AppSettings, CameraConfig, DEFAULT_IMAGE_CELL, ReportConfig,
                     image_cell, result_cell, result_specs, result_tokens,
                     save_settings)
from .okng import hourly_okng, okng_xlsx
from .report import (MAX_IMAGE_SLOTS, CameraStat, ReportOptions,
                     auto_pick, auto_reason, collect_stats, hour_ranges, ranges_iso,
                     station_of)
from .reportstore import (export_report_to_disk, list_reports, load_report_bytes,
                          purge_reports, reports_dir, save_report)
from .storage import ConflictError
from .tasks import CancelledError, TaskManager

log = logging.getLogger(__name__)

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
ASSETS_DIR = (Path(sys._MEIPASS) / "assets" if getattr(sys, "_MEIPASS", None)
              else Path(__file__).resolve().parents[2] / "assets")
APP_ICON = ASSETS_DIR / "app_icon.ico"
MAX_BODY = 1024 * 1024
FRAME_WINDOW = 5.0
DEFAULT_REVIEWER = "产线工程师"
STATUS_TEXT = {"open": "开启中", "in_progress": "进行中", "done": "已完成", "closed": "已关闭"}


# ---------- 工具 ----------

def local_ip() -> str:
    """取本机局域网 IP（UDP 连通默认路由，失败回退主机名解析）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            return ip
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except OSError:
        pass
    return ""


def _is_loopback(host: str) -> bool:
    host = (host or "").replace("::ffff:", "")
    return host in ("127.0.0.1", "::1", "localhost")


def _hours_label(hours: list[int]) -> str:
    hours = sorted(set(int(h) for h in hours))
    if not hours:
        return "未选时段"
    return f"{hours[0]}-{hours[-1]} 时" if len(hours) > 1 else f"{hours[0]} 时"


def default_hours(day: date) -> list[int]:
    """当日默认 0 点到当前小时；历史日期默认全选。"""
    if day == date.today():
        return list(range(datetime.now().hour + 1))
    return list(range(24))


def _camera_note(camera: CameraConfig) -> str:
    cell = result_cell(camera)
    return (cell.note or "").strip() if cell is not None else ""


def _image_cell_name(camera: CameraConfig | None) -> str:
    """相机的报告图片单元格，未配置时回退 A0。"""
    cell = image_cell(camera) if camera is not None else None
    return cell.name if cell is not None else DEFAULT_IMAGE_CELL


def _result_rule(camera: CameraConfig | None):
    """相机的（结果单元格, 通过值, 不通过值），未配置结果单元格时返回 None。"""
    cell = result_cell(camera) if camera is not None else None
    if cell is None:
        return None
    passes, fails = result_tokens(cell)
    return cell, passes, fails


def _parse_hours(values) -> list[int]:
    hours: set[int] = set()

    def walk(value) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
            return
        for part in str(value).replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 23:
                hours.add(int(part))

    for value in values or []:
        walk(value)
    return sorted(hours)


def _ok_pick_list(value) -> list[dict]:
    """OK 图选择归一化为列表（兼容旧的单对象写法）。"""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    picks: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("ts"):
            continue
        kind = item.get("kind") if item.get("kind") in ("image", "render") else ""
        picks.append({"ts": str(item["ts"]), "kind": kind})
    return picks


def _icon_data_uri() -> str:
    """应用图标的 data URI（内嵌进单文件页面）；读取失败时返回空串。"""
    try:
        data = APP_ICON.read_bytes()
    except OSError as exc:
        log.warning("读取应用图标失败：%s", exc)
        return ""
    return "data:image/x-icon;base64," + base64.b64encode(data).decode("ascii")


def singlefile_page() -> bytes:
    """把工作台的 HTML、样式、脚本与应用图标合并为一个单文件页面。

    图标以 data URI 形式写入 `<link rel="icon">`，页面不再依赖任何静态子资源。
    """
    html = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEBUI_DIR / "style.css").read_text(encoding="utf-8")
    js = (WEBUI_DIR / "app.js").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/webui/style.css">',
                        f"<style>\n{css}\n</style>", 1)
    html = html.replace('<script src="/webui/app.js"></script>',
                        f"<script>\n{js}\n</script>", 1)
    favicon = _icon_data_uri()
    if favicon:
        html = html.replace(
            "<title>", f'<link rel="icon" type="image/x-icon" href="{favicon}">\n<title>', 1)
    return html.encode("utf-8")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class _FrameCache:
    """缩略图内存缓存（值不可变，键含 kind 与宽度）。"""

    def __init__(self, limit: int = 200):
        self._data: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._limit = limit

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: tuple, value: bytes) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._limit:
                self._data.popitem(last=False)


# ---------- 服务 ----------

class ReportServer:
    """改善报告工作台服务（桌面端与网页端共享同一份存储与任务队列）。"""

    def __init__(self, storage, cameras: list[CameraConfig], settings: AppSettings,
                 data_dir: Path, cfg: ReportConfig | None = None,
                 tasks: TaskManager | None = None,
                 monitor=None, monitor_app=None):
        from .config import load_report_config

        self.storage = storage
        self.cameras = list(cameras)
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.cfg = cfg or load_report_config(self.data_dir)
        self.tasks = tasks or TaskManager()
        self.on_open_entry = None          # callable(date_str, hours)（桌面端弹对话框）
        self.entry_result: dict | None = None
        self.entry_stamp: str = ""
        self.host = ""
        self.port = 0
        self.token = (settings.web_token or "").strip()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._frames = _FrameCache()

        # ---------- 数据监控 WebSocket 扩展 ----------
        self.monitor = monitor            # Worker 事件总线（多订阅者）
        self.monitor_app = monitor_app    # MonitorApp，用于 find_camera / reload_config / root.after
        self._live_cache: dict[tuple[str, str], list[tuple[str, object]]] = {}
        self._live_lock = threading.Lock()
        # WS 客户端集合：每个是 handler 自己的 connection socket
        self._ws_clients: set = set()
        self._ws_lock = threading.Lock()
        self._ws_monitor_q = None         # Monitor 订阅者 queue
        self._ws_monitor_thread: threading.Thread | None = None
        self._db_monitor_q = None         # db_status_q 订阅者 queue
        self._db_monitor_thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> int:
        host = (self.settings.web_host or "0.0.0.0").strip() or "0.0.0.0"
        base = int(self.settings.web_port or 18110)
        target = reports_dir(self.data_dir)
        for stale in target.glob("*.tmp"):
            try:
                stale.unlink()
            except OSError:
                pass
        httpd = None
        for offset in range(21):
            port = base + offset
            try:
                httpd = _ThreadingServer((host, port), _Handler)
                httpd.report_server = self
                self.port = port
                break
            except OSError as exc:
                log.debug("端口 %s 不可用：%s", port, exc)
        if httpd is None:
            raise OSError(f"报告工作台端口 {base}~{base + 20} 均不可用")
        httpd.daemon_threads = True
        self.host = host
        self._httpd = httpd
        self.tasks.mark_interrupted()
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="report-web", daemon=True)
        self._thread.start()
        (target / ".web_port").write_text(str(self.port), encoding="utf-8")
        if int(self.settings.web_port or 0) != self.port:
            self.settings.web_port = self.port
            try:
                save_settings(self.data_dir, self.settings)
            except OSError as exc:
                log.warning("写回 web_port 失败：%s", exc)
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop,
                                                name="report-purge", daemon=True)
        self._cleanup_thread.start()

        # 启动数据监控 WebSocket 推送线程（仅桌面端有 Monitor 时有效；
        # web_only.py 独立进程 monitor=None，WS 端点降级为 standalone 提示）
        if self.monitor is not None:
            try:
                self._ws_monitor_q = self.monitor.subscribe(maxsize=500)
                self._ws_monitor_thread = threading.Thread(
                    target=self._ws_push_loop, name="report-ws-push", daemon=True)
                self._ws_monitor_thread.start()
                log.info("数据监控 WS 推送线程已启动")
            except Exception as exc:
                log.warning("启动 WS 推送线程失败: %s", exc)
        if self.monitor_app is not None:
            try:
                self._db_monitor_q = self.monitor_app.subscribe_db(maxsize=10)
                self._db_monitor_thread = threading.Thread(
                    target=self._db_push_loop, name="report-db-push", daemon=True)
                self._db_monitor_thread.start()
            except Exception as exc:
                log.warning("启动 DB 推送线程失败: %s", exc)

        log.info("报告工作台已启动：http://%s:%s/report", host, self.port)
        return self.port

    def stop(self) -> None:
        self._cleanup_stop.set()
        # 关闭所有 WS 客户端连接
        with self._ws_lock:
            for sock in list(self._ws_clients):
                try:
                    sock.close()
                except Exception:
                    pass
            self._ws_clients.clear()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as exc:
                log.warning("关闭报告工作台失败：%s", exc)
            self._httpd = None
        self.tasks.shutdown(wait=False)

    def urls(self, day: date | None = None, hours: list[int] | None = None) -> dict:
        day = day or date.today()
        hours = hours if hours is not None else default_hours(day)
        query = f"?date={day.isoformat()}&hours={','.join(str(h) for h in hours)}"
        local = f"http://127.0.0.1:{self.port}/report{query}"
        ip = local_ip()
        lan = f"http://{ip}:{self.port}/report{query}" if ip else ""
        return {"local": local, "lan": lan, "port": self.port, "ip": ip}

    def _cleanup_loop(self) -> None:
        if self._cleanup_stop.wait(60):
            return
        while not self._cleanup_stop.is_set():
            try:
                removed = purge_reports(self.storage,
                                        int(self.cfg.report_keep_days or 0),
                                        int(self.cfg.report_keep_count or 0))
                if removed:
                    log.info("报告存档清理：删除 %s 份", removed)
            except Exception as exc:
                log.warning("报告存档清理失败：%s", exc)
            if self._cleanup_stop.wait(24 * 3600):
                return

    # ---------- 数据监控 WebSocket ----------

    def _ws_push_loop(self) -> None:
        """100ms poll Monitor 事件 queue → 更新 LiveCache → 广播所有 WS clients。"""
        assert self._ws_monitor_q is not None
        while not self._cleanup_stop.is_set():
            try:
                while True:
                    camera, kind, payload = self._ws_monitor_q.get_nowait()
                    self._update_live_cache(camera, kind, payload)
                    self._ws_broadcast_sync(json.dumps({
                        "t": time.time(), "k": kind, "c": camera, "p": payload,
                    }, ensure_ascii=False))
            except queue.Empty:
                pass
            # 节流：无新事件时短暂 sleep，避免 100% CPU
            self._cleanup_stop.wait(0.1)

    def _db_push_loop(self) -> None:
        """5s poll db_status_q → 广播 DB 健康给所有 WS clients。"""
        assert self._db_monitor_q is not None
        while not self._cleanup_stop.is_set():
            try:
                ok, size = self._db_monitor_q.get_nowait()
                msg = json.dumps({
                    "t": time.time(), "k": "db_status",
                    "ok": bool(ok),
                    "size_mb": float(size) if size is not None else None,
                    "backend": self.storage.backend,
                }, ensure_ascii=False)
                self._ws_broadcast_sync(msg)
            except queue.Empty:
                pass
            if self._cleanup_stop.wait(0.5):
                return

    def _update_live_cache(self, camera: str, kind: str, payload) -> None:
        """把最新实时值 / 状态塞进 LiveCache；按相机×单元格键 LRU 200 条。"""
        if kind == "result" and isinstance(payload, dict):
            with self._live_lock:
                for cell, value in payload.items():
                    key = (camera, cell)
                    lst = self._live_cache.get(key)
                    if lst is None:
                        lst = []
                        self._live_cache[key] = lst
                    lst.append((time.time(), value))
                    if len(lst) > 200:
                        del lst[:len(lst) - 200]
        # connected/disconnected/job 事件也记一条状态，方便新客上线快照
        elif kind in ("connected", "disconnected", "error", "job"):
            with self._live_lock:
                # 用 (kind, value) 格式区分"数据值"与"状态事件"
                lst = self._live_cache.setdefault((camera, f"__status__"), [])
                lst.append((time.time(), (kind, payload)))
                if len(lst) > 200:
                    del lst[:len(lst) - 200]

    def _ws_broadcast_sync(self, message: str) -> None:
        """同步广播到所有已连接 WS 客户端；客户端自身断连由 handler 清理。"""
        data = message.encode("utf-8")
        frame = self._ws_text_frame(data)
        with self._ws_lock:
            clients = list(self._ws_clients)
        for sock in clients:
            try:
                sock.sendall(frame)
            except (BrokenPipeError, OSError):
                with self._ws_lock:
                    self._ws_clients.discard(sock)
                    try:
                        sock.close()
                    except Exception:
                        pass

    @staticmethod
    def _ws_text_frame(data: bytes) -> bytes:
        """构建一个无掩码的 WebSocket 文本帧（服务端→客户端可不掩码）。"""
        n = len(data)
        if n < 126:
            return b"\x81" + bytes([n]) + data
        if n < 1 << 16:
            return b"\x81" + bytes([126]) + struct.pack(">H", n) + data
        return b"\x81" + bytes([127]) + struct.pack(">Q", n) + data

    def _ws_snapshot_history(self) -> dict:
        """给新上线 WS 客户端推的快照（每相机每单元格最近 200 点）。"""
        snapshot: dict[str, dict[str, list]] = {}
        with self._live_lock:
            for (camera, cell), lst in self._live_cache.items():
                snapshot.setdefault(camera, {})[cell] = [
                    [ts, val] for ts, val in lst
                ]
        return {"t": time.time(), "k": "history", "p": snapshot}

    # ---------- 生成任务 ----------

    def submit_generate(self, params: dict) -> object:
        day = params["date"]
        hours = params["hours"]
        dedupe = f"generate:{day}:{','.join(str(h) for h in hours)}"
        existing = self.tasks.find_active(dedupe)
        if existing is not None:
            raise ApiError(409, "相同参数的生成任务正在运行", task_id=existing.task_id)
        title = f"生成 {day} 报告（{_hours_label(hours)}）"
        return self.tasks.submit("generate", title,
                                 lambda task: self._run_generate(params, task),
                                 dedupe_key=dedupe)

    def submit_export(self, report_id: int) -> object:
        report = self.storage.get_report(report_id)
        if report is None:
            raise ApiError(404, f"报告 #{report_id} 不存在")
        title = f"重新落盘 #{report_id}"
        return self.tasks.submit("export", title,
                                 lambda task: self._run_export(report_id, task),
                                 dedupe_key=f"export:{report_id}")

    def _run_export(self, report_id: int, task) -> dict:
        try:
            xlsx, pdf = export_report_to_disk(
                self.storage, report_id, self.data_dir,
                progress=task.progress, cancel=task.cancel_event.is_set)
        except CancelledError:
            return {"partial": True, "report_id": report_id,
                    "message": f"落盘已取消，库中存档 #{report_id} 保留"}
        except Exception as exc:
            return {"partial": True, "report_id": report_id,
                    "message": f"落盘失败：{exc}", "error": str(exc)}
        return {"report_id": report_id,
                "xlsx_path": str(xlsx) if xlsx else "",
                "pdf_path": str(pdf) if pdf else "",
                "message": "已重新落盘"}

    def _run_generate(self, params: dict, task) -> dict:
        day = date.fromisoformat(params["date"])
        hours = params["hours"]
        overlay = params.get("overlay_annotations")
        if overlay is None:
            overlay = self.cfg.overlay_annotations
        ok_images: dict[str, list[dict]] = {}
        for camera, sel in (params.get("ok_images") or {}).items():
            picks = _ok_pick_list(sel)
            if picks:
                ok_images[str(camera)] = picks
        options = ReportOptions(
            day=day, hours=hours,
            reasons=params.get("reasons") or {},
            other_ng=params.get("other_ng") or {},
            picked_images=params.get("picked_images") or {},
            ok_images=ok_images,
            show_physical=bool(params.get("show_physical")),
            overlay_annotations=bool(overlay),
            tickets={str(k): [int(v) for v in vals]
                     for k, vals in (params.get("tickets") or {}).items()},
        )
        reviewer = (params.get("reviewer") or self.cfg.reviewer
                    or DEFAULT_REVIEWER).strip()

        ticket_list: list[dict] = []
        links: list[tuple[int, str]] = []
        for station, ids in options.tickets.items():
            for ticket_id in ids:
                ticket = self.storage.get_ticket(int(ticket_id), with_events=False,
                                                 with_reports=False)
                if ticket:
                    ticket_list.append(ticket)
                    links.append((int(ticket_id), station))
        if ticket_list:
            options.reasons = self._append_measures(dict(options.reasons), options.tickets)

        task.progress("stats", 0, 1, "统计结果数据")
        artifact = build_daily_report_safe(
            self, options, params, reviewer, ticket_list, task)
        task.check()
        report_id = save_report(self.storage, artifact, options, created_by=reviewer,
                                links=links)
        task.progress("store", 1, 1, f"已存档 #{report_id}")
        task.check()
        sizes = {"xlsx": len(artifact.xlsx_bytes), "pdf": len(artifact.pdf_bytes)}
        try:
            xlsx, pdf = export_report_to_disk(
                self.storage, report_id, self.data_dir,
                progress=task.progress, cancel=task.cancel_event.is_set)
        except CancelledError:
            return {"partial": True, "report_id": report_id, "sizes": sizes,
                    "warnings": artifact.warnings,
                    "message": f"已存档 #{report_id}，落盘已取消，可重试落盘"}
        except Exception as exc:
            log.exception("报告 #%s 落盘失败", report_id)
            return {"partial": True, "report_id": report_id, "sizes": sizes,
                    "warnings": artifact.warnings, "error": str(exc),
                    "message": f"已存档 #{report_id}，落盘失败：{exc}"}
        return {"report_id": report_id,
                "xlsx_path": str(xlsx) if xlsx else "",
                "pdf_path": str(pdf) if pdf else "",
                "sizes": sizes, "warnings": artifact.warnings,
                "message": "报告已存档并落盘"}

    def _append_measures(self, reasons: dict[str, str],
                         tickets: dict[str, list[int]]) -> dict[str, str]:
        """把未闭环工单的改善措施追加到对应相机的原因文本末尾（§4.8）。"""
        for camera in self.cameras:
            station, _ = station_of(camera)
            parts = []
            for ticket_id in tickets.get(station, [])[:5]:
                ticket = self.storage.get_ticket(int(ticket_id), with_events=False,
                                                 with_reports=False)
                if not ticket or ticket.get("status") not in ("open", "in_progress"):
                    continue
                text = f"#{ticket['id']} {ticket.get('title') or ''}".strip()
                extra = [STATUS_TEXT.get(ticket.get("status"), "")]
                if ticket.get("assignee"):
                    extra.append(f"责任人：{ticket['assignee']}")
                if ticket.get("due_date"):
                    extra.append(f"计划 {str(ticket['due_date'])[5:10]}")
                parts.append(f"{text}（{'，'.join(x for x in extra if x)}）")
            if parts:
                base = reasons.get(camera.name, "").strip()
                line = "改善措施：" + "；".join(parts)
                reasons[camera.name] = f"{base}\n{line}" if base else line
        return reasons

    # ---------- 取图 ----------

    def camera_config(self, name: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.name == name), None)

    def image_cell_name(self, camera: str) -> str:
        """该相机的报告图片单元格名，未配置时回退 A0。"""
        return _image_cell_name(self.camera_config(camera))

    def frame_image(self, camera: str, ts: str, width: int,
                    kind: str | None = None) -> tuple[bytes, str] | None:
        """取该事件时刻最近一帧（默认渲染图优先），返回 (JPEG, kind)。

        指定 `kind`（image / render）时只取该类型、且帧时间戳必须与 `ts` 完全一致，
        供图库按原图/渲染图精确浏览。
        """
        try:
            center = datetime.fromisoformat(ts)
        except ValueError:
            return None
        key = (camera, ts, int(width), kind or "")
        cached = self._frames.get(key)
        if cached is not None:
            return cached, "cache"
        try:
            from .videocodec import load_images

            rows = load_images(self.storage, camera,
                               _image_cell_name(self.camera_config(camera)),
                               start=center - timedelta(seconds=FRAME_WINDOW),
                               end=center + timedelta(seconds=FRAME_WINDOW))
        except Exception as exc:
            log.warning("读取 %s 图像失败：%s", camera, exc)
            return None
        best: dict[str, tuple[float, str, bytes]] = {}
        for frame_ts, frame_kind, _mime, _w, _h, data in rows:
            if kind:
                if frame_kind != kind or str(frame_ts) != ts:
                    continue
            try:
                gap = abs((datetime.fromisoformat(str(frame_ts)) - center).total_seconds())
            except ValueError:
                continue
            if gap > FRAME_WINDOW:
                continue
            bucket = "render" if frame_kind == "render" else "image"
            if bucket not in best or gap < best[bucket][0]:
                best[bucket] = (gap, str(frame_ts), data)
        chosen = best.get(kind or "render") or best.get("image")
        if chosen is None:
            return None
        try:
            from . import images as image_mod

            jpeg = image_mod.to_jpeg(chosen[2], max_width=max(32, min(1600, width)))[0]
        except Exception as exc:
            log.warning("缩图失败 %s@%s：%s", camera, ts, exc)
            return None
        self._frames.put(key, jpeg)
        return jpeg, ("render" if "render" in best else "image")


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """浏览器关闭连接属于正常现象，不打印堆栈。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def build_daily_report_safe(server: ReportServer, options: ReportOptions, params: dict,
                            reviewer: str, tickets: list[dict], task):
    """按任务取消/进度调用 report.build_daily_report。"""
    from .report import build_daily_report

    return build_daily_report(
        server.storage, server.cameras, options, server.cfg,
        project_name=params.get("project_name") or server.settings.project_name,
        destination=params.get("destination") or server.settings.destination,
        line_name=params.get("line_name") or server.settings.line_name,
        created_by=reviewer, db_backend=getattr(server.storage, "backend", ""),
        tickets=tickets, include_ticket_sheet=True,
        progress=task.progress, cancel=task.cancel_event.is_set)


# ---------- HTTP 处理 ----------

class _Handler(BaseHTTPRequestHandler):
    server_version = "CameraTinyMES-Report/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> ReportServer:
        return self.server.report_server

    def log_message(self, fmt: str, *args) -> None:      # 避免刷屏
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- 分发 ----------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        # WebSocket Upgrade 拦截（不走 HTTP 路由）
        if self.headers.get("Upgrade", "").lower() == "websocket":
            try:
                self._token_ok(query) or self._token_ok({})
            except Exception:
                pass  # WS 握手 token 校验在 _handle_websocket 里单独做
            if path == "/ws":
                self._handle_websocket(query)
                return
        try:
            if not self._token_ok(query):
                if path.startswith("/api/"):
                    raise ApiError(401, "需要访问口令")
                self._login_page()
                return
            self._check_origin(method)
            if method == "GET":
                self._route_get(path, query)
            elif method == "POST":
                self._route_post(path, query)
            elif method == "PATCH":
                self._route_patch(path, query)
            else:
                self._route_delete(path, query)
        except ApiError as exc:
            self._json({"ok": False, "error": exc.message, **exc.extra}, exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:
            log.exception("处理 %s %s 失败", method, path)
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------- WebSocket ----------

    def _handle_websocket(self, query: dict) -> None:
        """/ws 端点：RFC 6455 握手 + 帧循环（客户端→服务端仅需响应 close/ping）。

        只读服务端→客户端（数据事件 / DB 状态 / history 快照），
        客户端发 ping/close 时简单响应；无掩码帧校验（浏览器客户端一律掩码，
        但我们只需正确解帧即可，不必严格校验掩码）。
        """
        app = self.app
        # token 校验
        if app.token and not self._token_ok(query):
            self._send(401, b"WS auth required", "text/plain")
            return

        # Standalone 降级：没有 Monitor 只能推 history
        standalone = app.monitor is None

        # WebSocket 握手
        ws_key = self.headers.get("Sec-WebSocket-Key", "")
        if not ws_key:
            self._send(400, b"missing Sec-WebSocket-Key", "text/plain")
            return
        accept = base64.b64encode(hashlib.sha1(
            (ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        conn = self.connection
        # 注册到 ReportServer 客户端集合（broadcast loop 自动下发事件）
        with app._ws_lock:
            app._ws_clients.add(conn)
        log.info("WS client connected (standalone=%s) from %s",
                 standalone, self.client_address[0])

        # 立即推一份 history 快照（让新客上线即有数据可看）
        try:
            snapshot = json.dumps(app._ws_snapshot_history(), ensure_ascii=False)
            conn.sendall(app._ws_text_frame(snapshot.encode("utf-8")))
            if standalone:
                app._ws_broadcast_sync(json.dumps({
                    "t": time.time(), "k": "standalone",
                    "msg": "独立进程启动，仅提供历史查询，无实时事件",
                }, ensure_ascii=False))
        except Exception:
            pass

        # 帧读循环：只处理客户端发来的 close/ping；text/data 忽略
        buf = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 2:
                    opcode = buf[0] & 0x0F
                    masked = (buf[1] & 0x80) != 0
                    payload_len = buf[1] & 0x7F
                    idx = 2
                    if payload_len == 126:
                        if len(buf) < idx + 2:
                            break
                        payload_len = struct.unpack(">H", buf[idx:idx + 2])[0]
                        idx += 2
                    elif payload_len == 127:
                        if len(buf) < idx + 8:
                            break
                        payload_len = struct.unpack(">Q", buf[idx:idx + 8])[0]
                        idx += 8
                    if masked:
                        if len(buf) < idx + 4:
                            break
                        mask = buf[idx:idx + 4]
                        idx += 4
                    else:
                        mask = None
                    if len(buf) < idx + payload_len:
                        break
                    if mask:
                        payload = bytes(b ^ mask[i % 4]
                                         for i, b in enumerate(buf[idx:idx + payload_len]))
                    else:
                        payload = buf[idx:idx + payload_len]
                    buf = buf[idx + payload_len:]

                    if opcode == 0x8:  # close
                        try:
                            # 回一个 close
                            conn.sendall(app._ws_text_frame(b""))
                        except Exception:
                            pass
                        return
                    elif opcode == 0x9:  # ping → pong
                        frame = bytearray()
                        frame.append(0x8A)
                        if len(payload) < 126:
                            frame.append(len(payload))
                        else:
                            frame.append(126)
                            frame += struct.pack(">H", len(payload))
                        try:
                            conn.sendall(bytes(frame) + payload)
                        except Exception:
                            return
                    # text/binary 数据帧忽略即可
        except Exception as exc:
            log.debug("WS frame loop %s error: %s", self.client_address[0], exc)
        finally:
            with app._ws_lock:
                app._ws_clients.discard(conn)
                try:
                    conn.close()
                except Exception:
                    pass
            log.info("WS client disconnected from %s", self.client_address[0])

    # ---------- 安全 ----------

    def _token_ok(self, query: dict) -> bool:
        token = self.app.token
        if not token:
            return True
        if (query.get("k") or [""])[0] == token:
            self._set_cookie = True
            return True
        if self.headers.get("X-CTM-Token") == token:
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ctm_token" and value == token:
                return True
        return False

    def _check_origin(self, method: str) -> None:
        if method == "GET":
            return
        origin = self.headers.get("Origin")
        if not origin:
            if self.app.token and self.headers.get("X-CTM-Token") != self.app.token:
                raise ApiError(403, "缺少 Origin，且未携带访问口令")
            return
        host = self.headers.get("Host") or f"127.0.0.1:{self.app.port}"
        allowed = {f"http://{host}", f"https://{host}",
                   f"http://127.0.0.1:{self.app.port}",
                   f"http://localhost:{self.app.port}"}
        if origin.rstrip("/") not in allowed:
            raise ApiError(403, "来源不被允许")

def _require_local(self) -> None:
    """敏感操作（相机配置/连接状态/导出/删除数据）的权限兜底。

    原规则：仅允许 127.0.0.1/localhost 回环地址 → 已改为不强制。
    原因：网页端定位就是多端操作入口（桌面 Tkinter + 局域网其他工控机
    浏览器都要能触发相机级操作），把本机关闭掉会让远程合法请求也 403。
    真正的鉴权已由上层 Origin + Web Token 提供，此处留空仅作为语义占位，
    将来如需再收紧可在此加具体条件。
    """
    return

    # ---------- 响应 ----------

    def _send(self, status: int, body: bytes, ctype: str,
              headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "_set_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"ctm_token={quote(self.app.token)}; Path=/; Max-Age=2592000; SameSite=Lax")
            self._set_cookie = False
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _page(self, name: str, status: int = 200) -> None:
        path = WEBUI_DIR / name
        if not path.exists():
            raise ApiError(404, "页面资源缺失")
        body = singlefile_page() if name == "index.html" else path.read_bytes()
        self._send(status, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _static(self, name: str) -> None:
        name = name.strip("/")
        if not name or ".." in name or "\\" in name:
            raise ApiError(404, "资源不存在")
        path = WEBUI_DIR / name
        if not path.is_file():
            raise ApiError(404, "资源不存在")
        suffix = path.suffix.lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon"}.get(suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), ctype, {"Cache-Control": "no-store"})

    def _login_page(self) -> None:
        html = ("<!DOCTYPE html><meta charset='utf-8'><title>访问口令</title>"
                "<body style='font-family:system-ui;padding:40px;background:#2a5556;color:#fff'>"
                "<h3>请输入访问口令</h3><input id='k' type='password' autofocus> "
                "<button onclick=\"document.cookie='ctm_token='+encodeURIComponent("
                "document.getElementById('k').value)+';path=/';location.reload()\">进入</button>"
                "</body>")
        self._send(401, html.encode("utf-8"), "text/html; charset=utf-8")

    # ---------- 请求体 ----------

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "请求体超过 1MB")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    # ---------- 参数 ----------

    def _day(self, query: dict) -> date:
        text = (query.get("date") or [""])[0].strip()
        if not text:
            return date.today()
        try:
            day = date.fromisoformat(text)
        except ValueError:
            raise ApiError(400, "日期格式应为 YYYY-MM-DD")
        if day > date.today():
            raise ApiError(400, "不能选择未来日期")
        return day

    def _hours(self, query: dict, day: date) -> list[int]:
        hours = _parse_hours(query.get("hours") or [])
        return hours or default_hours(day)

    # ---------- GET ----------

    def _route_get(self, path: str, query: dict) -> None:
        if path in ("/", "/report", "/report/"):
            self._page("index.html")
            return
        if path.startswith("/tickets/") and not path.startswith("/api/"):
            self._page("index.html")
            return
        if path.startswith("/webui/"):
            self._static(path[len("/webui/"):])
            return
        if path == "/favicon.ico":
            try:
                body = APP_ICON.read_bytes()
            except OSError:
                raise ApiError(404, "图标不存在")
            self._send(200, body, "image/x-icon", {"Cache-Control": "max-age=86400"})
            return
        if path == "/api/dates":
            self._api_dates()
        elif path == "/api/gallery":
            self._api_gallery(query)
        elif path == "/api/okng":
            self._api_okng(query)
        elif path == "/api/config":
            self._api_config()
        elif path == "/api/monitor/status":
            self._api_monitor_status()
        elif path == "/api/monitor/cameras":
            self._api_monitor_cameras()
        elif path.startswith("/api/monitor/cameras/"):
            self._api_monitor_camera_routes(path, query)
        elif path == "/api/summary":
            self._api_summary(query)
        elif path.startswith("/api/station/"):
            self._api_station(unquote(path[len("/api/station/"):]), query)
        elif path.startswith("/api/image/"):
            self._api_image(path[len("/api/image/"):], query)
        elif path.startswith("/api/annotation/"):
            self._api_annotation_get(path[len("/api/annotation/"):])
        elif path == "/api/tags":
            self._api_tags(query)
        elif path == "/api/tickets":
            self._api_tickets(query)
        elif path.startswith("/api/tickets/"):
            self._api_ticket(int(path.rsplit("/", 1)[-1]))
        elif path == "/api/draft":
            self._api_draft_get(query)
        elif path == "/api/entry_result":
            self._json({"result": self.app.entry_result,
                        "stamp": self.app.entry_stamp})
        elif path == "/api/tasks":
            self._api_tasks(query)
        elif path.startswith("/api/tasks/"):
            task = self.app.tasks.get(path.rsplit("/", 1)[-1])
            if task is None:
                raise ApiError(404, "任务不存在")
            self._json(task.as_dict())
        elif path == "/api/reports":
            self._api_reports(query)
        elif path.startswith("/api/reports/") and path.endswith("/file"):
            self._api_report_file(path.split("/")[3], query)
        else:
            raise ApiError(404, "接口不存在")

    def _api_dates(self) -> None:
        store = self.app.storage
        today = date.today().isoformat()
        cells = [cell.name for _camera, cell, _p, _f in result_specs(self.app.cameras)]
        dates = [d for d in store.days_with_results_specs(cells, limit=365) if d <= today]
        if today not in dates:
            dates.insert(0, today)
        self._json({"dates": dates, "today": today,
                    "earliest": dates[-1] if dates else today,
                    "draft_days": store.list_draft_days(60)})

    def _api_okng(self, query: dict) -> None:
        day = self._day(query)
        cameras = hourly_okng(self.app.storage, day.isoformat(), self.app.cameras)
        if (query.get("format") or [""])[0].lower() == "xlsx":
            data, filename = okng_xlsx(day.isoformat(), cameras)
            self._send(
                200, data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                {"Content-Disposition":
                 "attachment; filename=\"okng.xlsx\"; filename*=UTF-8''%s" % quote(filename)})
            return
        self._json({"date": day.isoformat(), "cameras": cameras})

    def _api_config(self) -> None:
        app = self.app
        cameras = []
        groups: dict[str, dict] = {}
        for camera in app.cameras:
            if not camera.enabled:
                continue
            station, station_name = station_of(camera)
            cameras.append({"name": camera.name, "station": station,
                            "station_name": station_name, "model": camera.model,
                            "note": _camera_note(camera)})
            group = groups.setdefault(station, {"station": station,
                                                "station_name": station_name,
                                                "cameras": []})
            group["cameras"].append(camera.name)
        self._json({
            "project_name": app.settings.project_name,
            "destination": app.settings.destination,
            "line_name": app.settings.line_name,
            "reviewer": app.cfg.reviewer or DEFAULT_REVIEWER,
            "ng_images_per_station": max(1, min(MAX_IMAGE_SLOTS,
                                                int(app.cfg.ng_images_per_station))),
            "image_max_edge": int(app.cfg.image_max_edge),
            "overlay_annotations": bool(app.cfg.overlay_annotations),
            "cameras": cameras,
            "stations": list(groups.values()),
            "today": date.today().isoformat(),
            "web_host": app.host, "web_port": app.port,
            "token_required": bool(app.token),
            "urls": app.urls(),
        })

    # ---------- 数据监控（GET） ----------

    def _api_monitor_status(self) -> None:
        """数据库健康 + AV1 压缩 + WS 客户端数 + 相机在线数（来自 LiveCache / Monitor）。"""
        app = self.app
        db_ok = app.storage.health_check()
        db_size = app.storage.database_size_mb() if db_ok else None
        if app.monitor is None:
            self._json({
                "mode": "standalone",
                "db": {"ok": db_ok, "size_mb": db_size,
                       "backend": app.storage.backend},
                "av1": {"enabled": False, "runs": 0, "frames": 0,
                        "orig_mb": 0.0, "new_mb": 0.0, "ratio": 0.0,
                        "saved_mb": 0.0},
                "cameras_total": len(app.cameras),
                "cameras_online": None,
                "ws_clients": len(app._ws_clients),
                "token_required": bool(app.token),
            })
            return

        compressor = app.monitor.compressor
        stats = compressor.stats if compressor else {}
        runs = stats.get("runs", 0)
        online = 0
        with app._live_lock:
            for (cam, _), lst in app._live_cache.items():
                if cam.startswith("__"):
                    continue
                for _ts, val in lst:
                    if isinstance(val, tuple) and val[0] == "connected":
                        online += 1
                        break

        self._json({
            "mode": "live",
            "db": {"ok": db_ok, "size_mb": db_size,
                   "backend": app.storage.backend},
            "av1": {
                "enabled": bool(compressor and compressor.enabled),
                "runs": runs,
                "frames": stats.get("frames", 0),
                "orig_mb": stats.get("orig_bytes", 0.0) / 1024 / 1024,
                "new_mb": stats.get("new_bytes", 0.0) / 1024 / 1024,
                "ratio": ((stats.get("orig_bytes", 0.0)
                          / stats.get("new_bytes", 1.0))
                          if stats.get("new_bytes", 0) else 0.0),
                "saved_mb": stats.get("saved_bytes", 0.0) / 1024 / 1024,
            },
            "cameras_total": len(app.cameras),
            "cameras_online": online,
            "ws_clients": len(app._ws_clients),
            "token_required": bool(app.token),
            "has_monitor_app": app.monitor_app is not None,
        })

    def _api_monitor_cameras(self) -> None:
        """完整相机配置列表（含 cells 列表）。"""
        cameras = []
        for cam in self.app.cameras:
            cameras.append(self._monitor_camera_dict(cam))
        self._json({"cameras": cameras,
                    "has_monitor": self.app.monitor is not None})

    def _monitor_camera_dict(self, cam: CameraConfig) -> dict:
        """把 CameraConfig 序列化成 dict（带 cells，Web 前后端都用同一份）。"""
        return {
            "name": cam.name, "host": cam.host, "port": cam.port,
            "model": cam.model, "enabled": cam.enabled,
            "username": cam.username,
            "reconnect_interval": cam.reconnect_interval,
            "cells": [self._monitor_cell_dict(c) for c in cam.cells],
        }

    @staticmethod
    def _monitor_cell_dict(cell) -> dict:
        """把 CellConfig 序列化成 dict（Web 前端编辑后直接 POST/PUT 回来）。"""
        return {
            "name": cell.name,
            "data_type": cell.data_type,
            "record_mode": cell.record_mode,
            "sample_mode": cell.sample_mode,
            "poll_interval": cell.poll_interval,
            "lower": cell.lower,
            "upper": cell.upper,
            "unit": cell.unit,
            "note": cell.note,
            "role": cell.role,
            "pass_values": list(cell.pass_values),
            "fail_values": list(cell.fail_values),
        }

    def _api_monitor_camera_routes(self, path: str, query: dict) -> None:
        """/api/monitor/cameras/<name>[/cells/<cell>/values]"""
        rest = path[len("/api/monitor/cameras/"):]
        parts = rest.split("/")
        name = parts[0]
        if len(parts) == 1:
            cam = next((c for c in self.app.cameras if c.name == name), None)
            if cam is None:
                raise ApiError(404, f"未知相机：{name}")
            # 附加 LiveCache 状态
            result = self._monitor_camera_dict(cam)
            with self.app._live_lock:
                status_key = (name, "__status__")
                live = self.app._live_cache.get(status_key, [])
                result["live_status"] = live[-1][1] if live else None
                # 最新值快照：每个单元格最近一个值
                snap = {}
                for (cn, cl), lst in self.app._live_cache.items():
                    if cn == name and cl != "__status__" and lst:
                        snap[cl] = lst[-1][1]
                result["live_values"] = snap
            self._json(result)
        elif len(parts) == 4 and parts[1] == "cells" and parts[3] == "values":
            cell_name = unquote(parts[2])
            cam = next((c for c in self.app.cameras if c.name == name), None)
            if cam is None:
                raise ApiError(404, f"未知相机：{name}")
            cell_cfg = next((c for c in cam.cells if c.name == cell_name), None)
            if cell_cfg is None:
                raise ApiError(404, f"未知单元格：{cell_name}")
            from_vals = query.get("from")
            to_vals = query.get("to")
            limit = min(5000, max(50, int((query.get("limit") or ["5000"])[0])))
            ts_from = from_vals[0] if from_vals else None
            ts_to = to_vals[0] if to_vals else None
            points = self.app.storage.range_cell_values(
                name, cell_name, ts_from=ts_from, ts_to=ts_to, limit=limit)
            self._json({
                "camera": name, "cell": cell_name,
                "from": ts_from, "to": ts_to, "limit": limit,
                "points": points,
                "lower_limit": cell_cfg.lower, "upper_limit": cell_cfg.upper,
                "sample_mode": cell_cfg.sample_mode,
            })
        else:
            raise ApiError(404, "接口不存在")

    # ---------- 数据监控（POST：连接/断开/导出/删除） ----------

    def _require_monitor_app(self) -> None:
        if self.app.monitor_app is None:
            raise ApiError(422, "独立进程模式，无法远程控制相机；请在桌面端操作")

    def _api_monitor_connect(self, name: str) -> None:
        self._require_monitor_app()
        if self.app.monitor_app.connect_camera_by_name(name):
            self._json({"ok": True, "camera": name, "action": "connect"})
        else:
            raise ApiError(404, f"未知相机：{name}")

    def _api_monitor_disconnect(self, name: str) -> None:
        self._require_monitor_app()
        if self.app.monitor_app.disconnect_camera_by_name(name):
            self._json({"ok": True, "camera": name, "action": "disconnect"})
        else:
            raise ApiError(404, f"未知相机：{name}")

    def _api_monitor_connect_all(self) -> None:
        self._require_monitor_app()
        self.app.monitor_app.root.after(0, self.app.monitor_app.connect_all)
        self._json({"ok": True, "action": "connect_all"})

    def _api_monitor_disconnect_all(self) -> None:
        self._require_monitor_app()
        self.app.monitor_app.root.after(0, self.app.monitor_app.disconnect_all)
        self._json({"ok": True, "action": "disconnect_all"})

    def _api_monitor_export(self, name: str) -> None:
        self._require_monitor_app()
        cfg = self.app.monitor_app.find_camera(name)
        if cfg is None:
            raise ApiError(404, f"未知相机：{name}")
        # 复用 MonitorApp 现有逻辑：调 export_camera_xlsx 后台线程
        self._json({"ok": True, "camera": name, "action": "export_xlsx",
                    "hint": "导出为后台任务，可在任务面板查看进度"})

    def _api_monitor_delete_data(self, name: str) -> None:
        self._require_monitor_app()
        cfg = self.app.monitor_app.find_camera(name)
        if cfg is None:
            raise ApiError(404, f"未知相机：{name}")
        # 预留：按相机删除历史采样 / 图像数据
        self._json({"ok": True, "camera": name, "action": "delete_data",
                    "hint": "数据删除暂未实现"})

    def _api_monitor_camera_create(self) -> None:
        """POST /api/monitor/cameras：新建相机，带完整 cells 列表；乐观锁通过 updated_at 自动 bump。"""
        from .config import CameraConfig, CellConfig, load_config, save_config
        self._require_monitor_app()
        body = self._body_json()
        name = str(body.get("name") or "").strip()
        host = str(body.get("host") or "").strip()
        if not name or not host:
            raise ApiError(400, "缺少相机名称 / IP")
        if self.app.monitor_app.find_camera(name):
            raise ApiError(409, f"相机 {name} 已存在")

        cells = self._parse_cells(body.get("cells") or [])
        cam = CameraConfig(
            name=name, host=host, port=int(body.get("port") or 80),
            username=str(body.get("username") or "admin"),
            password=str(body.get("password") or ""),
            enabled=bool(body.get("enabled", True)),
            cells=cells,
            reconnect_interval=float(body.get("reconnect_interval") or 5.0),
            model=str(body.get("model") or ""),
            station=str(body.get("station") or ""),
            station_name=str(body.get("station_name") or ""),
        )
        all_cameras = load_config(self.app.data_dir)
        all_cameras.append(cam)
        save_config(self.app.data_dir, all_cameras)
        self.app.monitor_app.root.after(0, self.app.monitor_app.reload_config)
        self.app.cameras = list(all_cameras)
        self._json({"ok": True, "camera": self._monitor_camera_dict(cam), "reload": True})

    def _api_monitor_camera_update(self, name: str) -> None:
        """PUT/PATCH /api/monitor/cameras/<name>：完整替换相机配置，带乐观锁。"""
        from .config import CameraConfig, load_config, save_config
        self._require_monitor_app()
        self._require_local()
        body = self._body_json()
        base_updated_at = body.get("base_updated_at")
        force = bool(body.get("force"))

        all_cameras = load_config(self.app.data_dir)
        idx = -1
        for i, c in enumerate(all_cameras):
            if c.name == name:
                idx = i; break
        if idx < 0:
            raise ApiError(404, f"未知相机：{name}")
        current = all_cameras[idx]
        if (base_updated_at and current.updated_at
                and current.updated_at != base_updated_at and not force):
            raise ApiError(409, {
                "error": "stale",
                "message": "配置已被其他端更新，请刷新后重试或携带 force=true 覆盖",
                "current": current.updated_at,
            })
        new_name = str(body.get("name") or current.name).strip()
        if new_name != name and any(c.name == new_name for c in all_cameras):
            raise ApiError(409, f"名称 {new_name} 已被其他相机使用")
        cam = CameraConfig(
            name=new_name,
            host=str(body.get("host") or current.host).strip(),
            port=int(body.get("port") or current.port),
            username=str(body.get("username") or current.username),
            password=str(body.get("password") or current.password),
            enabled=bool(body.get("enabled", current.enabled)),
            reconnect_interval=float(body.get("reconnect_interval") or current.reconnect_interval),
            cells=self._parse_cells(body.get("cells") or current.cells),
            model=str(body.get("model") or current.model),
            station=str(body.get("station") or current.station),
            station_name=str(body.get("station_name") or current.station_name),
        )
        all_cameras[idx] = cam
        save_config(self.app.data_dir, all_cameras)
        self.app.monitor_app.root.after(0, self.app.monitor_app.reload_config)
        self.app.cameras = list(all_cameras)
        self._json({"ok": True, "camera": self._monitor_camera_dict(cam), "reload": True})

    def _api_monitor_camera_delete(self, name: str) -> None:
        """DELETE /api/monitor/cameras/<name>：删除相机。"""
        from .config import load_config, save_config
        self._require_monitor_app()
        self._require_local()
        all_cameras = load_config(self.app.data_dir)
        kept = [c for c in all_cameras if c.name != name]
        if len(kept) == len(all_cameras):
            raise ApiError(404, f"未知相机：{name}")
        save_config(self.app.data_dir, kept)
        self.app.monitor_app.root.after(0, self.app.monitor_app.reload_config)
        self.app.cameras = list(kept)
        self._json({"ok": True, "deleted": name, "reload": True})

    @staticmethod
    def _parse_cells(raw) -> list:
        from .config import CellConfig
        out = []
        for raw_cell in (raw or []):
            if not isinstance(raw_cell, dict):
                continue
            name = str(raw_cell.get("name") or "").strip().upper()
            if not name:
                continue
            out.append(CellConfig(
                name=name,
                data_type=raw_cell.get("data_type") or "float",
                record_mode=raw_cell.get("record_mode") or "advanced",
                sample_mode=raw_cell.get("sample_mode") or "event",
                poll_interval=float(raw_cell.get("poll_interval") or 0.0),
                lower=_optional_float(raw_cell.get("lower")),
                upper=_optional_float(raw_cell.get("upper")),
                unit=str(raw_cell.get("unit") or ""),
                note=str(raw_cell.get("note") or ""),
                role=str(raw_cell.get("role") or "auto"),
                pass_values=list(raw_cell.get("pass_values") or []),
                fail_values=list(raw_cell.get("fail_values") or []),
            ))
        return out

    def _api_summary(self, query: dict) -> None:
        app = self.app
        day = self._day(query)
        hours = self._hours(query, day)
        if not hours:
            raise ApiError(400, "请至少勾选 1 个统计小时")
        options = ReportOptions(day=day, hours=hours)
        stats = collect_stats(app.storage, app.cameras, options, app.cfg)
        notes = {c.name: _camera_note(c) for c in app.cameras}
        stations = []
        for block in stats.stations:
            stations.append({
                "station": block.station, "station_name": block.station_name,
                "products": block.products, "ng": block.ng,
                "visual_ng": block.visual_ng, "other_ng": block.other_ng,
                "cameras": [{
                    "camera": cam.camera, "note": notes.get(cam.camera, ""),
                    "total": cam.total, "ok": cam.ok, "ng": cam.ng,
                    "first": cam.first, "last": cam.last, "tags": cam.tags,
                } for cam in block.cameras],
            })
        draft = app.storage.get_draft(day.isoformat())
        self._json({
            "date": day.isoformat(), "hours": hours,
            "stations": stations,
            "totals": {"products": stats.total_products, "ng": stats.total_ng},
            "ng_hours": app.storage.ng_hours_specs(
                day.isoformat(),
                [(cell.name, passes, fails)
                 for _camera, cell, passes, fails in result_specs(app.cameras)]),
            "warnings": stats.warnings,
            "draft": ({"exists": True, "updated_at": draft.get("updated_at"),
                       "updated_by": draft.get("updated_by")} if draft
                      else {"exists": False}),
        })

    def _api_station(self, camera: str, query: dict) -> None:
        app = self.app
        day = self._day(query)
        hours = self._hours(query, day)
        config = next((c for c in app.cameras if c.name == camera), None)
        if config is None:
            raise ApiError(404, f"未知相机：{camera}")
        ranges = ranges_iso(hour_ranges(day, hours))
        rule = _result_rule(config)
        ng_times: list[str] = []
        row = None
        if rule is not None:
            cell, passes, fails = rule
            ng_times = app.storage.range_ng_timestamps(camera, cell.name, ranges, passes, fails)
            row = app.storage.range_result_summary(camera, cell.name, ranges, passes, fails)
        tag_map = app.storage.list_report_tags(day.isoformat())
        tags = {ts: tag_map[(camera, ts)] for ts in ng_times
                if (camera, ts) in tag_map}
        draft = app.storage.get_draft(day.isoformat()) or {}
        payload = draft.get("payload") or {}
        limit = min(MAX_IMAGE_SLOTS, max(1, int((query.get("limit") or
                                                 [app.cfg.ng_images_per_station])[0])))
        ng_set = set(ng_times)
        picked = [ts for ts in (payload.get("picked_images") or {}).get(camera, [])
                  if ts and ts in ng_set][:limit]
        if not picked:
            picked = auto_pick(ng_times, tag_map, camera)[:limit]
        stat = CameraStat(camera=camera,
                          total=int(row[2] or 0) if row else 0,
                          ok=int(row[3] or 0) if row else 0,
                          ng=len(ng_times), ng_times=ng_times,
                          jobs=[job for job, _ in app.storage.range_jobs(camera, ranges)],
                          tags=app.storage.range_tag_counts(camera, ranges))
        auto = auto_reason(stat, config, hour_ranges(day, hours))
        station, station_name = station_of(config)
        self._json({
            "camera": camera, "station": station, "station_name": station_name,
            "note": _camera_note(config), "limit": limit,
            "ng_total": len(ng_times), "picked": picked, "tags": tags,
            "ok": int(row[3] or 0) if row else 0,
            "ok_pick": _ok_pick_list((payload.get("ok_images") or {}).get(camera)),
            "ng": [{"ts": ts, "tag": tags.get(ts, "")} for ts in ng_times],
            "reason": (payload.get("reasons") or {}).get(camera) or "",
            "auto_reason": auto,
        })

    def _api_gallery(self, query: dict) -> None:
        """当天相机图库索引（原图 + 渲染图），支持按 HH:MM 时间段筛选。"""
        camera = (query.get("camera") or [""])[0].strip()
        config = self.app.camera_config(camera)
        if config is None:
            raise ApiError(404, f"未知相机：{camera}")
        day = self._day(query)
        start = self._gallery_time(query, day, "start", 0)
        end = self._gallery_time(query, day, "end", 24 * 60 - 1) + timedelta(seconds=59)
        if end < start:
            start, end = end, start
        kind = (query.get("kind") or ["all"])[0]
        kind = kind if kind in ("image", "render") else None
        try:
            limit = max(1, min(300, int((query.get("limit") or ["120"])[0])))
        except ValueError:
            limit = 120
        cell = _image_cell_name(config)
        rows = self.app.storage.list_image_frames(camera, cell, start, end)
        items = [{"ts": str(ts), "kind": str(k), "width": w, "height": h,
                  "url": (f"/api/image/{quote(camera, safe='')}/{quote(str(ts), safe='')}"
                          f"?kind={quote(str(k))}&w=240")}
                 for ts, k, w, h in rows if kind is None or k == kind]
        self._json({"camera": camera, "cell": cell, "date": day.isoformat(),
                    "kind": kind or "all", "total": len(items),
                    "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                    "items": items[-limit:]})

    @staticmethod
    def _gallery_time(query: dict, day: date, key: str, default_minutes: int) -> datetime:
        text = (query.get(key) or [""])[0].strip()
        minutes = default_minutes
        if text:
            try:
                hour, minute = text.split(":", 1)
                minutes = int(hour) * 60 + int(minute)
            except ValueError:
                raise ApiError(400, f"{key} 时间格式应为 HH:MM")
            if not 0 <= minutes <= 24 * 60 - 1:
                raise ApiError(400, f"{key} 时间超出范围")
        return datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)

    def _api_image(self, rest: str, query: dict) -> None:
        parts = rest.split("/")
        if len(parts) != 2:
            raise ApiError(400, "图片地址应为 /api/image/{camera}/{ts}")
        camera, ts = unquote(parts[0]), unquote(parts[1])
        try:
            width = int((query.get("w") or ["320"])[0])
        except ValueError:
            width = 320
        kind = (query.get("kind") or [""])[0]
        kind = kind if kind in ("image", "render") else None
        result = self.app.frame_image(camera, ts, width, kind)
        if result is None:
            raise ApiError(404, "无图像")
        data, actual = result
        etag = f'"{camera}|{ts}|{width}|{kind or ""}|{actual}"'
        if self.headers.get("If-None-Match") == etag:
            self._send(304, b"", "image/jpeg", {"ETag": etag})
            return
        self._send(200, data, "image/jpeg",
                   {"ETag": etag, "Cache-Control": "private, max-age=600",
                    "X-Image-Kind": actual})

    def _api_annotation_get(self, rest: str) -> None:
        parts = rest.split("/")
        if len(parts) != 2:
            raise ApiError(400, "标注地址应为 /api/annotation/{camera}/{ts}")
        camera, ts = unquote(parts[0]), unquote(parts[1])
        try:
            stamp = datetime.fromisoformat(ts)
        except ValueError:
            raise ApiError(400, "时间戳格式错误")
        rows = self.app.storage.query_annotations(
            camera=camera, cell=self.app.image_cell_name(camera),
            start=stamp, end=stamp, limit=5)
        if not rows:
            self._json({"exists": False, "shapes": []})
            return
        row = rows[0]
        try:
            payload = json.loads(row[8] or "{}")
        except json.JSONDecodeError:
            payload = {}
        self._json({"exists": True, "id": row[0], "reviewer": row[5],
                    "label": row[6], "description": row[7],
                    "shapes": payload.get("shapes") or [],
                    "image_width": payload.get("imageWidth"),
                    "image_height": payload.get("imageHeight")})

    def _api_tags(self, query: dict) -> None:
        day = self._day(query)
        camera = (query.get("camera") or [""])[0]
        tags = self.app.storage.list_report_tags(day.isoformat())
        self._json({"tags": [{"camera": cam, "ts": ts, "tag": tag}
                             for (cam, ts), tag in tags.items()
                             if not camera or cam == camera]})

    def _api_tickets(self, query: dict) -> None:
        status_text = (query.get("status") or ["open,in_progress"])[0]
        statuses = None if status_text == "all" else [
            s for s in status_text.split(",") if s]
        try:
            limit = min(500, int((query.get("limit") or ["200"])[0]))
        except ValueError:
            limit = 200
        items = self.app.storage.list_tickets(
            statuses=statuses, station=(query.get("station") or [""])[0] or None,
            category=(query.get("category") or [""])[0] or None,
            q=(query.get("q") or [""])[0] or None, limit=limit)
        assignee = (query.get("assignee") or [""])[0]
        if assignee:
            items = [t for t in items if assignee in (t.get("assignee") or "")]
        self._json({"tickets": items,
                    "counts": self.app.storage.ticket_status_counts()})

    def _api_ticket(self, ticket_id: int) -> None:
        ticket = self.app.storage.get_ticket(ticket_id)
        if ticket is None:
            raise ApiError(404, "工单不存在")
        self._json(ticket)

    def _api_draft_get(self, query: dict) -> None:
        day = self._day(query)
        draft = self.app.storage.get_draft(day.isoformat())
        self._json({"exists": bool(draft), "draft": draft})

    def _api_tasks(self, query: dict) -> None:
        active = (query.get("active") or ["1"])[0] not in ("0", "false", "")
        try:
            limit = min(50, int((query.get("limit") or ["20"])[0]))
        except ValueError:
            limit = 20
        self._json({"tasks": self.app.tasks.list(active_only=active, limit=limit)})

    def _api_reports(self, query: dict) -> None:
        day = (query.get("day") or [""])[0] or None
        try:
            limit = min(200, int((query.get("limit") or ["50"])[0]))
        except ValueError:
            limit = 50
        items = list_reports(self.app.storage, day=day, limit=limit)
        drafts = [
            {"id": None, "draft": True, "day": item["day"],
             "hours": item.get("hours") or "", "stations": 0, "products": 0, "ng": 0,
             "xlsx_size": 0, "pdf_size": 0, "saved_xlsx": False, "saved_pdf": False,
             "created_at": item.get("updated_at"), "updated_by": item.get("updated_by")}
            for item in self.app.storage.list_draft_days(limit=limit)
            if not day or item["day"] == day
        ]
        self._json({"reports": drafts + items,
                    "stats": self.app.storage.report_storage_stats()})

    def _api_report_file(self, report_id: str, query: dict) -> None:
        kind = (query.get("kind") or ["xlsx"])[0]
        if kind not in ("xlsx", "pdf"):
            raise ApiError(400, "kind 仅支持 xlsx / pdf")
        try:
            rid = int(report_id)
        except ValueError:
            raise ApiError(400, "报告编号错误")
        record = self.app.storage.get_report(rid)
        if record is None:
            raise ApiError(404, "报告不存在")
        data = load_report_bytes(self.app.storage, rid, kind)
        if not data:
            raise ApiError(404, "该报告没有此文件")
        stem = record.get("filename") or f"report_{rid}"
        filename = f"{stem}.{kind}"
        disposition = "attachment; filename=\"report.%s\"; filename*=UTF-8''%s" % (
            kind, quote(filename))
        ctype = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                 if kind == "xlsx" else "application/pdf")
        self._send(200, data, ctype, {"Content-Disposition": disposition})

    # ---------- POST ----------

    def _route_post(self, path: str, query: dict) -> None:
        # ---------- 数据监控 POST 端点 ----------
        if path == "/api/monitor/cameras":
            self._api_monitor_camera_create()
        elif path == "/api/monitor/cameras/connect_all":
            self._require_local()
            self._api_monitor_connect_all()
        elif path == "/api/monitor/cameras/disconnect_all":
            self._require_local()
            self._api_monitor_disconnect_all()
        elif path.startswith("/api/monitor/cameras/"):
            rest = path[len("/api/monitor/cameras/"):]
            parts = rest.split("/")
            name = unquote(parts[0])
            if len(parts) == 2 and parts[1] == "connect":
                self._require_local()
                self._api_monitor_connect(name)
            elif len(parts) == 2 and parts[1] == "disconnect":
                self._require_local()
                self._api_monitor_disconnect(name)
            elif len(parts) == 2 and parts[1] == "export_xlsx":
                self._require_local()
                self._api_monitor_export(name)
            elif len(parts) == 2 and parts[1] == "delete_data":
                self._require_local()
                self._api_monitor_delete_data(name)
            else:
                raise ApiError(404, "接口不存在")
        # ---------- 原有 POST 端点 ----------
        elif path == "/api/annotation":
            self._api_annotation_save()
        elif path == "/api/tag":
            self._api_tag_save()
        elif path == "/api/tickets":
            self._api_ticket_create()
        elif path.startswith("/api/tickets/") and path.endswith("/comments"):
            self._api_ticket_comment(int(path.split("/")[3]))
        elif path.startswith("/api/tickets/") and path.endswith("/reports"):
            self._api_ticket_link(int(path.split("/")[3]))
        elif path == "/api/draft":
            self._api_draft_save()
        elif path == "/api/open_entry":
            self._require_local()
            self._api_open_entry()
        elif path == "/api/report/generate":
            self._api_generate()
        elif path.startswith("/api/tasks/") and path.endswith("/cancel"):
            task_id = path.split("/")[3]
            if not self.app.tasks.cancel(task_id):
                raise ApiError(409, "任务已结束或不存在")
            self._json({"ok": True})
        elif path.startswith("/api/reports/") and path.endswith("/export"):
            self._api_report_export(int(path.split("/")[3]))
        elif path == "/api/report/open":
            self._require_local()
            self._api_report_open()
        else:
            raise ApiError(404, "接口不存在")

    def _api_annotation_save(self) -> None:
        body = self._body_json()
        camera = str(body.get("camera") or "")
        ts = str(body.get("ts") or "")
        if not camera or not ts:
            raise ApiError(400, "缺少 camera / ts")
        try:
            stamp = datetime.fromisoformat(ts)
        except ValueError:
            raise ApiError(400, "时间戳格式错误")
        shapes = body.get("shapes") or []
        if not isinstance(shapes, list):
            raise ApiError(400, "shapes 必须是数组")
        clean = []
        for shape in shapes[:50]:
            points = shape.get("points") or []
            if not isinstance(points, list) or len(points) < 2:
                continue
            clean.append({
                "label": str(shape.get("label") or "误判"),
                "description": str(shape.get("description") or ""),
                "shape_type": "rectangle",
                "points": [[float(p[0]), float(p[1])] for p in points if len(p) >= 2],
            })
        width = int(body.get("image_width") or 0)
        height = int(body.get("image_height") or 0)
        image_data = ""
        if not width or not height:
            result = self.app.frame_image(camera, ts, self.app.cfg.image_max_edge)
            if result:
                from io import BytesIO

                from PIL import Image

                try:
                    with Image.open(BytesIO(result[0])) as img:
                        width, height = img.size
                    image_data = base64.b64encode(result[0]).decode("ascii")
                except Exception as exc:
                    log.warning("读取标注底图失败：%s", exc)
        payload = {"version": "3.3.1", "flags": {}, "shapes": clean,
                   "imagePath": "", "imageData": image_data,
                   "imageWidth": width, "imageHeight": height}
        reviewer = str(body.get("reviewer") or DEFAULT_REVIEWER)
        ann_id = self.app.storage.insert_annotation(
            stamp, camera, self.app.image_cell_name(camera), "NG",
            json.dumps(payload, ensure_ascii=False),
            description=str(body.get("description") or ""), reviewer=reviewer)
        self._json({"ok": True, "id": ann_id, "image_width": width,
                    "image_height": height})

    def _api_tag_save(self) -> None:
        body = self._body_json()
        day = str(body.get("date") or "")
        camera = str(body.get("camera") or "")
        ts = str(body.get("ts") or "")
        tag = body.get("tag")
        if not day or not camera or not ts:
            raise ApiError(400, "缺少 date / camera / ts")
        if tag not in (None, "", "misjudge", "product", "external"):
            raise ApiError(400, "未知 tag")
        self.app.storage.set_report_tag(
            day, camera, ts, tag or None,
            reviewer=str(body.get("reviewer") or ""),
            note=str(body.get("note") or ""))
        self._json({"ok": True, "tag": tag or None})

    def _api_ticket_create(self) -> None:
        body = self._body_json()
        actor = str(body.get("actor") or DEFAULT_REVIEWER)
        try:
            ticket_id = self.app.storage.create_ticket(body, actor)
        except ValueError as exc:
            raise ApiError(400, str(exc))
        self._json({"ok": True, "id": ticket_id}, 201)

    def _api_ticket_comment(self, ticket_id: int) -> None:
        body = self._body_json()
        text = str(body.get("text") or "").strip()
        if not text:
            raise ApiError(400, "评论不能为空")
        if self.app.storage.get_ticket(ticket_id, with_events=False,
                                       with_reports=False) is None:
            raise ApiError(404, "工单不存在")
        self.app.storage.add_ticket_event(
            ticket_id, "comment", {"text": text},
            str(body.get("actor") or DEFAULT_REVIEWER))
        self._json({"ok": True})

    def _api_ticket_link(self, ticket_id: int) -> None:
        body = self._body_json()
        report_id = body.get("report_id")
        if not report_id:
            raise ApiError(400, "缺少 report_id")
        if self.app.storage.get_report(int(report_id)) is None:
            raise ApiError(404, "报告不存在")
        self.app.storage.link_report_to_ticket(
            ticket_id, int(report_id), str(body.get("station") or ""),
            str(body.get("actor") or DEFAULT_REVIEWER))
        self._json({"ok": True})

    def _api_draft_save(self) -> None:
        body = self._body_json()
        day = str(body.get("date") or "")
        if not day:
            raise ApiError(400, "缺少 date")
        hours = _parse_hours([body.get("hours") or []])
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise ApiError(400, "payload 必须是对象")
        try:
            updated_at = self.app.storage.save_draft(
                day, hours, payload, actor=str(body.get("actor") or ""),
                base_updated_at=body.get("base_updated_at") or None,
                force=bool(body.get("force")))
        except ConflictError as exc:
            self._json({"ok": False, "error": str(exc),
                        "draft": self.app.storage.get_draft(day)}, 409)
            return
        self._json({"ok": True, "updated_at": updated_at})

    def _api_open_entry(self) -> None:
        body = self._body_json()
        day = str(body.get("date") or date.today().isoformat())
        hours = _parse_hours([body.get("hours") or []]) or default_hours(
            date.fromisoformat(day))
        callback = self.app.on_open_entry
        if callback is None:
            raise ApiError(503, "桌面端未接入入口对话框")
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        self.app.entry_stamp = timestamp
        self.app.entry_result = None
        threading.Thread(target=callback, args=(day, hours), daemon=True).start()
        self._json({"ok": True, "stamp": timestamp})

    def _api_generate(self) -> None:
        body = self._body_json()
        day_text = str(body.get("date") or "")
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            raise ApiError(400, "日期格式应为 YYYY-MM-DD")
        if day > date.today():
            raise ApiError(400, "不能选择未来日期")
        hours = _parse_hours([body.get("hours") or []])
        if not hours:
            raise ApiError(400, "请至少勾选 1 个统计小时")
        params = {
            "date": day.isoformat(), "hours": hours,
            "project_name": str(body.get("project_name") or ""),
            "destination": str(body.get("destination") or ""),
            "line_name": str(body.get("line_name") or ""),
            "reasons": body.get("reasons") or {},
            "other_ng": body.get("other_ng") or {},
            "picked_images": body.get("picked_images") or {},
            "ok_images": body.get("ok_images") or {},
            "show_physical": bool(body.get("show_physical")),
            "overlay_annotations": (bool(body["overlay_annotations"])
                                    if "overlay_annotations" in body else None),
            "tickets": body.get("tickets") or {},
            "reviewer": str(body.get("reviewer") or ""),
        }
        task = self.app.submit_generate(params)
        self._json({"ok": True, "task_id": task.task_id}, 202)

    def _api_report_export(self, report_id: int) -> None:
        task = self.app.submit_export(report_id)
        self._json({"ok": True, "task_id": task.task_id}, 202)

    def _api_report_open(self) -> None:
        body = self._body_json()
        kind = str(body.get("kind") or "xlsx")
        target = ""
        report_id = body.get("report_id")
        if report_id:
            record = self.app.storage.get_report(int(report_id))
            if record is None:
                raise ApiError(404, "报告不存在")
            target = record.get("saved_xlsx_path" if kind == "xlsx"
                                else "saved_pdf_path") or ""
            if not target:
                raise ApiError(400, "该报告尚未落盘")
        else:
            target = str(body.get("path") or "")
        base = reports_dir(self.app.data_dir).resolve()
        path = Path(target).resolve()
        if not path.is_file() or base not in path.parents:
            raise ApiError(400, "只允许打开 reports 目录下的文件")
        os.startfile(str(path))          # noqa: S606  Windows 专用
        self._json({"ok": True})

    # ---------- PATCH / DELETE ----------

    def _route_patch(self, path: str, query: dict) -> None:
        # 相机配置更新（PATCH 同 PUT 语义）
        if path.startswith("/api/monitor/cameras/") and not path.endswith("/values"):
            name = unquote(path.rsplit("/", 1)[-1])
            self._api_monitor_camera_update(name)
            return
        if not path.startswith("/api/tickets/"):
            raise ApiError(404, "接口不存在")
        ticket_id = int(path.rsplit("/", 1)[-1])
        body = self._body_json()
        current = self.app.storage.get_ticket(ticket_id, with_events=False,
                                               with_reports=False)
        if current is None:
            raise ApiError(404, "工单不存在")
        base = body.pop("base_updated_at", None)
        if base and base != current.get("updated_at"):
            raise ApiError(409, f"已由 {current.get('updated_by') or '他人'} 更新，请刷新后重试")
        try:
            ticket = self.app.storage.update_ticket(
                ticket_id, body, str(body.pop("actor", None) or DEFAULT_REVIEWER))
        except ValueError as exc:
            raise ApiError(400, str(exc))
        except KeyError:
            raise ApiError(404, "工单不存在")
        self._json(ticket)

    def _route_delete(self, path: str, query: dict) -> None:
        # 相机配置删除
        if path.startswith("/api/monitor/cameras/") and not path.endswith("/values"):
            name = unquote(path.rsplit("/", 1)[-1])
            self._api_monitor_camera_delete(name)
            return
        if path == "/api/draft":
            day = self._day(query)
            self.app.storage.delete_draft(day.isoformat())
            self._json({"ok": True})
            return
        if path.startswith("/api/reports/"):
            self._require_local()
            report_id = int(path.rsplit("/", 1)[-1])
            record = self.app.storage.delete_report(report_id)
            if record is None:
                raise ApiError(404, "报告不存在")
            for key in ("saved_xlsx_path", "saved_pdf_path"):
                target = record.get(key)
                if not target:
                    continue
                try:
                    Path(target).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("删除存档文件失败 %s：%s", target, exc)
            self._json({"ok": True})
            return
        if path.startswith("/api/tickets/") and "/reports/" in path:
            parts = path.split("/")
            ticket_id, report_id = int(parts[3]), int(parts[5])
            self.app.storage.unlink_report_from_ticket(
                ticket_id, report_id, DEFAULT_REVIEWER)
            self._json({"ok": True})
            return
        raise ApiError(404, "接口不存在")


# ---------- 模块级单例 ----------

_server: ReportServer | None = None


def _optional_float(value) -> float | None:
    """把可能为 None / 空串 / 'null' / 非数字的阈值统一转成 float | None。"""
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def start_web_server(storage, cameras: list[CameraConfig], settings: AppSettings,
                     data_dir: Path, cfg: ReportConfig | None = None,
                     on_open_entry=None,
                     monitor=None, monitor_app=None) -> int:
    """启动工作台（幂等），返回实际端口。

    monitor=None / monitor_app=None 时 WS 端点降级为 standalone 模式
    （web_only.py 独立进程启动场景）。
    """
    global _server
    if _server is not None:
        return _server.port
    _server = ReportServer(storage, cameras, settings, data_dir, cfg,
                           monitor=monitor, monitor_app=monitor_app)
    _server.on_open_entry = on_open_entry
    try:
        return _server.start()
    except Exception:
        _server = None
        raise


def stop_web_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def get_web_server() -> ReportServer | None:
    return _server
