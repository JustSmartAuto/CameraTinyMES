# 数据监控 Web 实施方案

> 版本 1.0 · 2026-11-13
> 目标：在现有 `CameraTinyMES` Web 工作台基础上新增"数据监控"页签（顶部第一个），完整复刻 tkinter 主界面的相机数据监控与单元格配置能力，并保持 Web 端与 Tkinter 端状态同源、实时同步。

---

## 1. 现状盘点

### 1.1 Tkinter 主界面已覆盖功能

| 能力 | 入口 | 数据来源 |
|---|---|---|
| 多相机宫格面板，每单元格实时值 + 类型/阈值/模式标签 | 工具栏「连接全部/断开全部」+ 每面板「连接/断开」按钮 | `Monitor.events` queue（100ms poll） |
| 单相机趋势图（matplotlib，红=超差，虚线=阈值基线） | 每面板「趋势图」按钮 | `storage` 查询历史 samples |
| 相机增删改（名称/IP/端口/启用/作业刷新） | 工具栏「添加相机」、下拉菜单「配置单元格」 | `cameras.json` + `Worker` stop/start |
| 单元格订阅编辑（数据类型/记录模式/采样模式/上下限/单位/用途/通过值） | 每面板「编辑」按钮 → `CellEditorDialog` | `cameras.json` + `Worker` 重启后生效 |
| 相机级 xlsx 导出 | 每面板「导出xlsx」按钮 | `storage.range_*` + `wpsdispimg` |
| 全量/选择性 xlsx 导出、删除相机数据 | 数据菜单 | `storage.export_xlsx` / `storage.delete_*` |
| 数据库健康状态（SQLite/MariaDB + 尺寸 MB） | 工具栏右侧 | 后台线程 5s poll `storage.health_check` + `database_size_mb` |
| AV1 压缩进度（轮次/帧数/体积/压缩比） | 工具栏右侧 | `monitor.compressor.stats`，50×100ms 刷一次 |
| 运行日志（连接/断开/作业切换/错误） | 主界面底部 Text | `Monitor.events` + Worker |
| 托盘图标、开机自启、数据库自动迁移 | 启动流程 | `tray.py` / `autostart.py` / `storage._migrate_schema` |

**核心实时总线**：`Monitor.events: queue.Queue(maxsize=1000)`，Worker 线程 `put_nowait` 推送六种事件 `(camera_name, kind, payload)`：

```
connected     payload=None         → Worker websocket 握手成功
disconnected  payload=None         → Worker websocket 断开
error         payload=str          → 异常信息
session       payload=str          → CogSocket sid
result        payload={cell:value} → 新采样点（非图像/点云/媒体单元格）
job           payload=str          → 当前作业（jobx 配方名）
image         payload={cell:str}   → 图像单元格值（占位，UI 只显占位）
```

满队列时 `put_nowait` 丢事件，不阻塞采集线程。`Monitor._poll_events` 只在 tk 主线程轮询，Web 后端完全看不到实时事件。

### 1.2 现有 Web 后端与前端

**后端** (`src/cameratinymes/web.py`)：
- `ReportServer` 构造函数只收 `storage / cameras / settings / data_dir / cfg / tasks`，无 `Monitor` 引用
- HTTP 端点覆盖：配置快照、日汇总、相机详情、NG 图库、图像、标注、标签、工单（CRUD + 评论 + 链接）、草稿（GET/POST）、任务队列、报告存档（GET/POST），共 **~25 个端点**
- 无实时事件推送（WebSocket/SSE），所有数据按需 HTTP 拉取
- `_check_origin` 仅对 POST/PATCH/DELETE 生效；GET 开放；token=口令可走 `?k=` / Header `X-CTM-Token` / Cookie
- `_require_local` 对"打开文件/目录/退出/POST /api/open_entry"等敏感操作强制仅回环地址
- `_FrameCache`：`OrderedDict` LRU 缓存缩略图（200 条，key = `(camera, ts, width, kind)`），模式可复用

**前端** (`src/cameratinymes/webui/`)：
- 单文件壳 `index.html`（168 行）+ `app.js`（1710 行）+ `style.css`；4 个页签：工作台 / 报告存档 / 工单 / OK-OK 率
- BeOS 风格令牌（`assets/07-beos.css`），与 Tkinter 主界面视觉一致
- 已具备：延迟 150ms 顶部进度条、首屏骨架、请求序号丢弃过期响应、后台任务面板、OK/NG 率跨 0 点自动切日

**关键缺口**：

| 缺口 | 现状 | 本方案要补 |
|---|---|---|
| 实时连接状态 / 最新单元格值 | 无 | Monitor.events 多订阅者 + WebSocket 推送 |
| 相机 CRUD + 单元格订阅编辑 | 只读 `_api_config`（无 cells，无写接口） | 完整读写，写后触发 Tkinter 热重载 |
| 数据库健康 / AV1 压缩状态 | 无 | db_status_q 多订阅者 + HTTP 轮询 |
| 历史趋势图（Chart.js） | 无 | 新增 `storage.range_cell_values` + Chart.js 前端 |
| 相机级 xlsx 导出 | 无 | 复用现有 `web.py` `/api/report/export` 异步任务 |
| 敏感操作本地限制 | 已有 `_require_local` | 新增端点也要遵循 |
| OK/NG 判定规则 | web.py 已按相机独立配置规则聚合 | 继续复用 `config.result_specs` / `okng.hourly_okng` |
| 图表库 | 无 | Chart.js v4（本地 UMD 压缩版，随 PyInstaller 打包） |

### 1.3 技术硬约束（从项目记忆摘取，本方案必须遵守）

1. Web 后端入口：`ThreadingHTTPServer` 单进程，与 Tkinter 主线程同进程，Worker 事件在独立 asyncio 线程
2. 权限：GET 开放；POST 需 Origin（本机/局域网 IP）或空 Origin + `X-CTM-Token`；敏感操作 `_require_local`（回环地址）
3. PyInstaller 打包：已配 `--add-data ...;webui`，新资源（chart.umd.min.js）放 `webui/` 即可带上
4. SQLite vs MariaDB：`ts` 字段均为 ISO8601 本地时间字符串，`BETWEEN` 可直接比较；`length()` 在两端语义不同，**不得用**；`storage.open_storage` 已封装双实现
5. `Monitor.events` 是生产端，丢事件不能阻塞 Worker；`db_status_q` 同理
6. cameras.json 配置热重载：Tkinter 当前不监听文件变更，Worker 不会因配置变更自动重启 session

---

## 2. 整体架构

### 2.1 数据流拓扑

```
                ┌────────────────────────────────────┐
                │  Worker 线程（asyncio）             │
                │  CogSocket ↔ samples 落库           │
                └──────────┬──────────┬───────────────┘
                           │          │ put_nowait
                           ▼          ▼
                 Monitor.events[0]   Monitor.events[1]
                  (Tkinter poll)     (Web 订阅者)
                           │          │
                    ┌──────▼──┐   ┌───▼──────────────────────────┐
                    │ ui.py   │   │ ReportServer._events_q      │
                    │ _poll_  │   │ 100ms poll → WebSocket push │
                    │ events  │   └───────────┬──────────────────┘
                    └─────────┘               │
                    (status/value        ┌─────▼────────┐
                     → CameraPanel)      │ 浏览器 Tab A │
                                         │ 浏览器 Tab B │
                                         └──────────────┘
                                         (独立 WS 连接)

   cameras.json ───── 写 ──→ Web POST /api/cameras  ──→ save_config + reload_config
                                             │
                                             ▼
                                 Tkinter reload_config()
                                   save_config → _rebuild_grid → Worker stop/start
```

### 2.2 关键设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | `Monitor.events` 从单条 `queue.Queue` 改成 `list[queue.Queue]` 多订阅者，每个订阅者独立 `maxsize=1000`，`put_nowait` 逐队列推送 | 不阻塞 Worker 采集（任一满队列只丢该订阅者的事件，不影响 Tkinter）；保留现有 Tkinter poll 逻辑 0 改造成本；Web 端丢失仅影响浏览器显示精度，不影响数据落库 |
| D2 | 实时事件走 **WebSocket**（`/ws` 端点），历史趋势走 HTTP 拉取 | 避免 HTTP 轮询 1s+ 延迟；pyproject.toml 已有 `websockets>=15`；WebSocket 握手是 HTTP Upgrade，可在现有 `ThreadingHTTPServer` 上拦截 Upgrade 头并将该连接移交给 `websockets` 协议栈 |
| D3 | Chart.js UMD 压缩版（约 90KB）拷到 `src/cameratinymes/webui/chart.umd.min.js`，**不走 CDN** | PyInstaller 打包后无外网必须自包含；打包脚本已配 `--add-data`；CDN 路径在源码运行时也不可用 |
| D4 | 配置热重载链路：Web 写 cameras.json → `MonitorApp.reload_config()` → save_config + rebuild_grid + Worker stop/start | Worker 已连接状态下 session 已按旧 cell 列表打开，cell 集合变化必须重开 session；不变化的相机 Worker 不动；save_config 是原子写，_rebuild_grid 已做状态快照，Worker stop/start 由 Monitor 封装 |
| D5 | 相机/单元格配置的 Web 端编辑走 **POST + 乐观锁**（读时返回 `updated_at`，写时携带 `base_updated_at`，冲突返回 409 让客户端确认覆盖） | 与现有 `report_drafts` 的乐观锁机制一致；避免 Tkinter 和 Web 端同时编辑互相覆盖 |
| D6 | 数据库健康与 AV1 压缩状态走 **HTTP 轮询 5s**，复用现有 `db_status_q` 多订阅者改造 | 这两项变化慢（AV1 压缩 30 帧一刷约几秒~几十秒），5s 足够；WebSocket 带宽省给实时值 |
| D7 | 实时数值在 ReportServer 进程内维护 `_LiveCache`（每相机每单元格最近 200 点，`OrderedDict` LRU，类似 `_FrameCache`），供 WebSocket 客户端上线时拉取"最近 5 秒快照" | 新上线页面不用等下一个事件才能看到值；Chart.js 初始数据直接从 LiveCache 拿；LiveCache 独立于 Monitor.events queue，满了只丢最旧值 |
| D8 | "数据监控"页签插到 `index.html` 顶部 **第一个 tab**（工作台之前），`data-view="monitor"` | 用户默认打开即看实时状态；其他页签逻辑不动 |

### 2.3 不做的事（边界）

- **不接管 Tkinter 主循环**：WebSocket 推送在独立线程跑，Worker 事件在独立 asyncio 线程，`MonitorApp.reload_config()` 由 Web 后端通过 `get_web_server().on_reload_cameras(cameras)` 回调触发，Tkinter 主线程用 `root.after(0, ...)` 安全调度
- **不做 WebRTC 视频流**：图表走 Canvas；图像走 HTTP 静态下载（复用现有 `/api/image/` 端点 + `_FrameCache`）
- **不做"Web ↔ Tkinter 双向操作镜像"**：Tkinter 端操作（比如工具栏点"连接全部"）**不主动推到 Web 端**，Web 端通过 WS 事件和 HTTP 刷新看到最终状态即可；Web 端配置变更会**触发 Tkinter 重载**（单向 Web→Tkinter）
- **不做 WebSocket 消息的跨进程广播**：`web_only.py` 是独立进程，无 Monitor 引用，`/ws` 端点在该模式下返回 `{"error": "standalone mode, no live events"}`；`web_only.py` 只提供历史数据查询

---

## 3. 后端改造

### 3.1 Monitor / db_status_q 多订阅者改造（worker.py + ui.py）

**worker.py — Monitor.events 拆分**：

```python
# worker.py 改造前
class Monitor:
    def __init__(self, storage):
        self.events: queue.Queue = queue.Queue(maxsize=1000)

# 改造后
class Monitor:
    def __init__(self, storage):
        self._queues: list[queue.Queue] = []          # 多订阅者
        self.events = self.subscribe(maxsize=1000)     # Tkinter 原有订阅者保持兼容

    def subscribe(self, maxsize: int = 1000) -> queue.Queue:
        q = queue.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def _push(self, camera_name: str, kind: str, payload=None) -> None:
        for q in self._queues:
            try:
                q.put_nowait((camera_name, kind, payload))
            except queue.Full:
                pass   # 单订阅者满队列丢事件，不阻塞 Worker
```

Worker 侧所有 `self.events.put_nowait(...)` 调用**保留不动**——把 `queue.Queue.put_nowait` 替换成 Monitor 封装的 `_push` 方法，效果是把事件扇出到所有订阅者，原有 Tkinter 订阅者的消费逻辑零改动。

**ui.py — db_status_q 同理**：

```python
# ui.py MonitorApp._start_db_monitor 改造
def _start_db_monitor(self):
    self._db_queues: list[queue.Queue] = [self._db_status_q]   # 原有
    ...  # 后台线程 put_nowait 改为扇出

def subscribe_db(self, maxsize=50) -> queue.Queue:
    q = queue.Queue(maxsize=maxsize)
    self._db_queues.append(q)
    return q
```

### 3.2 storage 新增 `range_cell_values` 方法（storage.py）

用于 Chart.js 历史趋势图：按相机 + 单元格 + 时间范围取逐点 value。双实现（SQLiteStorage / MariaDBStorage）共用一个 SQL 模板，因为两端 `ts` 都是 ISO 字符串，`BETWEEN` 可直接比较。

```python
# 加在 StorageBase，SQLiteStorage / MariaDBStorage 都走基类实现
def range_cell_values(self, camera: str, cell: str,
                      ts_from: str, ts_to: str,
                      limit: int = 5000) -> list[tuple[str, float | None, int | None]]:
    """按相机+单元格+时间范围取逐点采样：(ts, value, ok)。
    value 可能为 None（string/bool 单元格或无法解析为数值）；
    ok 可能为 None（无阈值判定）。limit 避免超大时间范围。
    """
    sql = (
        "SELECT ts, value, ok FROM samples"
        " WHERE camera=? AND cell=? AND ts>=? AND ts<=?"
        " ORDER BY ts DESC LIMIT ?"
    )
    rows = self._fetchall(sql, (camera, cell, ts_from, ts_to, int(limit)))
    return list(reversed(rows))   # 升序供前端直接渲染
```

MariaDB 端同样调用 `_fetchall`——`storage.open_storage` 已根据 `db_backend` 自动选择 SQLiteStorage / MariaDBStorage，基类定义方法两边都能继承。**注意**：如果后续需要做 `GROUP BY date / hour` 或聚合，MariaDB 可能要改写（字符串 substr vs DATE 函数），但本方法只做直查，两边等价。

### 3.3 ReportServer 扩展（web.py）

**构造函数新增 Monitor / MonitorApp 引用 + LiveCache + WS 端点**：

```python
# web.py ReportServer 改造
class ReportServer:
    def __init__(self, storage, cameras, settings, data_dir,
                 cfg=None, tasks=None,
                 monitor: "Monitor | None" = None,          # NEW
                 monitor_app: "MonitorApp | None" = None): # NEW
        ...
        self.monitor = monitor
        self.monitor_app = monitor_app
        self._live_cache: dict[tuple[str, str], list[tuple[str, object]]] = {}
        self._live_lock = threading.Lock()
        self._live_subscription: queue.Queue | None = None   # Monitor 订阅者 queue
        self._ws_clients: set = set()
        self._ws_lock = threading.Lock()
        self._ws_stop = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._db_subscription: queue.Queue | None = None     # db_status_q 订阅者
        self._db_thread: threading.Thread | None = None

    def attach_monitor(self, monitor, monitor_app) -> None:
        """由 MonitorApp 在启动 ReportServer 后回调，绑定 Monitor 引用。"""
        self.monitor = monitor
        self.monitor_app = monitor_app
        if monitor is not None:
            self._live_subscription = monitor.subscribe(maxsize=500)
            self._ws_thread = threading.Thread(target=self._ws_push_loop,
                                               name="report-ws-push", daemon=True)
            self._ws_thread.start()
        if monitor_app is not None:
            self._db_subscription = monitor_app.subscribe_db(maxsize=10)
            self._db_thread = threading.Thread(target=self._db_push_loop,
                                               name="report-db-push", daemon=True)
            self._db_thread.start()
```

**WebSocket 端点注册**：在 `_Handler._dispatch` 里检测 `Upgrade: websocket` 请求，拦截 `/ws` 路径并握手：

```python
# _Handler 改造
def _dispatch(self, method):
    if self.headers.get("Upgrade", "").lower() == "websocket":
        if path == "/ws":
            self._handle_websocket()
            return
    ...  # 原有 HTTP 路由
```

WebSocket 握手校验：复用 `_token_ok`（query `k` / Cookie / Header `X-CTM-Token`），**不强制 Origin**（浏览器 WS 连接会带 Origin，但同一进程内 Tkinter 不会；空 Origin 只要 token 通过也放行，与 HTTP POST 策略一致）。

**LiveCache 更新 + WS 推送循环**：

```python
def _ws_push_loop(self):
    """100ms poll Monitor events queue → 更新 LiveCache → 广播到所有 WS clients。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while not self._ws_stop.is_set():
        try:
            while True:
                camera, kind, payload = self._live_subscription.get_nowait()
                self._update_live_cache(camera, kind, payload)
                loop.run_until_complete(
                    self._broadcast({"t": time.time(), "k": kind,
                                     "c": camera, "p": payload})
                )
        except queue.Empty:
            pass
        self._ws_stop.wait(0.1)
```

广播时用 `websockets.broadcast(clients, json_msg)`——`websockets` 库自带工具，把消息并发推送到所有连接客户端，不会阻塞单客户端慢导致其他端延迟。

**db_status_q 推送循环**（5s 刷一次，直接广播给 WS clients，前端按需消费）：

```python
def _db_push_loop(self):
    while not self._ws_stop.is_set():
        try:
            ok, size = self._db_subscription.get_nowait()
            msg = {"k": "db_status", "ok": ok, "size_mb": size,
                   "backend": self.storage.backend}
            self._broadcast_sync(msg)
        except queue.Empty:
            pass
        self._ws_stop.wait(0.5)   # db_status_q 5s 一刷，0.5s poll 足够
```

AV1 压缩状态**不进 WebSocket**，走 HTTP `/api/status` 轮询（5s 一次），数据频率低且不影响实时值精度。

### 3.4 新增 REST 端点清单

所有端点挂在 `/api/monitor/*` 路径下，遵循现有 Origin/Token/_require_local 约束：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/monitor/status` | GET 开放 | 返回数据库健康、AV1 压缩状态、相机总数/在线数、已连接 WS 客户端数 |
| GET | `/api/monitor/cameras` | GET 开放 | 返回**完整相机配置**（含 cells 列表、每 cell 数据类型/阈值/用途/通过值），复用 `_api_config` 但新增 `cells` 字段 |
| GET | `/api/monitor/cameras/<name>` | GET 开放 | 返回单相机完整配置 + 当前运行状态（connected/disconnected + 最新值快照 + 作业名） |
| GET | `/api/monitor/cameras/<name>/cells/<cell>/values` | GET 开放 | 返回历史采样 `[{ts, value, ok}]`，query `from`/`to`/`limit`（默认 1 天/5000 点），底层 `storage.range_cell_values` |
| POST | `/api/monitor/cameras` | Origin+Token | 新增相机（名称/IP/端口/用户名/密码/启用/单元格列表），返回新相机；写 `cameras.json` + 触发 `reload_config` |
| PUT | `/api/monitor/cameras/<name>` | Origin+Token+**require_local** | 更新相机完整配置；携带 `base_updated_at`，冲突返回 409；写 cameras.json + reload_config |
| DELETE | `/api/monitor/cameras/<name>` | Origin+Token+**require_local** | 删除相机；写 cameras.json + reload_config；相关 Worker 自动 stop |
| POST | `/api/monitor/cameras/<name>/connect` | **require_local** | 触发 `Monitor.start_camera(cfg)`（Worker 线程） |
| POST | `/api/monitor/cameras/<name>/disconnect` | **require_local** | 触发 `Monitor.stop_camera(name)` |
| POST | `/api/monitor/cameras/connect_all` | **require_local** | 触发 `MonitorApp.connect_all()` |
| POST | `/api/monitor/cameras/disconnect_all` | **require_local** | 触发 `MonitorApp.disconnect_all()` |
| POST | `/api/monitor/cameras/<name>/export_xlsx` | Origin+Token+**require_local** | 复用现有后台任务管理器；返回 `{"task_id": ...}`，轮询 `/api/tasks/<id>` 拿进度 |
| POST | `/api/monitor/cameras/<name>/delete_data` | Origin+Token+**require_local** | 删除该相机历史数据（可选指定单元格）；调用 `storage.delete_samples` / `storage.delete_images` |

**POST/PUT 端点的乐观锁冲突处理**：

```python
# /api/monitor/cameras/<name> PUT 示例
def _api_monitor_camera_update(self, name):
    body = self._body_json()
    current = self.monitor_app.find_camera(name)
    if current is None: raise ApiError(404, ...)
    base = body.get("base_updated_at")
    if base and current.updated_at != base:
        raise ApiError(409, "配置已被其他端更新，携带 force=true 覆盖或刷新页面")
    ...  # 合并 → save_config → reload_config
```

### 3.5 MonitorApp 对外暴露（ui.py）

Web 后端要触发 Tkinter 端操作，需要 MonitorApp 公开几个方法：

```python
class MonitorApp:
    # 新增
    def find_camera(self, name: str) -> CameraConfig | None: ...
    def reload_config(self, cameras: list[CameraConfig] | None = None) -> None:
        """Web 写 cameras.json 后回调：保存 → _rebuild_grid → stop/start 变更的 Worker。
        必须在 tk 主线程执行：调用方用 root.after(0, self._reload_config_threadsafe)。"""
        ...
    def connect_camera_by_name(self, name: str) -> None: ...
    def disconnect_camera_by_name(self, name: str) -> None: ...
    def connect_all(self) -> None: ...
    def disconnect_all(self) -> None: ...
```

Web 后端 `ReportServer` 持有 `monitor_app` 引用时，用 `root.after(0, fn)` 安全调度到主线程，避免跨线程操作 Tkinter 控件。

### 3.6 start_web_server 调用链改造

```python
# app.py main() 调用链
from cameratinymes.ui import MonitorApp
# app 启动 ReportServer 时传入 monitor + monitor_app
port = web.start_web_server(storage, cameras, settings, data_dir,
                            monitor=monitor,       # NEW
                            monitor_app=app)       # NEW
```

`MonitorApp._start_report_server` 内部把 `self.monitor` 和 `self` 一起传给 `start_web_server`。原有 `web_only.py` 独立进程启动时这两个参数都是 None，WS 端点降级返回 standalone 模式提示。

---

## 4. 前端改造

### 4.1 index.html 页签扩展

```html
<header class="topbar">
  <div class="title-row">
    <!-- NEW：数据监控（第一个 tab） -->
    <div class="tab active" data-view="monitor">数据监控</div>
    <div class="tab" data-view="report">工作台</div>
    ...
  </div>
</header>
```

在 `<main>` 里新增 `<main class="monitor hidden" id="monitorView">`，放在现有 `workbench` 之前。结构：

```
monitorView
┌─ monitor-top (状态栏：数据库健康 / AV1 压缩 / WS 连接指示灯)
├─ monitor-toolbar (连接全部 / 断开全部 / 添加相机 / 下拉选择相机→编辑)
├─ monitor-grid (多相机宫格，复用 Tkinter CameraPanel 布局)
│  ┌─ CameraCard × N (每卡：标题 tab + 连接状态点 + 单元格行 + 操作按钮)
│  └─ 单卡结构：与 CameraPanel 一致但用 DOM + CSS
└─ monitor-bottom (日志 tail，最后 200 行)
```

BeOS 风格：黄色圆角 tab、药丸按钮、白底内容区、状态点（在线 teal / 离线 dark / 错误 red / 连接中 yellow），令牌复用 `assets/07-beos.css`。

### 4.2 Chart.js 引入

在 `index.html` 里加一行（走本地资源，不 CDN）：

```html
<script src="/webui/chart.umd.min.js"></script>
```

**资源准备**：下载 Chart.js v4 UMD 压缩版（约 90KB）到 `src/cameratinymes/webui/chart.umd.min.js`。PyInstaller 打包脚本 `build_exe.py` 已配 `--add-data src/cameratinymes/webui;cameratinymes/webui`，新文件自动带上。源码运行时 `/webui/chart.umd.min.js` 被 `_Handler._static("chart.umd.min.js")` 拦截返回 304/200。

Chart.js 用于两处：
1. **实时数值面板**（每相机每单元格一个迷你折线，默认显示最近 5s/200 点 LiveCache；"展开"按钮打开历史趋势对话框）
2. **历史趋势对话框**（类似 Tkinter `ChartWindow`：选时间范围 → `GET /api/monitor/cameras/<name>/cells/<cell>/values` → Chart.js 折线，红段=超差区间，虚线=阈值基线）

### 4.3 实时数据同步（WebSocket 客户端）

```javascript
// app.js 新增模块
const WS_RECONNECT = { delay: 500, max: 30000, step: 1.5 };

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws${tokenParam()}`;
}

function connectWs() {
  const ws = new WebSocket(wsUrl());
  let backoff = WS_RECONNECT.delay;

  ws.onopen = () => { backoff = WS_RECONNECT.delay; setWsStatus(true); };
  ws.onclose = () => {
    setWsStatus(false);
    setTimeout(connectWs, backoff);
    backoff = Math.min(WS_RECONNECT.max, backoff * WS_RECONNECT.step);
  };
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    dispatchWs(msg);   // 路由：db_status → 状态栏；camera_status → 卡片；result → 单元格值
  };
}

function dispatchWs(msg) {
  if (msg.k === "db_status")      renderDbStatus(msg);
  if (msg.k === "connected")      renderCameraConnected(msg.c);
  if (msg.k === "disconnected")   renderCameraDisconnected(msg.c);
  if (msg.k === "error")          renderCameraError(msg.c, msg.p);
  if (msg.k === "result")         renderCellValues(msg.c, msg.p);   // msg.p = {cell:value}
  if (msg.k === "job")            renderCameraJob(msg.c, msg.p);
  if (msg.k === "history")        renderLiveSnapshot(msg.p);        // 新 tab 上线时推
}
```

**断线重连**：指数退避（500ms→30s），与现有 HTTP 轮询丢弃过期响应机制独立运作，互不影响。新 Tab 上线时，WS 端点会先推一条 `history` 消息（每相机每单元格最近 200 点 LiveCache 快照），让页面立即有数据可看，不用等下一个事件。

### 4.4 相机 CRUD 与单元格编辑对话框

"添加相机 / 编辑相机 / 删除相机"走同一个模态框（`openModal` 已有实现）。表单字段：

```
相机基本信息：名称 / IP / 端口 / 用户名 / 密码 / 启用勾选
单元格列表：行式表格（序号/名称/数据类型/记录模式/采样模式/上下限/单位/用途/通过值/不通过值/删除/上下移）
底部按钮：取消 / 保存（携带 base_updated_at，冲突弹 409 确认覆盖）
```

行编辑器用 HTML5 `<input>` / `<select>`，与现有报告草稿/工单编辑器风格一致。每单元格的 `role` / `pass_values` / `fail_values` 从后端 `_api_monitor_cameras/<name>` 拉全量配置，避免前端猜。

### 4.5 实时单元格值的迷你折线

每相机每单元格卡片内嵌一个 Chart.js 迷你折线（宽 140px × 高 36px，`pointRadius: 0`，`borderWidth: 1.5`，`ticks: { display: false }`），数据来自 WS 推送的 `result` 事件，每收到新点就 `chart.data.labels.push(ts_ms); chart.data.datasets[0].data.push(value);`，长度超过 200 时 `shift()`。红色超差区间通过 `segment` 配置：

```javascript
plugins: {
  tooltip: { enabled: false },
  legend:  { display: false }
},
scales: { x: { display: false }, y: { display: false } },
datasets: [{
  borderColor: (ctx) => {
    const v = ctx.parsed.y;
    return v < lower || v > upper ? "#ff3333" : "#00cc88";
  },
  borderWidth: 1.5, pointRadius: 0, tension: 0.35,
}],
```

### 4.6 启动时加载状态

复用现有"延迟 150ms 进度条 + 首屏骨架占位 + 请求序号丢弃过期响应"机制。WS 连接状态单独显示一个指示灯在 `monitor-top`：绿=连接中，红=断开（显示重连倒计时），灰=standalone 模式。

---

## 5. 接口契约（JSON）

### 5.1 WS 消息格式

```json
// result 事件（最频繁）
{"t": 1762906656.123, "k": "result", "c": "cam55", "p": {"A1": 9.84, "C11": "OK"}}

// 连接状态
{"t": 1762906656.1, "k": "connected", "c": "cam145"}
{"t": 1762906657.2, "k": "error", "c": "cam145", "p": "websocket handshake failed"}

// 作业切换
{"t": 1762906658.3, "k": "job", "c": "cam55", "p": "myjob.jobx"}

// 新客户端上线快照（一次性推，每相机每单元格最近 200 点）
{"t": 1762906660, "k": "history", "p": {
  "cam55": {
    "A1": {"ts": [["2026-11-13T09:01:00", 9.84, null], ...]},
    "C11": {"ts": [["2026-11-13T09:01:00", null, 1], ...]}
  }
}}

// 数据库健康（每 5s）
{"t": 1762906665, "k": "db_status", "ok": true, "size_mb": 164.2, "backend": "sqlite"}
```

### 5.2 HTTP `/api/monitor/status`

```json
{"db": {"ok": true, "size_mb": 164.2, "backend": "sqlite"},
 "av1": {"runs": 3, "frames": 90, "orig_mb": 12.4, "new_mb": 2.1, "ratio": 5.9, "saved_mb": 10.3},
 "cameras_total": 24,
 "cameras_online": 18,
 "ws_clients": 3,
 "web_token_required": false}
```

### 5.3 HTTP `/api/monitor/cameras/<name>/cells/<cell>/values`

query `from=2026-11-13T09:00:00&to=2026-11-13T10:00:00&limit=5000`

```json
{"camera": "cam55", "cell": "A1", "from": "2026-11-13T09:00:00", "to": "2026-11-13T10:00:00",
 "points": [["2026-11-13T09:00:01.123", 9.84, 1], ["2026-11-13T09:00:02.456", 10.02, 1], ...],
 "lower_limit": 9.5, "upper_limit": 10.5}
```

### 5.4 HTTP POST `/api/monitor/cameras`

```json
{"name": "cam_new", "host": "192.168.10.200", "port": 80, "enabled": true,
 "username": "", "password": "",
 "cells": [
   {"name": "A0", "data_type": "image", "record_mode": "simple", "sample_mode": "event",
    "role": "image", "pass_values": [], "fail_values": [], "note": "图片"},
   {"name": "C11", "data_type": "string", "record_mode": "simple", "sample_mode": "event",
    "role": "result", "pass_values": ["OK"], "fail_values": ["NG"]},
   {"name": "A1", "data_type": "float", "record_mode": "advanced", "sample_mode": "event",
    "lower": 9.5, "upper": 10.5, "unit": "mm", "role": "data"}
 ]}
```

响应：`{"name": "cam_new", "updated_at": "2026-11-13T10:15:30", "reload": true}`（`reload=true` 表示 Tkinter 已收到重载回调）

### 5.5 HTTP PUT `/api/monitor/cameras/<name>`

请求体同上，额外加 `"base_updated_at": "2026-11-13T10:15:30"`。冲突返回 `409 {"error": "stale", "current": "...", "force_url": "..."}`。

---

## 6. 安全与权限

所有新增端点遵循现有 web.py 权限矩阵：

| 端点 | 方法 | Origin 校验 | Token | require_local |
|---|---|---|---|---|
| `/api/monitor/status` | GET | — | — | — |
| `/api/monitor/cameras` | GET | — | — | — |
| `/api/monitor/cameras/<name>` | GET | — | — | — |
| `/api/monitor/.../values` | GET | — | — | — |
| `/ws` | WS Upgrade | 本机或局域网 IP 必须匹配；空 Origin + 合法 Token 放行 | query `k=` / Cookie / Header | — |
| `/api/monitor/cameras` | POST | ✅ POST 标准 | ✅ | — |
| `/api/monitor/cameras/<name>` | PUT | ✅ PUT 标准 | ✅ | ✅ 强制仅本机 |
| `/api/monitor/cameras/<name>` | DELETE | ✅ DELETE 标准 | ✅ | ✅ 强制仅本机 |
| `/api/monitor/cameras/<name>/connect` | POST | ✅ | ✅ | ✅ 强制仅本机 |
| `/api/monitor/cameras/<name>/disconnect` | POST | ✅ | ✅ | ✅ 强制仅本机 |
| `/api/monitor/cameras/connect_all` | POST | ✅ | ✅ | ✅ 强制仅本机 |
| `/api/monitor/cameras/disconnect_all` | POST | ✅ | ✅ | ✅ 强制仅本机 |
| `/api/monitor/cameras/<name>/export_xlsx` | POST | ✅ | ✅ | ✅ 强制仅本机（防远程触发大文件导出） |
| `/api/monitor/cameras/<name>/delete_data` | POST | ✅ | ✅ | ✅ 强制仅本机 |

**设计原则**：
- GET / 历史查询类端点开放（局域网其他电脑只读访问没问题）
- 任何**改变相机配置 / 连接状态 / 触发数据库写入**的 POST/PUT/DELETE 都 **Origin+Token** 双校验
- 任何**会阻塞 Worker 或产生重副作用**的操作（连接/断开/导出/删除数据）叠加 `_require_local`——只有本机回环地址能调，防止远程工控机误操作同一台相机

---

## 7. 实施步骤（分批上线，每步可验证）

### 第 1 批 —— 后端基础 + 实时推送（核心）

| # | 文件 | 改动 | 验证 |
|---|---|---|---|
| 1 | `src/cameratinymes/worker.py` | `Monitor.events` 从单条 queue → `list[queue.Queue]` 多订阅者；新增 `subscribe()` | 启动后 Tkinter 实时值不丢事件（与改前一致） |
| 2 | `src/cameratinymes/ui.py` | `db_status_q` 多订阅者；`MonitorApp.subscribe_db()`；新增 `find_camera / reload_config` 等公开方法 | 连接状态 / 数据库状态正常，`reload_config` 后相机面板重建且 Worker 重启 |
| 3 | `src/cameratinymes/storage.py` | 新增 `StorageBase.range_cell_values` 基类实现 | SQLite + MariaDB 两端都能返回相同格式的逐点数据 |
| 4 | `src/cameratinymes/web.py` | ReportServer 新增 `monitor / monitor_app` 参数；WS 端点 `/ws` + LiveCache + WS push loop；`/api/monitor/status` + `/api/monitor/cameras`（含 cells 字段） | 浏览器打开 `/ws` 能收到 `result` / `connected` / `disconnected` 事件；LiveCache 近 200 点可拉 |

**验收点**：Worker 每秒事件数 × 队列 2 条 × 1000 容量 → 即使 Web 端慢（浏览器卡顿、Tab 后台），Tkinter 端零丢事件；WebSocket 3 个 Tab 同时连接不互相阻塞。

### 第 2 批 —— 前端"数据监控"页签 + Chart.js 迷你折线

| # | 文件 | 改动 | 验证 |
|---|---|---|---|
| 5 | `src/cameratinymes/webui/chart.umd.min.js` | 下载 Chart.js v4 UMD 压缩版（约 90KB）放到此路径 | `build_exe.py` 打包产物内有此文件，运行时浏览器 F12 能加载 |
| 6 | `src/cameratinymes/webui/index.html` | 新增 monitor 页签 tab + `<main id="monitorView">` 骨架 | 切到 monitor 页签能看到空状态 UI |
| 7 | `src/cameratinymes/webui/app.js` | WS 客户端 + `dispatchWs` 路由 + 相机卡片 DOM 渲染 + 迷你折线 Chart.js + 实时单元格值更新 | WS 指示灯变绿；相机卡片随 WS 事件显示/变值；迷你折线每秒刷一个新点 |
| 8 | `src/cameratinymes/webui/style.css` | monitor 页签 BeOS 风格（复用令牌） + 卡片响应式网格 | 12 台相机 3 列布局，1920×1080 窗口无水平滚动 |

**验收点**：WS 连接 → 3s 内所有已连接相机的单元格值都有显示；断开重连不闪屏（指数退避）；迷你折线在无新点时保持最后状态不抖动。

### 第 3 批 —— 相机 CRUD + 单元格订阅编辑（Web 端配置热重载）

| # | 文件 | 改动 | 验证 |
|---|---|---|---|
| 9 | `src/cameratinymes/web.py` | 新增相机 CRUD 端点（GET/POST/PUT/DELETE `/api/monitor/cameras*`）+ 乐观锁冲突处理 | POST 新相机 → cameras.json 更新 → Tkinter 面板重建 → Worker 重连 → 新相机 WS 事件到达 |
| 10 | `src/cameratinymes/web.py` | 新增连接状态端点（POST `/api/monitor/cameras/<name>/connect|disconnect|connect_all|disconnect_all`） | 远程浏览器点"连接全部" → 本机 WS 推 connected 事件 → 远程浏览器和 Tkinter 都看到状态变化 |
| 11 | `src/cameratinymes/web.py` | 新增数据操作端点（POST export_xlsx / delete_data，复用 tasks 队列） | 导出任务在 Web 后台任务面板可见，进度条 OK |
| 12 | `src/cameratinymes/webui/app.js` | 相机 CRUD 模态框 + 单元格行编辑器 + 保存按钮（携带 `base_updated_at`，409 弹确认覆盖） | 同一相机 Tkinter 和 Web 同时编辑 → 后保存的一方收到 409 → 点"覆盖"强制写入 |

**验收点**：POST 新相机 → cameras.json mtime 更新 → Tkinter 3s 内（`reload_config` 内已做同步 save_config + Worker stop/start）看到新面板；编辑单元格 → Worker session 按新 cell 列表重开；乐观锁冲突时 Tkinter 操作后 Web 再编辑 → Web 收到 409 并强制覆盖后两边一致。

### 第 4 批 —— 历史趋势对话框 + OK/NG 率面板（复用 OKNG 模块）

| # | 文件 | 改动 | 验证 |
|---|---|---|---|
| 13 | `src/cameratinymes/webui/app.js` | Chart.js 历史趋势对话框（选时间范围 → `GET /api/monitor/.../values` → 折线 + 红段=超差 + 虚线=阈值基线） | 选今天 0-12 点 → 折线图完整显示，超差点红，阈值基线可见 |
| 14 | 新增"数据监控"页签内置 OK/NG 率小面板（复用 `okng.hourly_okng`），与 OK/NG 率独立页签数据同源 | 同一天同相机 → 两个面板 OK/NG 数一致 |

### 第 5 批 —— 打包验证 + 文档

| # | 文件 | 改动 | 验证 |
|---|---|---|---|
| 15 | `build_exe.py` | 确认 `--add-data src/cameratinymes/webui;cameratinymes/webui` 已覆盖 chart.umd.min.js | 打包产物 dist/ 下启动 exe → 数据监控页签正常 → Chart.js 正常 → WS 正常 |
| 16 | `README.md` | 新增"数据监控 Web"章节，列出新端点、WS 协议、使用方式 | — |
| 17 | `docs/` | 本实施方案文档归档 | — |

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Monitor.events 多订阅者后，任一订阅者 queue 满了丢事件 | Web 端偶发丢实时值（浏览器卡顿、Tab 后台） | LiveCache 兜底：新事件上线时推 history 快照；满队列只丢该订阅者，不影响 Tkinter 和其他订阅者 |
| WebSocket 线程（`_ws_push_loop`）100ms poll queue，高频事件下 CPU 占用上升 | 多相机 × 多结果事件 → 队列每秒几千条 → poll 循环高频 | 500 queue 容量足够几秒缓冲；`get_nowait` 只 poll 一次（不 while True 扫完），控制单循环耗时；可后续优化为 `threading.Condition` 阻塞等待 |
| Web 编辑配置 → Tkinter reload_config() 耗时（重建面板 + Worker stop/start） | 配置变更后 3-5s 内 Tkinter 短暂显示空白 | reload_config 在 `root.after(0, ...)` 里执行，Tkinter 保持响应；rebuild_grid 已做状态快照，快速切换不闪烁 |
| MariaDB 大时间范围查 `range_cell_values` 可能超时 | 图表加载卡住 | 硬性 `LIMIT 5000`；SQL 走 `idx_samples_cam_cell` 索引；前端按小时分段按需拉取（默认 1 天） |
| `web_only.py` 独立进程无 Monitor 引用，WS 端点返回 `standalone` | 独立部署场景下 Web 端无实时值 | 明确说明文档；独立部署场景仅提供历史查询（数据监控页签可见但实时状态显示"standalone 模式，仅历史数据"） |
| 恶意客户端高频 WS 连接消耗内存 | 极端场景下大量 WS 客户端 → WebSocket 库的客户端集合无限增长 | ReportServer.ws_clients 加上限（默认 50），超出拒绝握手；每个客户端消息频率限速（websockets 库 `max_queue=1000`） |
| `_require_local` + Origin 校验可能误伤合法的局域网远程工控机操作 | 远程工控机想"添加相机"但 PUT 被 403 | 文档明确说明"添加/编辑/删除相机、连接/断开相机、触发导出/删除数据"仅限本机；远程工控机只做**只读监控**，配置变更回桌面端执行 |

---

## 9. 与现有模块的复用关系

| 功能 | 复用模块 | 复用方式 |
|---|---|---|
| OK/NG 判定规则 | `config.result_specs` + `okng.hourly_okng` | 直接调用，不重复实现判定逻辑 |
| 历史采样查询 | `storage` 基类新增 `range_cell_values` | 双实现共享，SQLite/MariaDB 两端继承 |
| 乐观锁机制 | `report_drafts` 的 `updated_at` 模式 | 相机配置加 `updated_at` 字段，Web PUT 携带 `base_updated_at` |
| 后台任务 / 进度条 | `tasks.py` TaskManager | 导出 xlsx / 删除数据走现有异步队列 |
| 图像缩略图缓存 | `_FrameCache` 改造 | LiveCache 复用 OrderedDict LRU 模式 |
| BeOS 风格令牌 | `assets/07-beos.css` | 数据监控页签直接用现有 CSS 类 |
| 打包 | `build_exe.py` `--add-data` | chart.umd.min.js 放 webui/ 目录自动带上 |

---

## 10. 附录：现有模块关键入口速查

- Monitor.events push 入口：`worker.py CogSocketClient._run()` → `_open_and_collect()` → `on_result()` / `_poll_loop()` 里 `self._push("result", {...})`
- Monitor.events 消费（Tkinter 端）：`ui.py MonitorApp._poll_events()` 每 100ms，路由到 CameraPanel.set_status / set_value
- db_status_q push：`ui.py MonitorApp._start_db_monitor()` 后台线程 5s poll，`_db_status_q.put((ok, size))`
- Web 后端入口：`web.py start_web_server(storage, cameras, settings, data_dir, ...)`
- Chart.js v4 UMD 压缩版下载：https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js （下载后拷到 `src/cameratinymes/webui/`）
- PyInstaller 打包脚本：`build_exe.py` 已配 `--add-data src/cameratinymes/webui;cameratinymes/webui`，新资源自动带上
