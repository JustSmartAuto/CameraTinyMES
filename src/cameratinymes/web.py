"""改善报告本地 Web 工作台：HTTP 服务、标注/草稿/tag/工单接口、报告生成任务。

设计见 `docs/当日改善报告功能设计.md` §4.1~§4.9、§6.5：
- 标准库 `ThreadingHTTPServer`，监听 `0.0.0.0`（可由 `settings.web_host` 改回仅本机）；
- 所有耗时操作（生成、落盘）只投递 `TaskManager`，HTTP 线程立即返回 `202 {task_id}`；
- 敏感操作（弹日期对话框、打开文件、删除存档）仅回环地址可用；
- 请求体上限 1MB，POST 校验 `Origin`，可选口令 `web_token`；
- 端口被占用时 +1 顺延（最多 20 次），实际端口写入 `settings.json` 与 `reports/.web_port`。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import sys
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import (AppSettings, CameraConfig, DEFAULT_IMAGE_CELL, ReportConfig,
                     image_cell, result_cell, result_specs, result_tokens,
                     save_settings)
from .report import (MAX_NG_IMAGES, CameraStat, ReportOptions,
                     auto_pick, auto_reason, collect_stats, hour_ranges, ranges_iso,
                     station_of)
from .reportstore import (export_report_to_disk, list_reports, load_report_bytes,
                          purge_reports, reports_dir, save_report)
from .storage import ConflictError
from .tasks import CancelledError, TaskManager

log = logging.getLogger(__name__)

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
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
                 tasks: TaskManager | None = None):
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
        log.info("报告工作台已启动：http://%s:%s/report", host, self.port)
        return self.port

    def stop(self) -> None:
        self._cleanup_stop.set()
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
        options = ReportOptions(
            day=day, hours=hours,
            reasons=params.get("reasons") or {},
            other_ng=params.get("other_ng") or {},
            picked_images=params.get("picked_images") or {},
            show_physical=bool(params.get("show_physical")),
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

    def frame_image(self, camera: str, ts: str, width: int) -> tuple[bytes, str] | None:
        """取该事件时刻最近一帧（渲染图优先），返回 (JPEG, kind)。"""
        try:
            center = datetime.fromisoformat(ts)
        except ValueError:
            return None
        key = (camera, ts, int(width))
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
        for frame_ts, kind, _mime, _w, _h, data in rows:
            try:
                gap = abs((datetime.fromisoformat(str(frame_ts)) - center).total_seconds())
            except ValueError:
                continue
            if gap > FRAME_WINDOW:
                continue
            bucket = "render" if kind == "render" else "image"
            if bucket not in best or gap < best[bucket][0]:
                best[bucket] = (gap, str(frame_ts), data)
        chosen = best.get("render") or best.get("image")
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
        if not _is_loopback(self.client_address[0]):
            raise ApiError(403, "该操作仅允许本机执行")

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
        self._send(status, path.read_bytes(), "text/html; charset=utf-8",
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
            self._send(200, b"", "image/x-icon")
            return
        if path == "/api/dates":
            self._api_dates()
        elif path == "/api/config":
            self._api_config()
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
            "ng_images_per_station": max(1, min(MAX_NG_IMAGES,
                                                int(app.cfg.ng_images_per_station))),
            "image_max_edge": int(app.cfg.image_max_edge),
            "cameras": cameras,
            "stations": list(groups.values()),
            "today": date.today().isoformat(),
            "web_host": app.host, "web_port": app.port,
            "token_required": bool(app.token),
            "urls": app.urls(),
        })

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
        limit = min(MAX_NG_IMAGES, max(1, int((query.get("limit") or
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
            "ng": [{"ts": ts, "tag": tags.get(ts, "")} for ts in ng_times],
            "reason": (payload.get("reasons") or {}).get(camera) or "",
            "auto_reason": auto,
        })

    def _api_image(self, rest: str, query: dict) -> None:
        parts = rest.split("/")
        if len(parts) != 2:
            raise ApiError(400, "图片地址应为 /api/image/{camera}/{ts}")
        camera, ts = unquote(parts[0]), unquote(parts[1])
        try:
            width = int((query.get("w") or ["320"])[0])
        except ValueError:
            width = 320
        result = self.app.frame_image(camera, ts, width)
        if result is None:
            raise ApiError(404, "无图像")
        data, kind = result
        etag = f'"{camera}|{ts}|{width}|{kind}"'
        if self.headers.get("If-None-Match") == etag:
            self._send(304, b"", "image/jpeg", {"ETag": etag})
            return
        self._send(200, data, "image/jpeg",
                   {"ETag": etag, "Cache-Control": "private, max-age=600",
                    "X-Image-Kind": kind})

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
        self._json({"reports": items,
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
        if path == "/api/annotation":
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
            "show_physical": bool(body.get("show_physical")),
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


def start_web_server(storage, cameras: list[CameraConfig], settings: AppSettings,
                     data_dir: Path, cfg: ReportConfig | None = None,
                     on_open_entry=None) -> int:
    """启动工作台（幂等），返回实际端口。"""
    global _server
    if _server is not None:
        return _server.port
    _server = ReportServer(storage, cameras, settings, data_dir, cfg)
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
