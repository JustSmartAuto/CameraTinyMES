"""SQLite / MariaDB 双后端落库 + xlsx 导出。

MariaDB 优先（支持多客户端汇聚），连接不可用时由 `open_storage` 自动回退 SQLite；
两套后端共用同一套表结构与数据访问接口，时间戳统一存 ISO8601 字符串（毫秒）。
xlsx 按 相机/日期/单元格 导出工作表，文件名带时间戳后缀。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .config import DatabaseConfig, MariaDBConfig, load_database_config

log = logging.getLogger(__name__)

# 数据库 schema 版本：规则与软件版本一致（YY.MM.DD.N，N=当日第几次 DDL 变更）
SCHEMA_VERSION = "26.11.10.1"

# 报告快速 tag / 工单枚举（docs/当日改善报告功能设计.md §3.3、§4.8）
REPORT_TAGS = ("misjudge", "product", "external")
TICKET_STATUSES = ("open", "in_progress", "done", "closed")
TICKET_CLOSE_REASONS = ("duplicate", "wontfix", "invalid", "fixed")
TICKET_CATEGORIES = ("misjudge", "product", "external", "other")
TICKET_PRIORITIES = ("P0", "P1", "P2", "P3")
# 状态机：open→in_progress→done→closed；任意状态可 reopen→open
TICKET_TRANSITIONS = {
    "open": {"in_progress", "closed"},
    "in_progress": {"done", "closed"},
    "done": {"closed", "in_progress"},
    "closed": {"open"},
}


class ConflictError(Exception):
    """乐观锁冲突：草稿已被其他客户端更新。"""


def version_tuple(value: str) -> tuple[int, int, int, int]:
    """四段版本号 → 整数元组；禁止用字符串比较（"26.11.9.30" > "26.11.10.2" 会误判）。"""
    parts = []
    for piece in str(value or "").strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    parts = (parts + [0, 0, 0, 0])[:4]
    return parts[0], parts[1], parts[2], parts[3]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _ranges_where(ranges: list[tuple[str, str]] | None,
                  column: str = "ts") -> tuple[str, tuple]:
    """半开区间 [(start, end), ...] → SQL 条件；空列表表示不限时段。"""
    ranges = [r for r in (ranges or []) if r and r[0] and r[1]]
    if not ranges:
        return "", ()
    clause = "(" + " OR ".join(f"({column}>=? AND {column}<?)" for _ in ranges) + ")"
    return clause, tuple(v for r in ranges for v in r)


# 结果单元格判定：优先 samples.ok；历史数据与文本结果未写 ok 时，按配置的通过/不通过文本兜底
DEFAULT_PASS_VALUES = ("OK",)
DEFAULT_FAIL_VALUES = ("NG",)


def _result_status(pass_values=DEFAULT_PASS_VALUES,
                   fail_values=DEFAULT_FAIL_VALUES) -> tuple[str, tuple]:
    """生成判定状态 SQL 表达式（1=OK，0=NG，NULL=未判定）及对应参数。"""
    parts = ["CASE WHEN ok IS NOT NULL THEN ok"]
    args: list[str] = []
    fails = tuple(fail_values or ())
    passes = tuple(pass_values or ())
    if fails:
        parts.append(f" WHEN UPPER(TRIM(text)) IN ({', '.join(['?'] * len(fails))}) THEN 0")
        args.extend(fails)
    if passes:
        parts.append(f" WHEN UPPER(TRIM(text)) IN ({', '.join(['?'] * len(passes))}) THEN 1")
        args.extend(passes)
    parts.append(" END")
    return "".join(parts), tuple(args)


_REPORT_COLUMNS = (
    "id", "day", "hours", "title", "filename", "xlsx_size", "pdf_size",
    "xlsx_sha256", "pdf_sha256", "stations", "products", "ng", "payload_json",
    "created_at", "created_by", "saved_xlsx_path", "saved_pdf_path", "saved_at",
)

_TICKET_COLUMNS = (
    "id", "title", "body", "measure", "status", "close_reason", "priority",
    "category", "station", "camera", "assignee", "due_date", "created_by",
    "created_at", "updated_at", "closed_at",
)


def _loads(text) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {"value": value}
    except (json.JSONDecodeError, TypeError):
        return {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,               -- ISO8601 本地时间
    camera TEXT NOT NULL,           -- 相机名
    cell TEXT NOT NULL,             -- 单元格名/地址
    job TEXT,                       -- 拍照时相机运行的作业（jobx 配方名）
    value REAL,                     -- 数值（无法解析为数值时为 NULL）
    text TEXT,                      -- 原始文本
    ok INTEGER,                     -- 1=合格 0=超差 NULL=无阈值
    lower_limit REAL,               -- 判定时的下限快照（阈值改判后历史仍可追溯）
    upper_limit REAL,               -- 判定时的上限快照
    spec_version INTEGER            -- 规格版本快照（预留，暂未启用）
);
CREATE INDEX IF NOT EXISTS idx_samples_cam_cell ON samples(camera, cell, ts);
-- idx_samples_cam_job 在 _migrate_schema 中建：旧库需先补 job 列，否则索引语句会失败

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,               -- ISO8601 本地时间（同一次拍照共用）
    camera TEXT NOT NULL,
    cell TEXT NOT NULL,
    job TEXT,                       -- 拍照时相机运行的作业（jobx 配方名）
    kind TEXT NOT NULL,             -- image=原始图 render=结果渲染图
    mime TEXT,                      -- 如 image/png
    width INTEGER,
    height INTEGER,
    data BLOB NOT NULL              -- 图像二进制
);
CREATE INDEX IF NOT EXISTS idx_images_cam_cell ON images(camera, cell, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,               -- 首次发生时间
    last_ts TEXT,                   -- 最近一次发生时间
    recovered_ts TEXT,              -- 恢复时间（NULL=未恢复）
    camera TEXT,                    -- 关联相机（可为空）
    cell TEXT,                      -- 关联单元格（可为空）
    code TEXT NOT NULL,             -- 事件码，如 CAMERA_OFFLINE / NG_BURST
    message TEXT,                   -- 事件详情
    status TEXT NOT NULL DEFAULT 'open'  -- open=未恢复 / recovered=已恢复 / acked=已处理
);
CREATE INDEX IF NOT EXISTS idx_events_camera_code_ts ON events(camera, code, ts);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id INTEGER,               -- 关联 images.id（删除图像时级联删除）
    camera TEXT NOT NULL,
    cell TEXT NOT NULL,
    ts TEXT NOT NULL,               -- 标注时间
    reviewer TEXT,                  -- 复核人
    label TEXT NOT NULL,            -- 误判类别
    description TEXT,               -- 误判原因 / 注意点
    json TEXT NOT NULL,             -- 完整 XAnyLabeling 3.3.x JSON（imageData 内嵌图片）
    FOREIGN KEY (image_id) REFERENCES images (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_annotations_image ON annotations(image_id);
CREATE INDEX IF NOT EXISTS idx_annotations_cam_cell_ts ON annotations(camera, cell, ts);

CREATE TABLE IF NOT EXISTS image_streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start TEXT NOT NULL,         -- 首帧时间
    ts_end TEXT NOT NULL,           -- 末帧时间
    camera TEXT NOT NULL,
    cell TEXT NOT NULL,
    job TEXT,                       -- 分片所属作业（jobx 配方名）
    kind TEXT NOT NULL,             -- image=原始图 render=结果渲染图
    codec TEXT NOT NULL,            -- av1
    container TEXT NOT NULL,        -- ivf
    frame_count INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    size_bytes INTEGER,             -- 压缩后码流大小
    data BLOB NOT NULL              -- AV1(IVF) 码流
);
CREATE INDEX IF NOT EXISTS idx_streams_cam_cell_ts ON image_streams(camera, cell, kind, ts_start);

CREATE TABLE IF NOT EXISTS image_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL,
    frame_index INTEGER NOT NULL,   -- 片内序号，从 0 开始
    ts TEXT NOT NULL,
    camera TEXT NOT NULL,
    cell TEXT NOT NULL,
    kind TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    FOREIGN KEY (stream_id) REFERENCES image_streams (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_frames_cam_cell_ts ON image_frames(camera, cell, kind, ts);
CREATE INDEX IF NOT EXISTS idx_frames_stream ON image_frames(stream_id, frame_index);

-- ---------- 改善报告（docs/当日改善报告功能设计.md §5.2）----------
CREATE TABLE IF NOT EXISTS schema_meta (
    `key`      TEXT PRIMARY KEY,        -- schema_version
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    day             TEXT NOT NULL,
    hours           TEXT NOT NULL,
    title           TEXT NOT NULL,
    filename        TEXT NOT NULL,
    xlsx_size       INTEGER NOT NULL,
    pdf_size        INTEGER NOT NULL,
    xlsx_sha256     TEXT NOT NULL,
    pdf_sha256      TEXT NOT NULL,
    stations        INTEGER NOT NULL,
    products        INTEGER NOT NULL,
    ng              INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    saved_xlsx_path TEXT,
    saved_pdf_path  TEXT,
    saved_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_day ON reports(day, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);

CREATE TABLE IF NOT EXISTS report_files (
    report_id INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,            -- xlsx | pdf
    part_no   INTEGER NOT NULL,
    data      BLOB NOT NULL,
    PRIMARY KEY (report_id, kind, part_no)
);

CREATE TABLE IF NOT EXISTS report_tags (
    day        TEXT NOT NULL,
    camera     TEXT NOT NULL,
    cell       TEXT NOT NULL DEFAULT 'C11',
    ts         TEXT NOT NULL,
    tag        TEXT NOT NULL,           -- misjudge | product | external
    reviewer   TEXT,
    note       TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, camera, ts)
);
CREATE INDEX IF NOT EXISTS idx_report_tags_day_tag ON report_tags(day, tag);
CREATE INDEX IF NOT EXISTS idx_report_tags_cam_ts ON report_tags(camera, ts);

CREATE TABLE IF NOT EXISTS report_drafts (
    day          TEXT PRIMARY KEY,
    hours        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    updated_by   TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    measure      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',
    close_reason TEXT,
    priority     TEXT NOT NULL DEFAULT 'P2',
    category     TEXT NOT NULL DEFAULT 'other',
    station      TEXT NOT NULL DEFAULT '',
    camera       TEXT NOT NULL DEFAULT '',
    assignee     TEXT NOT NULL DEFAULT '',
    due_date     TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tickets_station ON tickets(station, category);

CREATE TABLE IF NOT EXISTS ticket_reports (
    ticket_id  INTEGER NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    report_id  INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    station    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticket_id, report_id)
);
CREATE INDEX IF NOT EXISTS idx_ticket_reports_report ON ticket_reports(report_id);

CREATE TABLE IF NOT EXISTS ticket_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    actor     TEXT,
    kind      TEXT NOT NULL,            -- created|comment|status|assign|link_report|measure
    payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id, ts);
"""

# MariaDB 版 schema：与 SQLite 版字段一一对应，仅类型/自增语法不同
MARIADB_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id BIGINT NOT NULL AUTO_INCREMENT,
    ts VARCHAR(32) NOT NULL,
    camera VARCHAR(64) NOT NULL,
    cell VARCHAR(64) NOT NULL,
    job VARCHAR(128) NULL,
    value DOUBLE NULL,
    text LONGTEXT NULL,
    ok TINYINT NULL,
    lower_limit DOUBLE NULL,
    upper_limit DOUBLE NULL,
    spec_version INT NULL,
    PRIMARY KEY (id),
    KEY idx_samples_cam_cell (camera, cell, ts),
    KEY idx_samples_cam_job (camera, job, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS images (
    id BIGINT NOT NULL AUTO_INCREMENT,
    ts VARCHAR(32) NOT NULL,
    camera VARCHAR(64) NOT NULL,
    cell VARCHAR(64) NOT NULL,
    job VARCHAR(128) NULL,
    kind VARCHAR(16) NOT NULL,
    mime VARCHAR(64) NULL,
    width INT NULL,
    height INT NULL,
    data LONGBLOB NOT NULL,
    PRIMARY KEY (id),
    KEY idx_images_cam_cell (camera, cell, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    ts VARCHAR(32) NOT NULL,
    last_ts VARCHAR(32) NULL,
    recovered_ts VARCHAR(32) NULL,
    camera VARCHAR(64) NULL,
    cell VARCHAR(64) NULL,
    code VARCHAR(64) NOT NULL,
    message TEXT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    PRIMARY KEY (id),
    KEY idx_events_camera_code_ts (camera, code, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS annotations (
    id BIGINT NOT NULL AUTO_INCREMENT,
    image_id BIGINT NULL,
    camera VARCHAR(64) NOT NULL,
    cell VARCHAR(64) NOT NULL,
    ts VARCHAR(32) NOT NULL,
    reviewer VARCHAR(64) NULL,
    label VARCHAR(64) NOT NULL,
    description TEXT NULL,
    `json` LONGTEXT NOT NULL,
    PRIMARY KEY (id),
    KEY idx_annotations_image (image_id),
    KEY idx_annotations_cam_cell_ts (camera, cell, ts),
    CONSTRAINT fk_annotations_image FOREIGN KEY (image_id)
        REFERENCES images (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS image_streams (
    id BIGINT NOT NULL AUTO_INCREMENT,
    ts_start VARCHAR(32) NOT NULL,
    ts_end VARCHAR(32) NOT NULL,
    camera VARCHAR(64) NOT NULL,
    cell VARCHAR(64) NOT NULL,
    job VARCHAR(128) NULL,
    kind VARCHAR(16) NOT NULL,
    codec VARCHAR(16) NOT NULL,
    container VARCHAR(16) NOT NULL,
    frame_count INT NOT NULL,
    width INT NULL,
    height INT NULL,
    size_bytes INT NULL,
    data LONGBLOB NOT NULL,
    PRIMARY KEY (id),
    KEY idx_streams_cam_cell_ts (camera, cell, kind, ts_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS image_frames (
    id BIGINT NOT NULL AUTO_INCREMENT,
    stream_id BIGINT NOT NULL,
    frame_index INT NOT NULL,
    ts VARCHAR(32) NOT NULL,
    camera VARCHAR(64) NOT NULL,
    cell VARCHAR(64) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    width INT NULL,
    height INT NULL,
    PRIMARY KEY (id),
    KEY idx_frames_cam_cell_ts (camera, cell, kind, ts),
    KEY idx_frames_stream (stream_id, frame_index),
    CONSTRAINT fk_frames_stream FOREIGN KEY (stream_id)
        REFERENCES image_streams (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS schema_meta (
    `key` VARCHAR(64) NOT NULL,
    value VARCHAR(64) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reports (
    id BIGINT NOT NULL AUTO_INCREMENT,
    day VARCHAR(16) NOT NULL,
    hours VARCHAR(256) NOT NULL,
    title VARCHAR(255) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    xlsx_size BIGINT NOT NULL,
    pdf_size BIGINT NOT NULL,
    xlsx_sha256 CHAR(64) NOT NULL,
    pdf_sha256 CHAR(64) NOT NULL,
    stations INT NOT NULL,
    products INT NOT NULL,
    ng INT NOT NULL,
    payload_json LONGTEXT NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    created_by VARCHAR(64) NULL,
    saved_xlsx_path VARCHAR(1024) NULL,
    saved_pdf_path VARCHAR(1024) NULL,
    saved_at VARCHAR(32) NULL,
    PRIMARY KEY (id),
    KEY idx_reports_day (day, created_at),
    KEY idx_reports_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS report_files (
    report_id BIGINT NOT NULL,
    kind VARCHAR(8) NOT NULL,
    part_no INT NOT NULL,
    data MEDIUMBLOB NOT NULL,
    PRIMARY KEY (report_id, kind, part_no),
    CONSTRAINT fk_report_files_report FOREIGN KEY (report_id)
        REFERENCES reports (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS report_tags (
    day VARCHAR(16) NOT NULL,
    camera VARCHAR(64) NOT NULL,
    cell VARCHAR(64) NOT NULL DEFAULT 'C11',
    ts VARCHAR(32) NOT NULL,
    tag VARCHAR(16) NOT NULL,
    reviewer VARCHAR(64) NULL,
    note TEXT NULL,
    updated_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (day, camera, ts),
    KEY idx_report_tags_day_tag (day, tag),
    KEY idx_report_tags_cam_ts (camera, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS report_drafts (
    day VARCHAR(16) NOT NULL,
    hours VARCHAR(256) NOT NULL,
    payload_json LONGTEXT NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    updated_by VARCHAR(64) NULL,
    PRIMARY KEY (day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tickets (
    id BIGINT NOT NULL AUTO_INCREMENT,
    title VARCHAR(255) NOT NULL,
    body LONGTEXT NULL,
    measure LONGTEXT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    close_reason VARCHAR(16) NULL,
    priority VARCHAR(4) NOT NULL DEFAULT 'P2',
    category VARCHAR(16) NOT NULL DEFAULT 'other',
    station VARCHAR(32) NOT NULL DEFAULT '',
    camera VARCHAR(64) NOT NULL DEFAULT '',
    assignee VARCHAR(64) NOT NULL DEFAULT '',
    due_date VARCHAR(16) NULL,
    created_by VARCHAR(64) NULL,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    closed_at VARCHAR(32) NULL,
    PRIMARY KEY (id),
    KEY idx_tickets_status (status, updated_at),
    KEY idx_tickets_station (station, category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ticket_reports (
    ticket_id BIGINT NOT NULL,
    report_id BIGINT NOT NULL,
    station VARCHAR(32) NOT NULL DEFAULT '',
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (ticket_id, report_id),
    KEY idx_ticket_reports_report (report_id),
    CONSTRAINT fk_ticket_reports_ticket FOREIGN KEY (ticket_id)
        REFERENCES tickets (id) ON DELETE CASCADE,
    CONSTRAINT fk_ticket_reports_report FOREIGN KEY (report_id)
        REFERENCES reports (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ticket_events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    ticket_id BIGINT NOT NULL,
    ts VARCHAR(32) NOT NULL,
    actor VARCHAR(64) NULL,
    kind VARCHAR(16) NOT NULL,
    payload LONGTEXT NULL,
    PRIMARY KEY (id),
    KEY idx_ticket_events_ticket (ticket_id, ts),
    CONSTRAINT fk_ticket_events_ticket FOREIGN KEY (ticket_id)
        REFERENCES tickets (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def timestamp_suffix(ts: datetime | None = None) -> str:
    return (ts or datetime.now()).strftime("_%Y%m%d_%H%M%S")


def _resolve_sqlite_path(data_dir: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else Path(data_dir) / path


class _SqliteLikeConnection:
    """让 pymysql 连接具备 sqlite3 风格的 execute / commit / close 调用形态。"""

    # 2006=server gone away, 2013=lost connection：断线后重连并重试一次
    _DISCONNECT_CODES = (2006, 2013)

    def __init__(self, raw):
        self._raw = raw

    def _run(self, sql: str, args: tuple):
        cur = self._raw.cursor()
        cur.execute(sql, args)
        return cur

    def execute(self, sql: str, args: tuple = ()):
        try:
            return self._run(sql, args)
        except Exception as exc:
            if exc.args and exc.args[0] in self._DISCONNECT_CODES:
                self._raw.ping(reconnect=True)
                return self._run(sql, args)
            raise

    def commit(self) -> None:
        self._raw.commit()

    def executemany(self, sql: str, seq):
        cur = self._raw.cursor()
        cur.executemany(sql, seq)
        return cur

    def close(self) -> None:
        self._raw.close()


class StorageBase:
    """存储后端基类：对外暴露统一的数据访问接口，SQL 方言由子类提供。"""

    backend = "unknown"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = None
        # 数据库 schema 版本高于程序时置 True：本次只读，不写入
        self.schema_outdated = False

    # ---------- 由子类实现 ----------

    def _sql(self, sql: str) -> str:
        """SQL 方言转换（默认占位符为 ?）。"""
        return sql

    def _migrate_schema(self) -> None:
        """为旧库补齐后加的列（CREATE TABLE IF NOT EXISTS 不会改已有表）。"""

    # ---------- 内部执行 ----------

    def _execute(self, sql: str, args: tuple = ()) -> tuple[int | None, int]:
        """执行写语句并提交，返回 (lastrowid, rowcount)。"""
        with self._lock:
            cur = self._conn.execute(self._sql(sql), args)
            last_id, row_count = cur.lastrowid, cur.rowcount
            cur.close()
            self._conn.commit()
            return last_id, row_count

    def _fetchall(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), args)
            rows = cur.fetchall()
            cur.close()
            return rows

    def _execute_many(self, sql: str, seq) -> int:
        """批量写语句并提交一次，返回影响行数。"""
        seq = list(seq)
        if not seq:
            return 0
        with self._lock:
            cur = self._conn.executemany(self._sql(sql), seq)
            count = cur.rowcount
            cur.close()
            self._conn.commit()
            return count

    def health_check(self) -> bool:
        """轻量连通性检查：执行 SELECT 1，失败返回 False（不抛异常）。"""
        try:
            with self._lock:
                cur = self._conn.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            return True
        except Exception:
            return False

    def database_size_mb(self) -> float | None:
        """当前后端的数据占用（MB），不可用时返回 None。"""
        return None

    # ---------- 后台压缩专用 ----------

    def _exec_batch(self, ops: list[tuple[str, tuple]]) -> None:
        """在同一事务内执行多条写语句（后台压缩用）。"""
        with self._lock:
            cur = self._conn._raw.cursor() if hasattr(self._conn, "_raw") else self._conn.cursor()
            try:
                cur.execute(self._sql("BEGIN"))
                for sql, args in ops:
                    cur.execute(self._sql(sql), args)
                self._conn.commit()
            except Exception:
                try:
                    cur.execute(self._sql("ROLLBACK"))
                    self._conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                cur.close()

    def _exec_batch_with_insert_id(
        self,
        insert_sql: str, insert_args: tuple,
        remaining_ops: list[tuple[str, tuple]],
    ) -> int:
        """在同一事务内先执行 INSERT 拿到自增 id，再执行后续多条语句。

        用于后台压缩的三步原子操作：INSERT stream（拿 id）→ INSERT frames → DELETE images。
        如果任何一步失败，全部回滚。返回 INSERT 产生的自增 id。
        """
        with self._lock:
            cur = self._conn._raw.cursor() if hasattr(self._conn, "_raw") else self._conn.cursor()
            try:
                cur.execute(self._sql("BEGIN"))
                cur.execute(self._sql(insert_sql), insert_args)
                stream_id = cur.lastrowid
                for sql, args in remaining_ops:
                    cur.execute(self._sql(sql), args)
                self._conn.commit()
                return int(stream_id) if stream_id else 0
            except Exception:
                try:
                    cur.execute(self._sql("ROLLBACK"))
                    self._conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                cur.close()

    def query_images_for_compress(
        self,
        camera: str,
        cell: str,
        kind: str,
        job: str | None,
        before_ts: str,
        limit: int,
    ) -> list[tuple[int, str, int | None, int | None]]:
        """按 (camera, cell, kind, job) 查询稳定窗口内的 images，
        排除有 annotations 引用的行，返回 [(id, ts, width, height)]。"""
        sql = (
            "SELECT i.id, i.ts, i.width, i.height"
            " FROM images i"
            " WHERE i.camera=? AND i.cell=? AND i.kind=?"
            " AND i.ts<?"
            " AND i.id NOT IN (SELECT image_id FROM annotations WHERE image_id IS NOT NULL)"
        )
        args: list = [camera, cell, kind, before_ts]
        if job is None:
            sql += " AND i.job IS NULL"
        else:
            sql += " AND i.job=?"
            args.append(job)
        sql += " ORDER BY i.ts LIMIT ?"
        args.append(limit)
        return self._fetchall(sql, tuple(args))

    def delete_images_batch(self, ids: list[int]) -> int:
        """批量删除 images 行（后台压缩成功后清理原图），返回影响行数。"""
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        return self._execute(
            f"DELETE FROM images WHERE id IN ({marks})", tuple(ids))[1]

    def list_compressible_groups(
        self,
        before_ts: str,
        min_frames: int = 5,
    ) -> list[tuple[str, str, str, str | None, int]]:
        """列出所有可压缩分组（同一时间窗口内 images 表帧数 ≥ min_frames 的）。

        仅位图（image/render）参与 AV1 打包；点云等其他 kind 原样保留。
        返回 [(camera, cell, kind, job, frame_count)]。
        """
        sql = (
            "SELECT camera, cell, kind, job, COUNT(*)"
            " FROM images"
            " WHERE ts<?"
            " AND kind IN ('image', 'render')"
            " AND id NOT IN (SELECT image_id FROM annotations WHERE image_id IS NOT NULL)"
            " GROUP BY camera, cell, kind, job"
            f" HAVING COUNT(*) >= {min_frames}"
            " ORDER BY COUNT(*) DESC"
        )
        return self._fetchall(sql, (before_ts,))

    def insert_stream_and_delete_images(
        self,
        camera: str,
        cell: str,
        kind: str,
        job: str | None,
        frames: list[tuple[datetime, int | None, int | None]],
        stream_data: bytes,
        image_ids: list[int],
    ) -> int:
        """同一事务内原子执行：INSERT image_streams + INSERT image_frames + DELETE images。

        返回 stream_id；任何一步失败全部回滚。
        """
        ts_start = frames[0][0].isoformat(timespec="milliseconds")
        ts_end = frames[-1][0].isoformat(timespec="milliseconds")
        width, height = frames[0][1], frames[0][2]
        frame_count = len(frames)
        size_bytes = len(stream_data)

        stream_id = 0
        with self._lock:
            cur = self._conn._raw.cursor() if hasattr(self._conn, "_raw") else self._conn.cursor()
            try:
                cur.execute(self._sql("BEGIN"))
                # Step 1: INSERT image_streams，拿自增 id
                cur.execute(self._sql(
                    "INSERT INTO image_streams"
                    " (ts_start, ts_end, camera, cell, job, kind, codec, container,"
                    "  frame_count, width, height, size_bytes, data)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                ), (ts_start, ts_end, camera, cell, job, kind, "av1", "ivf",
                    frame_count, width, height, size_bytes, stream_data))
                stream_id = int(cur.lastrowid) if cur.lastrowid else 0
                if not stream_id:
                    raise RuntimeError("插入 image_streams 失败，未获得自增 id")

                # Step 2: INSERT image_frames（替换为真实 stream_id）
                for i, (ts, w, h) in enumerate(frames):
                    cur.execute(self._sql(
                        "INSERT INTO image_frames"
                        " (stream_id, frame_index, ts, camera, cell, kind, width, height)"
                        " VALUES (?,?,?,?,?,?,?,?)"
                    ), (stream_id, i, ts.isoformat(timespec="milliseconds"),
                        camera, cell, kind, w, h))

                # Step 3: DELETE 被压缩的原图
                if image_ids:
                    marks = ",".join("?" * len(image_ids))
                    cur.execute(self._sql(f"DELETE FROM images WHERE id IN ({marks})"),
                                tuple(image_ids))

                self._conn.commit()
                return stream_id
            except Exception:
                try:
                    cur.execute(self._sql("ROLLBACK"))
                    self._conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                cur.close()

    # ---------- 采样 ----------

    def insert_sample(
        self,
        ts: datetime,
        camera: str,
        cell: str,
        raw,
        ok: bool | None,
        lower_limit: float | None = None,
        upper_limit: float | None = None,
        spec_version: int | None = None,
        job: str | None = None,
    ) -> None:
        value = None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            pass
        self._execute(
            "INSERT INTO samples"
            " (ts, camera, cell, job, value, text, ok, lower_limit, upper_limit, spec_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(timespec="milliseconds"), camera, cell, job, value, str(raw), ok,
             lower_limit, upper_limit, spec_version),
        )

    def query(
        self,
        camera: str,
        cell: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, float | None, str, int | None, str | None]]:
        """返回 [(ts, value, text, ok, job)]，按时间升序。"""
        sql = "SELECT ts, value, text, ok, job FROM samples WHERE camera=? AND cell=?"
        args: list = [camera, cell]
        if start:
            sql += " AND ts>=?"
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            sql += " AND ts<=?"
            args.append(end.isoformat(timespec="milliseconds"))
        sql += " ORDER BY ts"
        return self._fetchall(sql, tuple(args))

    def query_all(
        self,
        camera: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, str, float | None, str, int | None, str | None]]:
        """返回某相机全部采样点 [(ts, cell, value, text, ok, job)]，按时间升序。"""
        sql = "SELECT ts, cell, value, text, ok, job FROM samples WHERE camera=?"
        args: list = [camera]
        if start:
            sql += " AND ts>=?"
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            sql += " AND ts<=?"
            args.append(end.isoformat(timespec="milliseconds"))
        sql += " ORDER BY ts, cell"
        return self._fetchall(sql, tuple(args))

    def cells(self, camera: str) -> list[str]:
        rows = self._fetchall(
            "SELECT DISTINCT cell FROM samples WHERE camera=? ORDER BY cell", (camera,))
        return [r[0] for r in rows]

    def cameras(self) -> list[str]:
        rows = self._fetchall("SELECT DISTINCT camera FROM samples ORDER BY camera")
        return [r[0] for r in rows]

    def delete_samples(self, camera: str, cell: str | None = None,
                       start: datetime | None = None,
                       end: datetime | None = None) -> int:
        """删除采样数据，返回删除行数。cell=None 时删除该相机全部单元格。"""
        sql = "DELETE FROM samples WHERE camera=?"
        args: list = [camera]
        if cell:
            sql += " AND cell=?"
            args.append(cell)
        if start:
            sql += " AND ts>=?"
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            sql += " AND ts<=?"
            args.append(end.isoformat(timespec="milliseconds"))
        return self._execute(sql, tuple(args))[1]

    # ---------- 图像 ----------

    def insert_image(
        self,
        ts: datetime,
        camera: str,
        cell: str,
        kind: str,
        data: bytes,
        mime: str = "image/png",
        job: str | None = None,
    ) -> tuple[int | None, int | None]:
        """写入图像 BLOB，返回 (宽, 高)（无法解析时为 None；点云等非位图不解析）。"""
        width = height = None
        if mime != "image/x-cpcd":
            try:
                from io import BytesIO

                from PIL import Image as PILImage

                with PILImage.open(BytesIO(data)) as im:
                    width, height = im.size
            except Exception:
                pass
        self._execute(
            "INSERT INTO images (ts, camera, cell, job, kind, mime, width, height, data)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(timespec="milliseconds"), camera, cell, job, kind,
             mime, width, height, data),
        )
        return width, height

    def query_images(
        self,
        camera: str,
        cell: str,
        kind: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, str, str, int | None, int | None, bytes]]:
        """返回 [(ts, kind, mime, width, height, data)]，按时间升序。"""
        sql = "SELECT ts, kind, mime, width, height, data FROM images WHERE camera=? AND cell=?"
        args: list = [camera, cell]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if start:
            sql += " AND ts>=?"
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            sql += " AND ts<=?"
            args.append(end.isoformat(timespec="milliseconds"))
        sql += " ORDER BY ts"
        return self._fetchall(sql, tuple(args))

    def delete_images(self, camera: str, cell: str | None = None,
                      start: datetime | None = None,
                      end: datetime | None = None) -> int:
        """删除图像数据，返回删除行数（含整片删除的 AV1 帧）。"""
        sql = "DELETE FROM images WHERE camera=?"
        args: list = [camera]
        if cell:
            sql += " AND cell=?"
            args.append(cell)
        if start:
            sql += " AND ts>=?"
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            sql += " AND ts<=?"
            args.append(end.isoformat(timespec="milliseconds"))
        deleted = self._execute(sql, tuple(args))[1]

        # AV1 分片按整片删除：片内帧互相依赖，不能只删其中几帧
        ssql = "SELECT id, frame_count FROM image_streams WHERE camera=?"
        sargs: list = [camera]
        if cell:
            ssql += " AND cell=?"
            sargs.append(cell)
        if start:
            ssql += " AND ts_end>=?"
            sargs.append(start.isoformat(timespec="milliseconds"))
        if end:
            ssql += " AND ts_start<=?"
            sargs.append(end.isoformat(timespec="milliseconds"))
        streams = self._fetchall(ssql, tuple(sargs))
        if streams:
            ids = [r[0] for r in streams]
            marks = ",".join("?" * len(ids))
            deleted += sum(r[1] for r in streams)
            self._execute(f"DELETE FROM image_frames WHERE stream_id IN ({marks})", tuple(ids))
            self._execute(f"DELETE FROM image_streams WHERE id IN ({marks})", tuple(ids))
        return deleted

    # ---------- AV1 帧序列 ----------

    def insert_image_stream(
        self,
        camera: str,
        cell: str,
        kind: str,
        frames: list[tuple[datetime, int | None, int | None]],
        data: bytes,
        codec: str = "av1",
        container: str = "ivf",
        job: str | None = None,
    ) -> int:
        """写入一个 AV1 分片，frames=[(ts, width, height)] 按帧序，返回分片 id。"""
        ts_start = frames[0][0].isoformat(timespec="milliseconds")
        ts_end = frames[-1][0].isoformat(timespec="milliseconds")
        width, height = frames[0][1], frames[0][2]
        stream_id, _ = self._execute(
            "INSERT INTO image_streams"
            " (ts_start, ts_end, camera, cell, job, kind, codec, container,"
            "  frame_count, width, height, size_bytes, data)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts_start, ts_end, camera, cell, job, kind, codec, container,
             len(frames), width, height, len(data), data),
        )
        self._execute_many(
            "INSERT INTO image_frames"
            " (stream_id, frame_index, ts, camera, cell, kind, width, height)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(int(stream_id), i, ts.isoformat(timespec="milliseconds"),
              camera, cell, kind, w, h)
             for i, (ts, w, h) in enumerate(frames)],
        )
        return int(stream_id)

    def query_stream_frames(
        self,
        camera: str,
        cell: str,
        kind: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, str, int, int, int | None, int | None]]:
        """返回 [(ts, kind, stream_id, frame_index, width, height)]，按时间升序。"""
        sql = ("SELECT ts, kind, stream_id, frame_index, width, height"
               " FROM image_frames WHERE camera=? AND cell=?")
        args: list = [camera, cell]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if start:
            sql += " AND ts>=?"
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            sql += " AND ts<=?"
            args.append(end.isoformat(timespec="milliseconds"))
        sql += " ORDER BY ts"
        return self._fetchall(sql, tuple(args))

    def get_image_stream(self, stream_id: int) -> bytes | None:
        rows = self._fetchall("SELECT data FROM image_streams WHERE id=?", (stream_id,))
        return bytes(rows[0][0]) if rows else None

    def list_image_frames(
        self,
        camera: str,
        cell: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, str, int | None, int | None]]:
        """返回图片索引 [(ts, kind, width, height)]，合并单帧 images 与 AV1 分片帧，按时间升序。"""
        rows: list[tuple] = []
        for table in ("images", "image_frames"):
            sql = f"SELECT ts, kind, width, height FROM {table} WHERE camera=? AND cell=?"
            args: list = [camera, cell]
            if start:
                sql += " AND ts>=?"
                args.append(start.isoformat(timespec="milliseconds"))
            if end:
                sql += " AND ts<=?"
                args.append(end.isoformat(timespec="milliseconds"))
            rows.extend(self._fetchall(sql, tuple(args)))
        rows.sort(key=lambda row: str(row[0]))
        return rows

    # ---------- 事件 ----------

    def insert_event(
        self,
        ts: datetime,
        code: str,
        camera: str | None = None,
        cell: str | None = None,
        message: str = "",
        status: str = "open",
    ) -> int:
        """记录一条异常事件（如相机断线、NG 突发），返回事件 id。"""
        stamp = ts.isoformat(timespec="milliseconds")
        event_id, _ = self._execute(
            "INSERT INTO events (ts, last_ts, camera, cell, code, message, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (stamp, stamp, camera, cell, code, message, status),
        )
        return event_id

    def query_events(
        self,
        camera: str | None = None,
        code: str | None = None,
        status: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[tuple[int, str, str | None, str | None, str | None, str | None,
                    str, str | None, str]]:
        """返回 [(id, ts, last_ts, recovered_ts, camera, cell, code, message, status)]，按时间倒序。"""
        sql = ("SELECT id, ts, last_ts, recovered_ts, camera, cell, code, message, status"
               " FROM events")
        where: list[str] = []
        args: list = []
        if camera:
            where.append("camera=?")
            args.append(camera)
        if code:
            where.append("code=?")
            args.append(code)
        if status:
            where.append("status=?")
            args.append(status)
        if start:
            where.append("ts>=?")
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            where.append("ts<=?")
            args.append(end.isoformat(timespec="milliseconds"))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return self._fetchall(sql, tuple(args))

    # ---------- 标注 ----------

    def insert_annotation(
        self,
        ts: datetime,
        camera: str,
        cell: str,
        label: str,
        json_text: str,
        description: str = "",
        reviewer: str | None = None,
        image_id: int | None = None,
    ) -> int:
        """保存一条误判标注（json_text 为 XAnyLabeling 3.3.x 完整 JSON），返回标注 id。"""
        ann_id, _ = self._execute(
            "INSERT INTO annotations"
            " (image_id, camera, cell, ts, reviewer, label, description, `json`)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (image_id, camera, cell, ts.isoformat(timespec="milliseconds"),
             reviewer, label, description, json_text),
        )
        return ann_id

    def query_annotations(
        self,
        camera: str | None = None,
        cell: str | None = None,
        image_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[tuple[int, int | None, str, str, str, str | None, str,
                    str | None, str]]:
        """返回 [(id, image_id, camera, cell, ts, reviewer, label, description, json)]，按时间倒序。"""
        sql = ("SELECT id, image_id, camera, cell, ts, reviewer, label, description, `json`"
               " FROM annotations")
        where: list[str] = []
        args: list = []
        if camera:
            where.append("camera=?")
            args.append(camera)
        if cell:
            where.append("cell=?")
            args.append(cell)
        if image_id is not None:
            where.append("image_id=?")
            args.append(image_id)
        if start:
            where.append("ts>=?")
            args.append(start.isoformat(timespec="milliseconds"))
        if end:
            where.append("ts<=?")
            args.append(end.isoformat(timespec="milliseconds"))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return self._fetchall(sql, tuple(args))

    # ---------- 事务 / schema 版本 ----------

    def _transaction(self, fn):
        """在单事务内执行 fn(cur)：失败整体回滚，成功提交并返回 fn 的结果。"""
        with self._lock:
            cur = self._conn._raw.cursor() if hasattr(self._conn, "_raw") else self._conn.cursor()
            try:
                cur.execute(self._sql("BEGIN"))
                result = fn(cur)
                self._conn.commit()
                return result
            except Exception:
                try:
                    cur.execute(self._sql("ROLLBACK"))
                    self._conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                cur.close()

    def _init_schema_meta(self) -> None:
        """读 schema_meta：新库/旧库写入当前版本；高于程序版本则只读启动。"""
        rows = self._fetchall(
            "SELECT value FROM schema_meta WHERE `key`='schema_version'")
        stored = str(rows[0][0]) if rows else ""
        if not stored:
            self._exec_batch([
                ("DELETE FROM schema_meta WHERE `key`='schema_version'", ()),
                ("INSERT INTO schema_meta (`key`, value, updated_at)"
                 " VALUES ('schema_version', ?, ?)", (SCHEMA_VERSION, _now_iso())),
            ])
            log.info("初始化数据库 schema 版本：%s", SCHEMA_VERSION)
            return
        if version_tuple(stored) > version_tuple(SCHEMA_VERSION):
            self.schema_outdated = True
            log.warning("数据库由更新版本创建（schema %s > 程序 %s），请升级程序；"
                        "本次启动不写入该库", stored, SCHEMA_VERSION)
            return
        if version_tuple(stored) < version_tuple(SCHEMA_VERSION):
            # 增量迁移由各后端 _migrate_schema 幂等完成，这里只更新版本号
            self._exec_batch([
                ("DELETE FROM schema_meta WHERE `key`='schema_version'", ()),
                ("INSERT INTO schema_meta (`key`, value, updated_at)"
                 " VALUES ('schema_version', ?, ?)", (SCHEMA_VERSION, _now_iso())),
            ])
            log.info("数据库 schema 版本 %s → %s", stored, SCHEMA_VERSION)

    # ---------- 改善报告：区间聚合 ----------

    def range_cell_summary(self, cell: str,
                           ranges: list[tuple[str, str]] | None,
                           pass_values=DEFAULT_PASS_VALUES,
                           fail_values=DEFAULT_FAIL_VALUES) -> list[tuple]:
        """各相机在时段内的结果计数：camera, cell, 总数, OK, NG, 首末时间。"""
        expr, token_args = _result_status(pass_values, fail_values)
        clause, args = _ranges_where(ranges)
        inner = f"SELECT camera, cell, ts, {expr} AS status FROM samples WHERE cell=?"
        if clause:
            inner += " AND " + clause
        sql = ("SELECT camera, cell, COUNT(*),"
               " COALESCE(SUM(CASE WHEN status=1 THEN 1 ELSE 0 END), 0),"
               " COALESCE(SUM(CASE WHEN status=0 THEN 1 ELSE 0 END), 0),"
               f" MIN(ts), MAX(ts) FROM ({inner}) t WHERE status IS NOT NULL"
               " GROUP BY camera, cell ORDER BY camera")
        return self._fetchall(sql, token_args + (cell,) + args)

    def range_result_summary(self, camera: str, cell: str,
                             ranges: list[tuple[str, str]] | None,
                             pass_values=DEFAULT_PASS_VALUES,
                             fail_values=DEFAULT_FAIL_VALUES) -> tuple | None:
        """单台相机在时段内的结果计数行：camera, cell, 总数, OK, NG, 首末时间。"""
        expr, token_args = _result_status(pass_values, fail_values)
        sql = ("SELECT camera, cell, COUNT(*),"
               " COALESCE(SUM(CASE WHEN status=1 THEN 1 ELSE 0 END), 0),"
               " COALESCE(SUM(CASE WHEN status=0 THEN 1 ELSE 0 END), 0),"
               " MIN(ts), MAX(ts)"
               f" FROM (SELECT camera, cell, ts, {expr} AS status FROM samples"
               " WHERE camera=? AND cell=?")
        clause, args = _ranges_where(ranges)
        if clause:
            sql += " AND " + clause
        sql += ") t WHERE status IS NOT NULL GROUP BY camera, cell"
        rows = self._fetchall(sql, token_args + (camera, cell) + args)
        return rows[0] if rows else None

    def range_ng_timestamps(self, camera: str, cell: str,
                            ranges: list[tuple[str, str]] | None,
                            pass_values=DEFAULT_PASS_VALUES,
                            fail_values=DEFAULT_FAIL_VALUES) -> list[str]:
        """该相机在时段内所有 NG 的 ts（升序），用于抽图与打 tag。"""
        expr, token_args = _result_status(pass_values, fail_values)
        sql = f"SELECT ts FROM samples WHERE camera=? AND cell=? AND {expr}=0"
        clause, args = _ranges_where(ranges)
        if clause:
            sql += " AND " + clause
        sql += " ORDER BY ts"
        return [str(row[0]) for row in
                self._fetchall(sql, (camera, cell) + token_args + args)]

    def range_ok_timestamps(self, camera: str, cell: str,
                            ranges: list[tuple[str, str]] | None,
                            limit: int = 2000,
                            pass_values=DEFAULT_PASS_VALUES,
                            fail_values=DEFAULT_FAIL_VALUES) -> list[str]:
        """该相机在时段内 OK 的 ts（升序，最多 limit 条），用于选 OK 参考图。"""
        expr, token_args = _result_status(pass_values, fail_values)
        sql = f"SELECT ts FROM samples WHERE camera=? AND cell=? AND {expr}=1"
        clause, args = _ranges_where(ranges)
        if clause:
            sql += " AND " + clause
        sql += " ORDER BY ts LIMIT ?"
        return [str(row[0]) for row in
                self._fetchall(sql, (camera, cell) + token_args + args + (int(limit),))]

    def range_cell_values(self, camera: str, cell: str,
                          ts_from: str | None = None, ts_to: str | None = None,
                          limit: int = 5000) -> list[tuple[str, float | None, int | None]]:
        """按相机+单元格+时间范围取逐点采样：(ts, value, ok)，升序。

        value 可能为 None（string/bool 单元格或无法解析为数值）；
        ok 可能为 None（无阈值判定）。硬性 LIMIT 避免前端误操作拉超大数据量。
        SQLite 与 MariaDB 两端 ts 都是 ISO 字符串，可直接 BETWEEN 比较，
        两端实现共用基类此方法，底层 _fetchall 在两端子类各自实现。
        """
        sql = "SELECT ts, value, ok FROM samples WHERE camera=? AND cell=?"
        args: list = [camera, cell]
        if ts_from:
            sql += " AND ts >= ?"
            args.append(ts_from)
        if ts_to:
            sql += " AND ts <= ?"
            args.append(ts_to)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        rows = self._fetchall(sql, tuple(args))
        # 降序取 → 翻成升序给前端直接喂 Chart.js
        return [(str(r[0]), r[1], r[2]) for r in reversed(rows)]

    def range_jobs(self, camera: str,
                   ranges: list[tuple[str, str]] | None) -> list[tuple[str, int]]:
        """该相机在时段内运行过的作业（按记录数降序，用于报告 B 列）。"""
        sql = ("SELECT job, COUNT(*) FROM samples"
               " WHERE camera=? AND job IS NOT NULL AND job<>''")
        clause, args = _ranges_where(ranges)
        if clause:
            sql += " AND " + clause
        sql += " GROUP BY job ORDER BY COUNT(*) DESC"
        return [(str(row[0]), int(row[1])) for row in self._fetchall(sql, (camera,) + args)]

    def days_with_results(self, cell: str = "C11", limit: int = 120) -> list[str]:
        """有结果记录的日期（倒序），用于入口对话框的日期下拉。"""
        rows = self._fetchall(
            "SELECT DISTINCT substr(ts, 1, 10) AS d FROM samples"
            " WHERE cell=? ORDER BY d DESC LIMIT ?", (cell, int(limit)))
        return [str(row[0]) for row in rows if row[0]]

    def days_with_results_specs(self, cells: list[str], limit: int = 120) -> list[str]:
        """多台相机结果单元格的日期并集（倒序），用于按相机配置取日期下拉。"""
        days: set[str] = set()
        for cell in dict.fromkeys(cells):
            days.update(self.days_with_results(cell, limit))
        return sorted(days, reverse=True)[:limit]

    def ng_hours(self, day: str, cell: str = "C11",
                 pass_values=DEFAULT_PASS_VALUES,
                 fail_values=DEFAULT_FAIL_VALUES) -> list[int]:
        """某天存在 NG 的小时（0~23），用于「仅NG时段」快捷勾选。"""
        start = f"{day}T00:00:00.000"
        end = (datetime.fromisoformat(day) + timedelta(days=1)).isoformat(
            timespec="milliseconds")
        expr, token_args = _result_status(pass_values, fail_values)
        rows = self._fetchall(
            "SELECT DISTINCT substr(ts, 12, 2) FROM samples"
            f" WHERE cell=? AND {expr}=0 AND ts>=? AND ts<?",
            (cell,) + token_args + (start, end))
        return sorted({int(str(row[0])[:2]) for row in rows
                       if str(row[0])[:2].isdigit()})

    def ng_hours_specs(self, day: str,
                       specs: list[tuple[str, tuple[str, ...], tuple[str, ...]]]) -> list[int]:
        """按（单元格, 通过值, 不通过值）规则求某天 NG 小时并集。"""
        hours: set[int] = set()
        for cell, pass_values, fail_values in specs:
            hours.update(self.ng_hours(day, cell, pass_values, fail_values))
        return sorted(hours)

    # ---------- 改善报告：存档 ----------

    def insert_report(self, meta: dict, files: dict[str, bytes],
                      chunk_size: int = 8 * 1024 * 1024,
                      links: list[tuple[int, str]] | None = None) -> int:
        """单事务写入 reports + report_files（+ 工单关联），返回 report_id。"""
        cols = ("day", "hours", "title", "filename", "xlsx_size", "pdf_size",
                "xlsx_sha256", "pdf_sha256", "stations", "products", "ng",
                "payload_json", "created_at", "created_by")
        args = tuple(meta.get(name) for name in cols)
        sql = (f"INSERT INTO reports ({', '.join(cols)})"
               f" VALUES ({', '.join(['?'] * len(cols))})")
        now = _now_iso()

        def run(cur):
            cur.execute(self._sql(sql), args)
            report_id = int(cur.lastrowid or 0)
            for kind, data in files.items():
                for part_no, offset in enumerate(range(0, len(data), chunk_size)):
                    cur.execute(
                        self._sql("INSERT INTO report_files"
                                  " (report_id, kind, part_no, data) VALUES (?,?,?,?)"),
                        (report_id, kind, part_no, data[offset:offset + chunk_size]))
            for ticket_id, station in links or []:
                cur.execute(
                    self._sql("INSERT INTO ticket_reports"
                              " (ticket_id, report_id, station, created_at)"
                              " VALUES (?,?,?,?)"),
                    (int(ticket_id), report_id, str(station or ""), now))
            return report_id

        return self._transaction(run)

    def get_report(self, report_id: int) -> dict | None:
        rows = self._fetchall(
            f"SELECT {', '.join(_REPORT_COLUMNS)} FROM reports WHERE id=?", (report_id,))
        return dict(zip(_REPORT_COLUMNS, rows[0])) if rows else None

    def list_reports(self, day: str | None = None, limit: int = 100,
                     offset: int = 0) -> list[dict]:
        sql = f"SELECT {', '.join(_REPORT_COLUMNS)} FROM reports"
        args: list = []
        if day:
            sql += " WHERE day=?"
            args.append(day)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        return [dict(zip(_REPORT_COLUMNS, row))
                for row in self._fetchall(sql, tuple(args))]

    def load_report_file(self, report_id: int, kind: str) -> bytes | None:
        """按 part_no 顺序拼接分块，返回完整文件字节。"""
        rows = self._fetchall(
            "SELECT data FROM report_files WHERE report_id=? AND kind=?"
            " ORDER BY part_no", (report_id, kind))
        if not rows:
            return None
        return b"".join(bytes(row[0]) for row in rows)

    def mark_report_saved(self, report_id: int, xlsx_path: str | None = None,
                          pdf_path: str | None = None) -> None:
        """落盘成功后回填路径（两个文件可分别成功，故都可选）。"""
        sets, args = [], []
        if xlsx_path is not None:
            sets.append("saved_xlsx_path=?")
            args.append(xlsx_path)
        if pdf_path is not None:
            sets.append("saved_pdf_path=?")
            args.append(pdf_path)
        if not sets:
            return
        sets.append("saved_at=?")
        args += [_now_iso(), int(report_id)]
        self._execute(f"UPDATE reports SET {', '.join(sets)} WHERE id=?", tuple(args))

    def delete_report(self, report_id: int) -> dict | None:
        """删除存档记录（分块级联删除），返回被删记录供调用方清理磁盘文件。"""
        report = self.get_report(report_id)
        if report is None:
            return None
        self._execute("DELETE FROM reports WHERE id=?", (int(report_id),))
        return report

    def report_storage_stats(self) -> dict:
        rows = self._fetchall(
            "SELECT COUNT(*), COALESCE(SUM(xlsx_size + pdf_size), 0) FROM reports")
        count, size = (rows[0][0] or 0, rows[0][1] or 0) if rows else (0, 0)
        return {"count": int(count), "bytes": int(size)}

    def purge_reports(self, keep_days: int, keep_count: int) -> list[dict]:
        """按保留天数/条数清理存档，返回被删记录（调用方据此删磁盘文件）。"""
        doomed: dict[int, dict] = {}
        if keep_days and keep_days > 0:
            cutoff = (datetime.now() - timedelta(days=int(keep_days))).isoformat(
                timespec="milliseconds")
            rows = self._fetchall(
                "SELECT id FROM reports WHERE created_at<?", (cutoff,))
            for (report_id,) in rows:
                report = self.get_report(int(report_id))
                if report:
                    doomed[int(report_id)] = report
        if keep_count and keep_count > 0:
            rows = self._fetchall("SELECT id FROM reports ORDER BY created_at DESC")
            for (report_id,) in rows[int(keep_count):]:
                report = self.get_report(int(report_id))
                if report:
                    doomed[int(report_id)] = report
        for report_id in doomed:
            self._execute("DELETE FROM reports WHERE id=?", (report_id,))
        return list(doomed.values())

    # ---------- 改善报告：快速 tag ----------

    def set_report_tag(self, day: str, camera: str, ts: str, tag: str | None,
                       reviewer: str = "", note: str = "",
                       cell: str = "C11") -> None:
        """写入/取消 tag（tag=None 即取消，删除该 ts 的行）。"""
        if tag is None:
            self._execute(
                "DELETE FROM report_tags WHERE day=? AND camera=? AND ts=?",
                (day, camera, ts))
            return
        if tag not in REPORT_TAGS:
            raise ValueError(f"未知 tag：{tag}")
        now = _now_iso()
        self._exec_batch([
            ("DELETE FROM report_tags WHERE day=? AND camera=? AND ts=?",
             (day, camera, ts)),
            ("INSERT INTO report_tags"
             " (day, camera, cell, ts, tag, reviewer, note, updated_at)"
             " VALUES (?,?,?,?,?,?,?,?)",
             (day, camera, cell, ts, tag, reviewer, note, now)),
        ])

    def list_report_tags(self, day: str) -> dict[tuple[str, str], str]:
        """{(camera, ts): tag}"""
        rows = self._fetchall(
            "SELECT camera, ts, tag FROM report_tags WHERE day=?", (day,))
        return {(str(row[0]), str(row[1])): str(row[2]) for row in rows}

    def range_tag_counts(self, camera: str,
                         ranges: list[tuple[str, str]] | None) -> dict[str, int]:
        """该相机在时段内各 tag 的数量（misjudge/product/external）。"""
        sql = "SELECT tag, COUNT(*) FROM report_tags WHERE camera=?"
        clause, args = _ranges_where(ranges)
        if clause:
            sql += " AND " + clause
        sql += " GROUP BY tag"
        return {str(row[0]): int(row[1]) for row in self._fetchall(sql, (camera,) + args)}

    # ---------- 改善报告：草稿 ----------

    def get_draft(self, day: str) -> dict | None:
        rows = self._fetchall(
            "SELECT day, hours, payload_json, updated_at, updated_by"
            " FROM report_drafts WHERE day=?", (day,))
        if not rows:
            return None
        hours_text, payload_text = rows[0][1] or "", rows[0][2] or "{}"
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {}
        return {
            "day": rows[0][0],
            "hours": [int(h) for h in str(hours_text).split(",") if h.strip().isdigit()],
            "payload": payload,
            "updated_at": rows[0][3],
            "updated_by": rows[0][4],
        }

    def save_draft(self, day: str, hours: list[int], payload: dict,
                   actor: str = "", base_updated_at: str | None = None,
                   force: bool = False) -> str:
        """保存草稿；base_updated_at 与服务端不一致时抛 ConflictError（除非 force）。"""
        now = _now_iso()
        hours_text = ",".join(str(h) for h in sorted({int(h) for h in hours}))
        payload_text = json.dumps(payload, ensure_ascii=False)

        def run(cur):
            cur.execute(self._sql(
                "SELECT updated_at FROM report_drafts WHERE day=?"), (day,))
            row = cur.fetchone()
            current = str(row[0]) if row else None
            if current and base_updated_at and current != base_updated_at and not force:
                raise ConflictError(f"草稿已被他人更新（{current}）")
            if current is None:
                cur.execute(self._sql(
                    "INSERT INTO report_drafts"
                    " (day, hours, payload_json, updated_at, updated_by)"
                    " VALUES (?,?,?,?,?)"), (day, hours_text, payload_text, now, actor))
            else:
                cur.execute(self._sql(
                    "UPDATE report_drafts SET hours=?, payload_json=?, updated_at=?,"
                    " updated_by=? WHERE day=?"),
                    (hours_text, payload_text, now, actor, day))
            return now

        return self._transaction(run)

    def delete_draft(self, day: str) -> None:
        self._execute("DELETE FROM report_drafts WHERE day=?", (day,))

    def list_draft_days(self, limit: int = 60) -> list[dict]:
        rows = self._fetchall(
            "SELECT day, updated_at, updated_by, hours FROM report_drafts"
            " ORDER BY updated_at DESC LIMIT ?", (int(limit),))
        return [{"day": row[0], "updated_at": row[1], "updated_by": row[2],
                 "hours": row[3] or ""}
                for row in rows]

    # ---------- 工单（改善措施）----------

    def create_ticket(self, data: dict, actor: str = "") -> int:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("工单标题不能为空")
        category = str(data.get("category") or "other").lower()
        if category not in TICKET_CATEGORIES:
            category = "other"
        priority = str(data.get("priority") or "P2").upper()
        if priority not in TICKET_PRIORITIES:
            priority = "P2"
        now = _now_iso()
        args = (title[:255], str(data.get("body") or ""),
                str(data.get("measure") or ""), "open", None, priority, category,
                str(data.get("station") or ""), str(data.get("camera") or ""),
                str(data.get("assignee") or "视觉相关人员"), data.get("due_date"),
                actor, now, now, None)

        def run(cur):
            cur.execute(self._sql(
                "INSERT INTO tickets (title, body, measure, status, close_reason,"
                " priority, category, station, camera, assignee, due_date, created_by,"
                " created_at, updated_at, closed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"), args)
            ticket_id = int(cur.lastrowid or 0)
            cur.execute(self._sql(
                "INSERT INTO ticket_events (ticket_id, ts, actor, kind, payload)"
                " VALUES (?,?,?,?,?)"),
                (ticket_id, now, actor, "created",
                 json.dumps({"title": title}, ensure_ascii=False)))
            return ticket_id

        return self._transaction(run)

    def update_ticket(self, ticket_id: int, patch: dict, actor: str = "") -> dict:
        """更新工单字段；状态变更校验状态机并记时间线。"""
        ticket = self.get_ticket(ticket_id, with_events=False, with_reports=False)
        if ticket is None:
            raise KeyError(f"工单不存在：{ticket_id}")
        current = ticket["status"]
        allowed = ("title", "body", "measure", "status", "close_reason", "priority",
                   "category", "station", "camera", "assignee", "due_date")
        changes = {k: v for k, v in (patch or {}).items() if k in allowed}
        new_status = changes.get("status")
        if new_status is not None:
            new_status = str(new_status)
            if new_status not in TICKET_STATUSES:
                raise ValueError(f"未知工单状态：{new_status}")
            if new_status != current and new_status not in TICKET_TRANSITIONS.get(current, set()):
                raise ValueError(f"不允许的状态变更：{current} → {new_status}")
        now = _now_iso()
        sets, args = [], []
        for key, value in changes.items():
            sets.append(f"{key}=?")
            args.append(value)
        if new_status == "closed" and current != "closed":
            sets.append("closed_at=?")
            args.append(now)
        elif new_status == "open" and current == "closed":
            sets.append("closed_at=?")
            args.append(None)
            sets.append("close_reason=?")
            args.append(None)
        sets.append("updated_at=?")
        args.append(now)
        args.append(int(ticket_id))

        events = []
        if new_status and new_status != current:
            events.append(("status", {"from": current, "to": new_status,
                                      "reason": changes.get("close_reason")}))
        if "assignee" in changes and changes["assignee"] != ticket.get("assignee"):
            events.append(("assign", {"from": ticket.get("assignee"),
                                      "to": changes["assignee"]}))
        if "measure" in changes and changes["measure"] != ticket.get("measure"):
            events.append(("measure", {"text": str(changes["measure"])[:500]}))
        if "body" in changes and changes["body"] != ticket.get("body"):
            events.append(("comment", {"text": str(changes["body"])}))

        def run(cur):
            cur.execute(self._sql(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?"),
                        tuple(args))
            for kind, payload in events:
                cur.execute(self._sql(
                    "INSERT INTO ticket_events (ticket_id, ts, actor, kind, payload)"
                    " VALUES (?,?,?,?,?)"),
                    (int(ticket_id), now, actor, kind,
                     json.dumps(payload, ensure_ascii=False)))
            return None

        self._transaction(run)
        return self.get_ticket(ticket_id) or {}

    def add_ticket_event(self, ticket_id: int, kind: str, payload: dict,
                         actor: str = "") -> None:
        now = _now_iso()
        self._exec_batch([
            ("INSERT INTO ticket_events (ticket_id, ts, actor, kind, payload)"
             " VALUES (?,?,?,?,?)",
             (int(ticket_id), now, actor, kind,
              json.dumps(payload or {}, ensure_ascii=False))),
            ("UPDATE tickets SET updated_at=? WHERE id=?", (now, int(ticket_id))),
        ])

    def link_report_to_ticket(self, ticket_id: int, report_id: int,
                              station: str = "", actor: str = "") -> None:
        now = _now_iso()
        self._exec_batch([
            ("DELETE FROM ticket_reports WHERE ticket_id=? AND report_id=?",
             (int(ticket_id), int(report_id))),
            ("INSERT INTO ticket_reports (ticket_id, report_id, station, created_at)"
             " VALUES (?,?,?,?)", (int(ticket_id), int(report_id), station, now)),
        ])
        self.add_ticket_event(ticket_id, "link_report",
                              {"report_id": int(report_id), "station": station}, actor)

    def unlink_report_from_ticket(self, ticket_id: int, report_id: int,
                                  actor: str = "") -> None:
        self._exec_batch([
            ("DELETE FROM ticket_reports WHERE ticket_id=? AND report_id=?",
             (int(ticket_id), int(report_id))),
            ("UPDATE tickets SET updated_at=? WHERE id=?", (_now_iso(), int(ticket_id))),
        ])
        self.add_ticket_event(ticket_id, "unlink_report",
                              {"report_id": int(report_id)}, actor)

    def list_tickets(self, statuses: list[str] | None = None, station: str | None = None,
                     category: str | None = None, q: str | None = None,
                     limit: int = 200, offset: int = 0) -> list[dict]:
        sql = f"SELECT {', '.join(_TICKET_COLUMNS)} FROM tickets"
        where, args = [], []
        if statuses:
            where.append(f"status IN ({', '.join(['?'] * len(statuses))})")
            args.extend(statuses)
        if station:
            where.append("station=?")
            args.append(station)
        if category:
            where.append("category=?")
            args.append(category)
        if q:
            where.append("(title LIKE ? OR body LIKE ? OR measure LIKE ?)")
            like = f"%{q}%"
            args.extend([like, like, like])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (" ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1"
                " WHEN 'done' THEN 2 ELSE 3 END, updated_at DESC LIMIT ? OFFSET ?")
        args += [int(limit), int(offset)]
        return [dict(zip(_TICKET_COLUMNS, row)) for row in self._fetchall(sql, tuple(args))]

    def get_ticket(self, ticket_id: int, with_events: bool = True,
                   with_reports: bool = True) -> dict | None:
        rows = self._fetchall(
            f"SELECT {', '.join(_TICKET_COLUMNS)} FROM tickets WHERE id=?",
            (int(ticket_id),))
        if not rows:
            return None
        ticket = dict(zip(_TICKET_COLUMNS, rows[0]))
        if with_events:
            events = self._fetchall(
                "SELECT id, ts, actor, kind, payload FROM ticket_events"
                " WHERE ticket_id=? ORDER BY ts, id", (int(ticket_id),))
            ticket["events"] = [
                {"id": row[0], "ts": row[1], "actor": row[2], "kind": row[3],
                 "payload": _loads(row[4])}
                for row in events
            ]
        if with_reports:
            links = self._fetchall(
                "SELECT tr.report_id, tr.station, tr.created_at, r.day, r.filename,"
                " r.created_at FROM ticket_reports tr"
                " LEFT JOIN reports r ON r.id = tr.report_id"
                " WHERE tr.ticket_id=? ORDER BY tr.created_at DESC", (int(ticket_id),))
            ticket["reports"] = [
                {"report_id": row[0], "station": row[1], "linked_at": row[2],
                 "day": row[3], "filename": row[4], "report_created_at": row[5],
                 "purged": row[4] is None}
                for row in links
            ]
        return ticket

    def ticket_status_counts(self) -> dict[str, int]:
        rows = self._fetchall("SELECT status, COUNT(*) FROM tickets GROUP BY status")
        counts = {name: 0 for name in TICKET_STATUSES}
        for row in rows:
            counts[str(row[0])] = int(row[1])
        return counts

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class SQLiteStorage(StorageBase):
    """SQLite 单文件后端（标准库，零依赖，作为兜底）。"""

    backend = "sqlite"

    def __init__(self, db_path: Path):
        super().__init__()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate_schema()
        self._init_schema_meta()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(samples)")}
        for name, ddl_type in (("lower_limit", "REAL"),
                               ("upper_limit", "REAL"),
                               ("spec_version", "INTEGER"),
                               ("job", "TEXT")):
            if name not in existing:
                self._conn.execute(f"ALTER TABLE samples ADD COLUMN {name} {ddl_type}")
        for table in ("images", "image_streams"):
            columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if "job" not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN job TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_cam_job ON samples(camera, job, ts)")

    def database_size_mb(self) -> float | None:
        try:
            return round(self.path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            return None


# 兼容旧调用方式：Storage(path) 等价于 SQLiteStorage(path)
Storage = SQLiteStorage


class MariaDBStorage(StorageBase):
    """MariaDB 后端：支持多客户端共享同一数据库；连接失败由 open_storage 回退 SQLite。"""

    backend = "mariadb"

    def __init__(self, cfg: MariaDBConfig | None = None):
        super().__init__()
        cfg = cfg or MariaDBConfig()
        if not re.fullmatch(r"[A-Za-z0-9_]+", cfg.database):
            raise ValueError(f"非法的数据库名：{cfg.database}")
        try:
            import pymysql
        except ImportError as exc:  # 未安装驱动时回退到 SQLite
            raise RuntimeError("未安装 pymysql，无法使用 MariaDB 后端") from exc

        self.host, self.port, self.database = cfg.host, cfg.port, cfg.database
        target = f"{cfg.host}:{cfg.port}/{cfg.database}"
        self.endpoint = target
        try:
            raw = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                                  password=cfg.password, charset="utf8mb4",
                                  connect_timeout=cfg.connect_timeout)
            self._conn = _SqliteLikeConnection(raw)
            with raw.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg.database}`"
                            " DEFAULT CHARACTER SET utf8mb4")
            # 用 select_db（而非 USE 语句）记录库名，断线重连后可自动恢复
            raw.select_db(cfg.database)
            for stmt in MARIADB_SCHEMA.split(";"):
                if stmt.strip():
                    self._execute(stmt)
            self._migrate_schema()
            self._init_schema_meta()
        except Exception as exc:
            self.close()
            raise RuntimeError(f"MariaDB 不可用（{target}）：{exc}") from exc

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def _migrate_schema(self) -> None:
        existing = {row[0] for row in self._fetchall("SHOW COLUMNS FROM samples")}
        for name, ddl_type in (("lower_limit", "DOUBLE"),
                               ("upper_limit", "DOUBLE"),
                               ("spec_version", "INT"),
                               ("job", "VARCHAR(128)")):
            if name not in existing:
                self._execute(f"ALTER TABLE samples ADD COLUMN {name} {ddl_type}")
        for table in ("images", "image_streams"):
            columns = {row[0] for row in self._fetchall(f"SHOW COLUMNS FROM {table}")}
            if "job" not in columns:
                self._execute(f"ALTER TABLE {table} ADD COLUMN job VARCHAR(128) NULL")
        keys = {row[2] for row in self._fetchall("SHOW INDEX FROM samples")}
        if "idx_samples_cam_job" not in keys:
            self._execute("ALTER TABLE samples ADD KEY idx_samples_cam_job (camera, job, ts)")

    def database_size_mb(self) -> float | None:
        try:
            rows = self._fetchall(
                "SELECT COALESCE(SUM(data_length + index_length), 0)"
                " FROM information_schema.tables WHERE table_schema = ?",
                (self.database,))
            return round(float(rows[0][0] or 0) / 1024 / 1024, 2)
        except Exception:
            return None


def open_storage(data_dir: Path, config: DatabaseConfig | None = None) -> StorageBase:
    """按配置选择存储后端：auto=MariaDB 优先，连接失败回退 SQLite。

    回退时记录日志并返回 SQLiteStorage，保证产线采集不因数据库缺失而中断；
    backend 显式设为 mariadb 时连接失败直接抛错，避免静默降级。
    """
    cfg = config or load_database_config(data_dir)
    if cfg.backend not in ("auto", "mariadb", "sqlite"):
        raise ValueError(f"未知的数据库后端：{cfg.backend}")
    if cfg.backend != "sqlite":
        try:
            storage = MariaDBStorage(cfg.mariadb)
            log.info("已连接 MariaDB %s:%s/%s", cfg.mariadb.host, cfg.mariadb.port,
                     cfg.mariadb.database)
            return storage
        except Exception as exc:
            if cfg.backend == "mariadb":
                raise
            log.warning("%s，回退 SQLite", exc)
    path = _resolve_sqlite_path(data_dir, cfg.sqlite_path)
    log.info("使用 SQLite 后端：%s", path)
    return SQLiteStorage(path)


def export_xlsx(
    storage: StorageBase,
    camera: str,
    out_dir: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    notes: dict[str, str] | None = None,
    image_cells: set[str] | None = None,
    cells: list[str] | None = None,
    progress_callback=None,  # callable(done:int, total:int, message:str)
    cancelled: threading.Event | None = None,
) -> Path:
    """按 单元格 生成工作表导出（文件名带时间戳后缀）。

    cells=None 时导出该相机全部单元格；指定时仅导出所选单元格。
    图像单元格：直接嵌入图像（原始图工作表 + A0 的渲染图工作表）。
    额外生成「合表」工作表：将同一时间戳（同一次拍照）下所有单元格的值
    合并到同一行，列名为单元格名，便于观察同一次检测中各数据的相互关系。

    progress_callback: 每个关键步骤完成后回调 progress_callback(done, total, message)。
    cancelled: 外部传入的 threading.Event，set() 时立即中止并抛出 CancelledError。
    """
    import threading as _t
    from collections import defaultdict
    from io import BytesIO

    from .wpsdispimg import DispImage, inject_dispimg

    class CancelledError(Exception):
        pass

    def _check() -> None:
        if cancelled and cancelled.is_set():
            raise CancelledError()

    def _prog(done: int, total: int, msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(done, total, msg)
            except Exception:
                pass

    wb = Workbook()
    wb.remove(wb.active)
    red = PatternFill("solid", start_color="FFC7CE")
    bold = Font(bold=True)
    notes = notes or {}
    image_cells = set(image_cells or ())
    cell_names = cells if cells is not None else storage.cells(camera)
    embedded: list[DispImage] = []
    _prog(0, len(cell_names) + 1, "准备导出")
    for idx, cell in enumerate(cell_names):
        _check()
        _prog(idx, len(cell_names) + 1, f"处理单元格 {cell}")
        if cell in image_cells:
            embedded.extend(
                _append_image_sheets(wb, storage, camera, cell, notes.get(cell, ""),
                                     bold, start, end, _check))
            continue
        rows = storage.query(camera, cell, start, end)
        ws = wb.create_sheet(title=cell[:31])
        ws.append(["时间戳", "作业(job)", "数值", "原始值", "判定"])
        for c in ws[1]:
            c.font = bold
        if notes.get(cell):
            ws["G1"] = "注解"
            ws["G1"].font = bold
            ws["G2"] = notes[cell]
        for row_idx, (ts, value, text, ok, job) in enumerate(rows, start=1):
            if row_idx % 500 == 0:
                _check()
            ws.append([ts, job or "", value, text, "OK" if ok else ("NG" if ok == 0 else "")])
            if ok == 0:
                for c in ws[ws.max_row]:
                    c.fill = red
        if ws.max_row > 1:
            ws.auto_filter.ref = f"A1:E{ws.max_row}"

    # 合表：同一次拍照（同一时间戳）的所有单元格合并到一行
    if cell_names:
        _check()
        _prog(len(cell_names), len(cell_names) + 1, "生成合表")
        all_rows = storage.query_all(camera, start, end)
        grouped: dict[str, dict[str, tuple[float | None, str, int | None, str | None]]] = defaultdict(dict)
        ts_jobs: dict[str, str | None] = {}
        for ts, cell, value, text, ok, job in all_rows:
            grouped[ts][cell] = (value, text, ok, job)
            ts_jobs[ts] = ts_jobs.get(ts) or job
        ws = wb.create_sheet(title="合表", index=0)
        headers = ["时间戳", "作业(job)"]
        for cell in cell_names:
            note = notes.get(cell, "")
            headers.append(f"{cell}（{note}）" if note else cell)
        ws.append(headers)
        for c in ws[1]:
            c.font = bold
        all_ts = sorted(grouped)
        for idx, ts in enumerate(all_ts):
            if idx % 500 == 0:
                _check()
            row_vals = grouped[ts]
            row: list = [ts, ts_jobs.get(ts) or ""]
            for cell in cell_names:
                item = row_vals.get(cell)
                if item is None:
                    row.append(None)
                else:
                    value, text, _ok, _job = item
                    row.append(value if value is not None else text)
            ws.append(row)
        if ws.max_row > 1:
            ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(cell_names)+2).coordinate}"
            ws.freeze_panes = "C2"

    _check()
    _prog(len(cell_names) + 1, len(cell_names) + 1, "保存文件")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{camera}{timestamp_suffix()}.xlsx"
    buffer = BytesIO()
    wb.save(buffer)
    data = buffer.getvalue()
    if embedded:
        # openpyxl 不支持 cellimages，保存后按 zip 层注入 DISPIMG 嵌入单元格
        try:
            data = inject_dispimg(data, embedded)
        except Exception:
            log.exception("DISPIMG 注入失败，导出的图像单元格将为空")
    path.write_bytes(data)
    _prog(len(cell_names) + 1, len(cell_names) + 1, f"完成 → {path.name}")
    return path


def _cell_image(ws, row: int, data: bytes, sheet: str,
                max_width: int = 480):
    """图像 BLOB → `DispImage`（B{row}）；无法解码（如点云 CPCD）返回 None。

    WPS 的 `a:ext` 取图片真实像素尺寸，所以行高必须按缩放后的高度设置，
    否则图片会被压在默认行高里看不全。
    """
    from io import BytesIO

    from PIL import Image

    from .wpsdispimg import DispImage

    try:
        with Image.open(BytesIO(data)) as probe:
            width, height = probe.size
            image_format = (probe.format or "JPEG").lower()
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    scale = min(1.0, max_width / width)
    # 行高按像素换算为磅（1px ≈ 0.75pt），避免图像压到下一行
    ws.row_dimensions[row].height = max(15, height * scale * 0.75)
    return DispImage(cell=f"B{row}", data=data, sheet=sheet,
                     extension="png" if image_format == "png" else "jpeg")


def _append_image_sheets(wb, storage: Storage, camera: str, cell: str, note: str,
                         bold, start, end, check_cancelled=None) -> list:
    """图像单元格：原始图与渲染图各生成一个工作表，图片嵌入单元格。"""
    from .videocodec import load_images

    embedded: list = []
    rows = load_images(storage, camera, cell, start=start, end=end)
    for kind, title in (("image", cell[:31]), ("render", f"{cell}渲染图"[:31])):
        items = [r for r in rows if r[1] == kind]
        if not items:
            continue
        ws = wb.create_sheet(title=title)
        ws.column_dimensions["A"].width = 26
        ws.column_dimensions["B"].width = 72
        ws.append(["时间戳", "图像"])
        for c in ws[1]:
            c.font = bold
        if note:
            ws["D1"] = "注解"
            ws["D1"].font = bold
            ws["D2"] = note
        for i, (ts, _kind, _mime, width, height, data) in enumerate(items, start=2):
            if check_cancelled and i % 20 == 0:
                check_cancelled()
            ws.cell(row=i, column=1, value=ts)
            image = _cell_image(ws, i, data, ws.title)
            if image is None:
                ws.cell(row=i, column=2, value=f"[图像 {width}×{height}]")
            else:
                embedded.append(image)
        ws.freeze_panes = "A2"
    return embedded
