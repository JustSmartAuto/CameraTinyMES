"""SQLite / MariaDB 双后端落库 + xlsx 导出。

MariaDB 优先（支持多客户端汇聚），连接不可用时由 `open_storage` 自动回退 SQLite；
两套后端共用同一套表结构与数据访问接口，时间戳统一存 ISO8601 字符串（毫秒）。
xlsx 按 相机/日期/单元格 导出工作表，文件名带时间戳后缀。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .config import DatabaseConfig, MariaDBConfig, load_database_config

log = logging.getLogger(__name__)

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
        """写入图像 BLOB，返回 (宽, 高)（无法解析时为 None）。"""
        width = height = None
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
) -> Path:
    """按 单元格 生成工作表导出（文件名带时间戳后缀）。

    cells=None 时导出该相机全部单元格；指定时仅导出所选单元格。
    图像单元格：直接嵌入图像（原始图工作表 + A0 的渲染图工作表）。
    额外生成「合表」工作表：将同一时间戳（同一次拍照）下所有单元格的值
    合并到同一行，列名为单元格名，便于观察同一次检测中各数据的相互关系。
    """
    from collections import defaultdict

    wb = Workbook()
    wb.remove(wb.active)
    red = PatternFill("solid", start_color="FFC7CE")
    bold = Font(bold=True)
    notes = notes or {}
    image_cells = set(image_cells or ())
    cell_names = cells if cells is not None else storage.cells(camera)
    for cell in cell_names:
        if cell in image_cells:
            _append_image_sheets(wb, storage, camera, cell, notes.get(cell, ""), bold,
                                 start, end)
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
        for ts, value, text, ok, job in rows:
            ws.append([ts, job or "", value, text, "OK" if ok else ("NG" if ok == 0 else "")])
            if ok == 0:
                for c in ws[ws.max_row]:
                    c.fill = red
        if ws.max_row > 1:
            ws.auto_filter.ref = f"A1:E{ws.max_row}"

    # 合表：同一次拍照（同一时间戳）的所有单元格合并到一行
    if cell_names:
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
        for ts in sorted(grouped):
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

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{camera}{timestamp_suffix()}.xlsx"
    wb.save(path)
    return path


def _embed_image(ws, row: int, data: bytes, max_width: int = 480) -> bool:
    """把图像 BLOB 嵌入单元格（B{row}），返回是否成功。"""
    from io import BytesIO

    from openpyxl.drawing.image import Image as XLImage

    try:
        img = XLImage(BytesIO(data))
    except Exception:
        return False
    if img.width > max_width:
        ratio = max_width / img.width
        img.width = max_width
        img.height = int(img.height * ratio)
    ws.add_image(img, f"B{row}")
    # 行高按像素换算为磅（1px ≈ 0.75pt），避免图像压到下一行
    ws.row_dimensions[row].height = max(15, img.height * 0.75)
    return True


def _append_image_sheets(wb, storage: Storage, camera: str, cell: str, note: str,
                         bold, start, end) -> None:
    """图像单元格：原始图与渲染图各生成一个嵌入式工作表。"""
    from .videocodec import load_images

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
            ws.cell(row=i, column=1, value=ts)
            if not _embed_image(ws, i, data):
                ws.cell(row=i, column=2, value=f"[图像 {width}×{height}]")
        ws.freeze_panes = "A2"
