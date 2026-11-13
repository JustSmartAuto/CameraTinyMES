"""配置与数据目录管理。

默认数据目录：D:/JustSmart/CameraTinyMES（D 盘不可用则回退 ~/AppData/CameraTinyMES），
启动时自动递归创建目录（xlsx / charts / db 子目录）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

APP_NAME = "CameraTinyMES"
SUBDIRS = ("xlsx", "charts", "db", "config")


def default_data_dir() -> Path:
    """默认数据目录：优先 D:/JustSmart，失败回退用户 AppData。"""
    candidates = [Path("D:/") / "JustSmart" / APP_NAME, Path.home() / "AppData" / APP_NAME]
    for base in candidates:
        try:
            ensure_subdirs(base)
            return base
        except OSError:
            continue
    raise RuntimeError("无法创建数据目录（D:/JustSmart 与 AppData 均不可用）")


def ensure_subdirs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)


# 单元格数据类型：设置时通常未知，用 auto 让运行时按值推断
CELL_DATA_TYPES = ("auto", "int", "float", "string", "bool", "image", "pointcloud", "other")
# 仅数值类型应用上下限阈值；auto 在运行时尝试按数值解析
NUMERIC_DATA_TYPES = {"auto", "int", "float"}
# 图像类型：从相机下载图像（结果图单元格额外取渲染图），不参与阈值判定
IMAGE_DATA_TYPE = "image"
# 点云类型：从相机下载 CPCD 点云（acqPointCloudView），不参与阈值判定
POINTCLOUD_DATA_TYPE = "pointcloud"
# 单元格用途：auto=按名称推断；data=普通数据；result=判定结果；image=报告图片
CELL_ROLES = ("auto", "data", "result", "image")
DEFAULT_RESULT_CELL = "C11"
DEFAULT_IMAGE_CELL = "A0"
DEFAULT_PASS_VALUES = ("OK",)
DEFAULT_FAIL_VALUES = ("NG",)

# 记录模式：simple=仅记录数据不做阈值判定；advanced=记录并按上下限判定 OK/NG
CELL_RECORD_MODES = ("simple", "advanced")


@dataclass
class CellConfig:
    """单个订阅单元格。"""

    name: str  # 单元格名或地址，如 "A2" / "Measure.Width"
    data_type: str = "auto"  # auto/int/float/string/bool/image/pointcloud/other；auto=设置时未知，运行时推断
    record_mode: str = "advanced"  # simple=仅记录；advanced=记录+阈值判定
    sample_mode: str = "event"  # event=按结果事件采样；poll=按固定周期轮询
    poll_interval: float = 1.0  # 轮询周期（秒），仅 poll 模式
    lower: float | None = None  # 阈值下限（仅 advanced 模式 + 数值类型有效），None 不判
    upper: float | None = None  # 阈值上限（仅 advanced 模式 + 数值类型有效）
    unit: str = ""  # 单位标注
    note: str = ""  # 注解：说明该单元格数值的业务含义（如 "焊缝宽度/mm"）
    role: str = "auto"  # auto=按名称推断；data=普通数据；result=判定结果；image=报告图片
    pass_values: list[str] = field(default_factory=list)  # 结果单元格中代表通过的文本，空=OK
    fail_values: list[str] = field(default_factory=list)  # 结果单元格中代表不通过的文本，空=NG

    @property
    def is_numeric(self) -> bool:
        """是否为数值类型（上下限阈值仅对此类生效）。"""
        return self.data_type in NUMERIC_DATA_TYPES

    @property
    def is_simple_record(self) -> bool:
        """是否为简单记录模式（不做阈值判定）。"""
        return self.record_mode == "simple"

    @property
    def is_image(self) -> bool:
        """是否为图像类型（从相机下载图像，不限上下限）。"""
        return self.data_type == IMAGE_DATA_TYPE

    @property
    def is_pointcloud(self) -> bool:
        """是否为点云类型（从相机下载 CPCD 点云，不限上下限）。"""
        return self.data_type == POINTCLOUD_DATA_TYPE

    @property
    def is_media(self) -> bool:
        """是否为图像 / 点云等媒体类型（随结果事件采样，不参与阈值判定）。"""
        return self.is_image or self.is_pointcloud

    @property
    def is_result_cell(self) -> bool:
        """是否被显式设置为判定结果单元格。"""
        return self.role == "result"


@dataclass
class CameraConfig:
    """单台相机配置。"""

    name: str
    host: str
    port: int = 80
    username: str = "admin"
    password: str = ""
    cells: list[CellConfig] = field(default_factory=list)
    reconnect_interval: float = 5.0
    enabled: bool = True
    station: str = ""       # 工站号（OP10）；空则按相机名前缀推断
    station_name: str = ""  # 工站名（管壳合件组装）；报告 C 列第二行
    model: str = ""         # 机型/产品型号；报告 B 列
    updated_at: str = ""    # 上次更新时间（ISO 字符串），用于 Web 端乐观锁

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"


def _norm_cell_name(name: str) -> str:
    return str(name).strip().upper().replace("$", "")


def _token_list(value) -> list[str]:
    """把 "OK, GOOD" / ["OK", "GOOD"] 统一成大写去重列表。"""
    if isinstance(value, str):
        parts = re.split(r"[,，、/\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = []
    tokens: list[str] = []
    for part in parts:
        token = part.strip().upper()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def result_cell(camera: CameraConfig) -> CellConfig | None:
    """解析相机的判定结果单元格：显式 role=result 优先，否则按名称 C11 推断。"""
    explicit = [c for c in camera.cells if c.is_result_cell]
    if explicit:
        return explicit[0]
    for cell in camera.cells:
        if _norm_cell_name(cell.name) == DEFAULT_RESULT_CELL:
            return cell
    return None


def extra_result_cells(camera: CameraConfig) -> list[CellConfig]:
    """显式配置了多个结果单元格时返回多余的（用于告警）。"""
    return [c for c in camera.cells if c.is_result_cell][1:]


def image_cell(camera: CameraConfig) -> CellConfig | None:
    """解析相机的报告图片单元格：显式 role=image 优先，否则首个图像单元格，再退回 A0。"""
    explicit = [c for c in camera.cells if c.role == "image"]
    if explicit:
        return explicit[0]
    for cell in camera.cells:
        if cell.is_image:
            return cell
    for cell in camera.cells:
        if _norm_cell_name(cell.name) == DEFAULT_IMAGE_CELL:
            return cell
    return None


def result_tokens(cell: CellConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """结果单元格的（通过值, 不通过值），未配置时回退 OK / NG。"""
    passes = tuple(_token_list(cell.pass_values)) or DEFAULT_PASS_VALUES
    fails = tuple(_token_list(cell.fail_values)) or DEFAULT_FAIL_VALUES
    return passes, fails


def result_specs(cameras: list[CameraConfig]) -> list[tuple[CameraConfig, CellConfig, tuple[str, ...], tuple[str, ...]]]:
    """每台相机的结果规则：(相机, 结果单元格, 通过值, 不通过值)，无结果单元格的相机跳过。"""
    specs = []
    for camera in cameras:
        cell = result_cell(camera)
        if cell is None:
            continue
        passes, fails = result_tokens(cell)
        specs.append((camera, cell, passes, fails))
    return specs


def config_path(data_dir: Path) -> Path:
    return data_dir / "config" / "cameras.json"


def load_config(data_dir: Path) -> list[CameraConfig]:
    path = config_path(data_dir)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    cameras = []
    for item in data:
        item = dict(item)
        cells = []
        for raw in item.get("cells", []):
            raw = dict(raw)
            raw["role"] = raw.get("role") if raw.get("role") in CELL_ROLES else "auto"
            raw["pass_values"] = _token_list(raw.get("pass_values"))
            raw["fail_values"] = _token_list(raw.get("fail_values"))
            cells.append(CellConfig(**raw))
        item["cells"] = cells
        cameras.append(CameraConfig(**item))
    return cameras


def save_config(data_dir: Path, cameras: list[CameraConfig]) -> None:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now().isoformat(timespec="seconds")
    data = []
    for cam in cameras:
        cam.updated_at = now_iso        # 每次保存都 bump，Web 端据此做乐观锁
        item = asdict(cam)
        # 新字段为默认值时省略，旧版程序读取时不会因未知字段报错
        for cell in item.get("cells", []):
            if cell.get("role", "auto") == "auto":
                cell.pop("role", None)
            if not cell.get("pass_values"):
                cell.pop("pass_values", None)
            if not cell.get("fail_values"):
                cell.pop("fail_values", None)
        data.append(item)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 数据库连接配置 ----------

# 与 reference/mariadb_installer.py 的安装参数保持一致
DEFAULT_MARIADB_HOST = "127.0.0.1"
DEFAULT_MARIADB_PORT = 8306
DEFAULT_MARIADB_USER = "justsmart"
DEFAULT_MARIADB_PASSWORD = "justsmart"
DEFAULT_MARIADB_DATABASE = "camera_tiny_mes"
DEFAULT_SQLITE_PATH = "db/cell_monitor.db"


@dataclass
class MariaDBConfig:
    """MariaDB 连接参数。"""

    host: str = DEFAULT_MARIADB_HOST
    port: int = DEFAULT_MARIADB_PORT
    user: str = DEFAULT_MARIADB_USER
    password: str = DEFAULT_MARIADB_PASSWORD
    database: str = DEFAULT_MARIADB_DATABASE
    connect_timeout: int = 3


@dataclass
class DatabaseConfig:
    """存储后端选择：auto=MariaDB 优先、失败回退 SQLite。"""

    backend: str = "auto"  # auto / mariadb / sqlite
    mariadb: MariaDBConfig = field(default_factory=MariaDBConfig)
    sqlite_path: str = DEFAULT_SQLITE_PATH


def database_config_path(data_dir: Path) -> Path:
    return data_dir / "config" / "database.json"


def load_database_config(data_dir: Path) -> DatabaseConfig:
    """读取 config/database.json；文件缺失或损坏时使用默认值（auto）。"""
    path = database_config_path(data_dir)
    if not path.exists():
        return DatabaseConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DatabaseConfig()
    mariadb = raw.get("mariadb") or {}
    sqlite = raw.get("sqlite") or {}
    return DatabaseConfig(
        backend=str(raw.get("db_backend", "auto")).lower(),
        mariadb=MariaDBConfig(
            host=str(mariadb.get("host", DEFAULT_MARIADB_HOST)),
            port=int(mariadb.get("port", DEFAULT_MARIADB_PORT)),
            user=str(mariadb.get("user", DEFAULT_MARIADB_USER)),
            password=str(mariadb.get("password", DEFAULT_MARIADB_PASSWORD)),
            database=str(mariadb.get("database", DEFAULT_MARIADB_DATABASE)),
            connect_timeout=int(mariadb.get("connect_timeout", 3)),
        ),
        sqlite_path=str(sqlite.get("path", DEFAULT_SQLITE_PATH)),
    )


# ---------- 应用设置（托盘 / 开机自启）----------

@dataclass
class AppSettings:
    """应用行为设置（持久化到 config/settings.json）。"""

    minimize_to_tray_on_start: bool = False  # 启动时是否最小化到托盘
    autostart_enabled: bool = False          # 开机自启（shell:startup 快捷方式）
    # 改善报告抬头默认值（报告页可逐次覆盖）
    project_name: str = ""
    destination: str = ""
    line_name: str = ""
    # 改善报告工作台（内嵌 Web）
    web_host: str = "0.0.0.0"   # 0.0.0.0=局域网可访问；127.0.0.1=仅本机
    web_port: int = 18110       # 被占用时启动时自动顺延并写回
    web_token: str = ""         # 访问口令；空=关闭


def settings_path(data_dir: Path) -> Path:
    return data_dir / "config" / "settings.json"


def load_settings(data_dir: Path) -> AppSettings:
    path = settings_path(data_dir)
    if not path.exists():
        return AppSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    return AppSettings(
        minimize_to_tray_on_start=bool(raw.get("minimize_to_tray_on_start", False)),
        autostart_enabled=bool(raw.get("autostart_enabled", False)),
        project_name=str(raw.get("project_name", "")),
        destination=str(raw.get("destination", "")),
        line_name=str(raw.get("line_name", "")),
        web_host=str(raw.get("web_host", "0.0.0.0") or "0.0.0.0"),
        web_port=int(raw.get("web_port", 18110) or 18110),
        web_token=str(raw.get("web_token", "")),
    )


def save_settings(data_dir: Path, settings: AppSettings) -> None:
    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------- 改善报告配置 ----------

@dataclass
class ReportConfig:
    """改善报告选项（持久化到 config/report.json，缺省即默认值）。

    工作台的监听地址/端口/口令放在 settings.json 的 web_host/web_port/web_token
    （桌面端设置界面可改），这里只放报告生成相关项，避免两套真值。
    """

    pdf_engine: str = "reportlab"         # reportlab | com
    ng_images_per_station: int = 100      # 每工站/每相机图片上限（1~100）
    image_max_edge: int = 800             # 嵌入/绘制图片长边上限（px）
    reviewer: str = ""                    # 默认审核人/编制人
    op_aggregate: str = "median"          # 数量聚合：median | max | first
    report_keep_days: int = 90            # 存档保留天数（0=不限）
    report_keep_count: int = 200          # 存档保留条数（0=不限）
    overlay_annotations: bool = False     # 报告图片是否叠加人工标注框


def report_config_path(data_dir: Path) -> Path:
    return data_dir / "config" / "report.json"


def load_report_config(data_dir: Path) -> ReportConfig:
    """读取 config/report.json；缺失或字段非法时用默认值。"""
    path = report_config_path(data_dir)
    if not path.exists():
        return ReportConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ReportConfig()
    if not isinstance(raw, dict):
        return ReportConfig()
    return ReportConfig(
        pdf_engine=str(raw.get("pdf_engine", "reportlab")).lower(),
        ng_images_per_station=max(1, min(100, int(raw.get("ng_images_per_station", 100)))),
        image_max_edge=max(200, min(2000, int(raw.get("image_max_edge", 800)))),
        reviewer=str(raw.get("reviewer", "")),
        op_aggregate=str(raw.get("op_aggregate", "median")).lower(),
        report_keep_days=max(0, int(raw.get("report_keep_days", 90))),
        report_keep_count=max(0, int(raw.get("report_keep_count", 200))),
        overlay_annotations=bool(raw.get("overlay_annotations", False)),
    )


def save_report_config(data_dir: Path, config: ReportConfig) -> None:
    path = report_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
