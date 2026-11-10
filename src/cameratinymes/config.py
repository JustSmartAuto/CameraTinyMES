"""配置与数据目录管理。

默认数据目录：D:/JustSmart/CameraTinyMES（D 盘不可用则回退 ~/AppData/CameraTinyMES），
启动时自动递归创建目录（xlsx / charts / db 子目录）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
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
CELL_DATA_TYPES = ("auto", "int", "float", "string", "bool", "image", "other")
# 仅数值类型应用上下限阈值；auto 在运行时尝试按数值解析
NUMERIC_DATA_TYPES = {"auto", "int", "float"}
# 图像类型：从相机下载图像（A0 额外取结果渲染图），不参与阈值判定
IMAGE_DATA_TYPE = "image"

# 记录模式：simple=仅记录数据不做阈值判定；advanced=记录并按上下限判定 OK/NG
CELL_RECORD_MODES = ("simple", "advanced")


@dataclass
class CellConfig:
    """单个订阅单元格。"""

    name: str  # 单元格名或地址，如 "A2" / "Measure.Width"
    data_type: str = "auto"  # auto/int/float/string/bool/other；auto=设置时未知，运行时推断
    record_mode: str = "advanced"  # simple=仅记录；advanced=记录+阈值判定
    sample_mode: str = "event"  # event=按结果事件采样；poll=按固定周期轮询
    poll_interval: float = 1.0  # 轮询周期（秒），仅 poll 模式
    lower: float | None = None  # 阈值下限（仅 advanced 模式 + 数值类型有效），None 不判
    upper: float | None = None  # 阈值上限（仅 advanced 模式 + 数值类型有效）
    unit: str = ""  # 单位标注
    note: str = ""  # 注解：说明该单元格数值的业务含义（如 "焊缝宽度/mm"）

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

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"


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
        item["cells"] = [CellConfig(**c) for c in item.get("cells", [])]
        cameras.append(CameraConfig(**item))
    return cameras


def save_config(data_dir: Path, cameras: list[CameraConfig]) -> None:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for cam in cameras:
        item = asdict(cam)
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

    image_embed: str = "dispimg"          # dispimg=WPS 嵌入单元格；floating=浮动图片
    pdf_engine: str = "reportlab"         # reportlab | com
    ng_images_per_station: int = 6        # 每工站 NG 图片上限（1~8）
    image_max_edge: int = 800             # 嵌入/绘制图片长边上限（px）
    reviewer: str = ""                    # 默认审核人/编制人
    op_aggregate: str = "median"          # 数量聚合：median | max | first
    report_keep_days: int = 90            # 存档保留天数（0=不限）
    report_keep_count: int = 200          # 存档保留条数（0=不限）


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
        image_embed=str(raw.get("image_embed", "dispimg")).lower(),
        pdf_engine=str(raw.get("pdf_engine", "reportlab")).lower(),
        ng_images_per_station=max(1, min(8, int(raw.get("ng_images_per_station", 6)))),
        image_max_edge=max(200, min(2000, int(raw.get("image_max_edge", 800)))),
        reviewer=str(raw.get("reviewer", "")),
        op_aggregate=str(raw.get("op_aggregate", "median")).lower(),
        report_keep_days=max(0, int(raw.get("report_keep_days", 90))),
        report_keep_count=max(0, int(raw.get("report_keep_count", 200))),
    )


def save_report_config(data_dir: Path, config: ReportConfig) -> None:
    path = report_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
