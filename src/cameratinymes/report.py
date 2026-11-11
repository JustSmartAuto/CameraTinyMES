"""当日改善报告：时段统计 + 版面模型（xlsx 与 PDF 共用同一份布局）。

设计见 `docs/当日改善报告功能设计.md`：
- 统计口径只读 `samples`（C11 结果单元格）与 `report_tags`，不改动采样链路；
- 版面模型 `LayoutModel` 是 xlsx 与 PDF 的单一真值：列宽、行高、合并、单元格样式、
  图片框位都记录在其中，两种输出按同一坐标渲染，保证「所见即所得」；
- 本模块不做图像抽取/缓存（由 reportstore 负责），只消费已准备好的图片字节。
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import (
    DEFAULT_IMAGE_CELL, CameraConfig, ReportConfig, extra_result_cells, image_cell,
    result_cell, result_tokens,
)

log = logging.getLogger(__name__)

# ---------- 版面常量（对齐客户模板 SAB视觉端NG原因分析）----------
NG_IMAGE_COLUMNS = 12           # M..X
COLUMNS: tuple[tuple[str, float, bool], ...] = (
    ("A", 2.5, True),
    ("B", 7.58, False),
    ("C", 27.2, False),
    ("D", 44.41, False),
    ("E", 41.17, True),         # 实物图片（可开关，默认隐藏）
    ("F", 41.17, True),         # OK 相机图片
    ("G", 15.38, False),
    ("H", 15.38, False),
    ("I", 15.38, False),
    ("J", 15.38, False),
    ("K", 9.03, False),
    ("L", 23.63, False),
    ("M", 27.18, False), ("N", 27.18, False), ("O", 27.18, False),
    ("P", 27.18, False), ("Q", 27.18, False), ("R", 27.18, False),
    ("S", 27.18, False), ("T", 27.18, False), ("U", 27.18, False),
    ("V", 27.18, False), ("W", 27.18, False), ("X", 27.18, False),
)
HEADER_LABELS = {
    "B": "No.",
    "C": "工站&机台",
    "D": "相机型号&站名",
    "E": "实物图片",
    "F": "OK相机图片",
    "G": "单站生产总数量",
    "H": "NG数量总数",
    "I": "视觉NG数量",
    "J": "其他NG数量",
    "K": "",
    "L": "",
}
HEADER_FILL = "00B050"
SHEET_TITLE_FMT = "{project}视觉改善分析"
ROW_HEIGHT_HEADER = 18
ROW_HEIGHT_DATA = 77
MAX_NG_IMAGES = NG_IMAGE_COLUMNS

STATION_PATTERN = r"^(OP\d+)"


# ---------- 数据结构 ----------

@dataclass
class ReportOptions:
    """一次报告生成的用户选择（与网页表单一致）。"""

    day: date
    hours: list[int]
    cameras: list[str] = field(default_factory=list)   # 空 = 全部启用相机
    reasons: dict[str, str] = field(default_factory=dict)       # 相机→原因（已审核）
    other_ng: dict[str, int] = field(default_factory=dict)      # 工站/相机→其他NG 改数
    picked_images: dict[str, list[str]] = field(default_factory=dict)  # 相机→NG ts（渲染图）
    show_physical: bool = False                        # 实物图片列（E）是否显示
    tickets: dict[str, list[int]] = field(default_factory=dict)  # 工站→关联工单 id


@dataclass
class CameraStat:
    camera: str
    total: int = 0
    ok: int = 0
    ng: int = 0
    first: str = ""
    last: str = ""
    jobs: list[str] = field(default_factory=list)
    ng_times: list[str] = field(default_factory=list)
    tags: dict[str, int] = field(default_factory=dict)
    picked: list[str] = field(default_factory=list)


@dataclass
class StationStat:
    station: str
    station_name: str
    cameras: list[CameraStat] = field(default_factory=list)
    products: int = 0
    ng: int = 0
    other_ng: int = 0
    visual_ng: int = 0

    @property
    def other_ng_auto(self) -> int:
        return sum(c.tags.get("misjudge", 0) + c.tags.get("external", 0)
                   for c in self.cameras)


@dataclass
class ReportStats:
    day: date
    hours: list[int]
    stations: list[StationStat] = field(default_factory=list)
    total_products: int = 0
    total_ng: int = 0
    adjusted_count: int = 0      # 有 OK 记录的相机数（已调完）
    pending_count: int = 0       # 启用但时段内无记录的相机数（剩下）
    warnings: list[str] = field(default_factory=list)


@dataclass
class CellSpec:
    row: int
    col: str
    text: Any = ""
    bold: bool = False
    size: float = 12
    color: str = "000000"
    align: str = "center"      # left|center|right
    valign: str = "center"     # top|center|bottom
    fill: str | None = None
    border: bool = True
    wrap: bool = True


@dataclass
class ImageSpec:
    row: int
    col: str
    data: bytes
    mime: str = "image/jpeg"
    width: int = 0       # px（显示尺寸）
    height: int = 0
    camera: str = ""
    ts: str = ""
    name: str = ""       # cellimages 中的图片名


@dataclass
class ColumnSpec:
    letter: str
    width: float
    hidden: bool = False


@dataclass
class LayoutModel:
    sheet_name: str
    title: str
    columns: list[ColumnSpec] = field(default_factory=list)
    rows: dict[int, float] = field(default_factory=dict)
    cells: list[CellSpec] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    images: list[ImageSpec] = field(default_factory=list)
    freeze: str = "B4"
    footer_start: int = 0

    def cell_map(self) -> dict[tuple[int, str], CellSpec]:
        return {(c.row, c.col): c for c in self.cells}


@dataclass
class ReportArtifact:
    xlsx_bytes: bytes
    pdf_bytes: bytes
    filename_stem: str
    stats: ReportStats
    layout: LayoutModel | None = None
    warnings: list[str] = field(default_factory=list)


# ---------- 统计 ----------

def hour_ranges(day: date, hours: list[int]) -> list[tuple[datetime, datetime]]:
    """小时集合 → 合并相邻小时后的半开区间。"""
    picked = sorted({int(h) for h in hours if 0 <= int(h) <= 23})
    ranges: list[tuple[datetime, datetime]] = []
    for hour in picked:
        start = datetime(day.year, day.month, day.day, hour)
        end = start + timedelta(hours=1)
        if ranges and ranges[-1][1] == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


def ranges_iso(ranges: list[tuple[datetime, datetime]]) -> list[tuple[str, str]]:
    return [(a.isoformat(timespec="milliseconds"), b.isoformat(timespec="milliseconds"))
            for a, b in ranges]


def format_hours(ranges: list[tuple[datetime, datetime]]) -> str:
    """`08:00-11:00、14:00-15:00（共 4 小时）`"""
    if not ranges:
        return "未选择时段"
    total = sum(int((b - a).total_seconds() // 3600) for a, b in ranges)
    text = "、".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in ranges)
    return f"{text}（共 {total} 小时）"


def station_of(camera: CameraConfig) -> tuple[str, str]:
    """(工站号, 工站名)：优先配置，其次按相机名前缀 OPxx 推断。"""
    station = (camera.station or "").strip()
    if not station:
        match = re.match(STATION_PATTERN, camera.name or "")
        station = match.group(1) if match else ""
    name = (camera.station_name or "").strip()
    return station or camera.name, name


def _aggregate(values: list[int], mode: str) -> int:
    values = [int(v) for v in values if v is not None]
    if not values:
        return 0
    if mode == "max":
        return max(values)
    if mode == "first":
        return values[0]
    return int(round(statistics.median(values)))


def collect_stats(storage, cameras: list[CameraConfig], options: ReportOptions,
                  config: ReportConfig) -> ReportStats:
    """按小时区间聚合结果单元格与 tag，得到逐站/逐相机统计。"""
    ranges = hour_ranges(options.day, options.hours)
    iso = ranges_iso(ranges)
    day_text = options.day.isoformat()
    enabled = [c for c in cameras if c.enabled and (not options.cameras or c.name in options.cameras)]
    stats = ReportStats(day=options.day, hours=sorted(set(int(h) for h in options.hours)))

    rows: dict[str, tuple] = {}
    tag_map = storage.list_report_tags(day_text)

    groups: dict[tuple[str, str], StationStat] = {}
    for camera in enabled:
        station, station_name = station_of(camera)
        block = groups.setdefault((station, station_name),
                                  StationStat(station=station, station_name=station_name))
        cam = CameraStat(camera=camera.name)
        cell = result_cell(camera)
        passes, fails = result_tokens(cell) if cell else ((), ())
        row = (storage.range_result_summary(camera.name, cell.name, iso, passes, fails)
               if cell else None)
        if row:
            rows[camera.name] = row
        if cell and extra_result_cells(camera):
            stats.warnings.append(f"{camera.name} 配置了多个结果单元格，仅使用 {cell.name}")
        if row:
            cam.total, cam.ok, cam.ng = int(row[2] or 0), int(row[3] or 0), int(row[4] or 0)
            cam.first, cam.last = str(row[5] or ""), str(row[6] or "")
            cam.ng_times = storage.range_ng_timestamps(camera.name, cell.name, iso, passes, fails)
            cam.jobs = [job for job, _ in storage.range_jobs(camera.name, iso)]
            cam.tags = storage.range_tag_counts(camera.name, iso)
        elif cell:
            stats.warnings.append(f"{camera.name} 在所选时段内无记录")
        else:
            stats.warnings.append(f"{camera.name} 未配置结果单元格，已跳过统计")
        ng_set = set(cam.ng_times)
        picked = [ts for ts in options.picked_images.get(camera.name, [])
                  if ts and ts in ng_set]
        if picked:
            cam.picked = picked[:MAX_NG_IMAGES]
        else:
            cam.picked = auto_pick(cam.ng_times, tag_map, camera.name)
        block.cameras.append(cam)

    for (station, _), block in groups.items():
        block.products = _aggregate([c.total for c in block.cameras], config.op_aggregate)
        block.ng = max((c.ng for c in block.cameras), default=0)
        override = options.other_ng.get(station)
        if override is None and len(block.cameras) == 1:
            override = options.other_ng.get(block.cameras[0].camera)
        if override is None:
            block.other_ng = min(block.ng, block.other_ng_auto)
        else:
            block.other_ng = max(0, min(block.ng, int(override)))
        block.visual_ng = max(0, block.ng - block.other_ng)
        stats.stations.append(block)

    stats.stations.sort(key=lambda s: s.station)
    stats.total_products = sum(s.products for s in stats.stations)
    stats.total_ng = sum(s.ng for s in stats.stations)
    stats.adjusted_count = sum(1 for c in enabled
                               if c.name in rows and int(rows[c.name][3] or 0) > 0)
    stats.pending_count = sum(1 for c in enabled if c.name not in rows)
    stats.hours = sorted({int(h) for h in options.hours})
    return stats


def auto_pick(ng_times: list[str], tag_map: dict[tuple[str, str], str],
              camera: str) -> list[str]:
    """自动抽图：优先未标注 tag 的 NG，均匀分布，最多 12 张。"""
    untagged = [ts for ts in ng_times if (camera, ts) not in tag_map]
    pool = untagged or list(ng_times)
    if len(pool) <= MAX_NG_IMAGES:
        return pool
    step = len(pool) / MAX_NG_IMAGES
    return [pool[min(len(pool) - 1, int(i * step))] for i in range(MAX_NG_IMAGES)]


def auto_reason(cam: CameraStat, camera: CameraConfig | None,
                ranges: list[tuple[datetime, datetime]]) -> str:
    """自动事实摘要（不含因果结论）；草稿里有已审核文本时由调用方优先使用。"""
    if cam.ng <= 0:
        return "无 NG，维持现状"
    rate = (cam.ng / cam.total * 100) if cam.total else 0.0
    cell = _result_cell(camera)
    notes = (cell.note or "").strip() if cell else ""
    if cell and (cell.lower is not None or cell.upper is not None):
        lower = "—" if cell.lower is None else f"{cell.lower:g}"
        upper = "—" if cell.upper is None else f"{cell.upper:g}"
        threshold = f"{lower}~{upper}"
    else:
        threshold = "—"
    jobs = "、".join(cam.jobs[:3]) or "—"
    first_t = _hhmm(cam.ng_times[0]) if cam.ng_times else "—"
    last_t = _hhmm(cam.ng_times[-1]) if cam.ng_times else "—"
    streak = _max_streak(cam.ng_times)
    tail = ""
    if streak[1] >= 3:
        tail = f"；{_hhmm(streak[0])} 起连续 {streak[1]} 件 NG，疑似工装/来料异常"
    return (f"视觉NG {cam.ng} 件（不良率 {rate:.1f}%）；检测项：{notes or '—'}；"
            f"判定阈值：{threshold}；统计时段：{format_hours(ranges)}；"
            f"NG 时段：{first_t}~{last_t}；配方：{jobs}{tail}")


def _result_cell(camera: CameraConfig | None):
    return result_cell(camera) if camera is not None else None


def _hhmm(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ts[-12:-4] if len(ts) >= 12 else ts


def _max_streak(times: list[str]) -> tuple[str, int]:
    """连续 NG 的最大长度（按时间相邻记录近似判断）。"""
    if not times:
        return "", 0
    best_start, best_len = times[0], 1
    start, length = times[0], 1
    for prev, cur in zip(times, times[1:]):
        try:
            gap = (datetime.fromisoformat(cur) - datetime.fromisoformat(prev)).total_seconds()
        except (TypeError, ValueError):
            gap = 999
        # 相邻 NG 间隔 <= 10 分钟视为连续
        if gap <= 600:
            length += 1
        else:
            start, length = cur, 1
        if length > best_len:
            best_start, best_len = start, length
    return best_start, best_len


# ---------- 版面模型 ----------

def camera_rows(stats: ReportStats) -> dict[str, int]:
    """相机 → 主表上行行号（第 4 行起，每相机固定 2 行）。"""
    rows: dict[str, int] = {}
    row = 4
    for block in stats.stations:
        for cam in block.cameras:
            rows[cam.camera] = row
            row += 2
    return rows


def build_layout(stats: ReportStats, options: ReportOptions, *,
                 project_name: str = "", destination: str = "",
                 line_name: str = "", created_at: datetime | None = None,
                 created_by: str = "", db_backend: str = "",
                 cameras: list[CameraConfig] | None = None) -> LayoutModel:
    """按模板生成主表版面（不含图片字节，图片由调用方回填 ImageSpec）。"""
    created_at = created_at or datetime.now()
    title = SHEET_TITLE_FMT.format(project=project_name or "")
    layout = LayoutModel(
        sheet_name=f"{options.day.month}月{options.day.day}日",
        title=title,
        columns=[ColumnSpec(letter, width, hidden)
                 for letter, width, hidden in COLUMNS],
    )
    ranges = hour_ranges(options.day, options.hours)
    camera_map = {c.name: c for c in (cameras or [])}

    # 第 1 行：标题
    layout.merges.append("B1:X1")
    layout.rows[1] = 28
    layout.cells.append(CellSpec(1, "B", title, bold=True, size=18, border=False))

    # 第 2 行：生成时间 / 目的地 / 线体（避开默认隐藏的 E、F 列）
    layout.rows[2] = 20
    layout.merges.extend(["B2:C2", "G2:J2", "L2:P2", "Q2:X2"])
    layout.cells.extend([
        CellSpec(2, "B", f"生成时间：{created_at:%Y-%m-%d %H:%M}", align="left"),
        CellSpec(2, "D", f"目的地：{destination or '—'}", align="left"),
        CellSpec(2, "G", f"线体：{line_name or '—'}", align="left"),
        CellSpec(2, "L", f"统计时段：{format_hours(ranges)}", align="left"),
        CellSpec(2, "Q", f"编制：{created_by or '—'}", align="left"),
    ])

    # 第 3 行：表头
    layout.rows[3] = ROW_HEIGHT_HEADER
    for letter, label in HEADER_LABELS.items():
        layout.cells.append(CellSpec(3, letter, label, bold=True, size=11,
                                     color="FFFFFF", fill=HEADER_FILL))
    for index in range(NG_IMAGE_COLUMNS):
        letter = _col_letter(13 + index)  # M=13 起
        layout.cells.append(CellSpec(3, letter, str(index + 1), bold=True, size=11,
                                     color="FFFFFF", fill=HEADER_FILL))

    # 数据区：一相机 2 行，同工站纵向合并 C/G/H/I/J
    rows = camera_rows(stats)
    for index, block in enumerate(stats.stations, start=1):
        if not block.cameras:
            continue
        start_row = rows[block.cameras[0].camera]
        for cam in block.cameras:
            row = rows[cam.camera]
            layout.rows[row] = ROW_HEIGHT_DATA
            layout.rows[row + 1] = ROW_HEIGHT_DATA
            camera = camera_map.get(cam.camera)
            model = (camera.model if camera and camera.model else "").strip()
            layout.cells.extend([
                CellSpec(row, "B", index, align="left"),
                CellSpec(row, "C", f"{block.station} {block.station_name}".strip()),
                CellSpec(row, "D", f"{model} {cam.camera}".strip()),
                CellSpec(row + 1, "D", block.station_name or cam.camera),
                CellSpec(row, "F", ""),
                CellSpec(row, "K", "", fill=HEADER_FILL),
                CellSpec(row + 1, "K", "", fill=HEADER_FILL),
                CellSpec(row, "L", "NG图片："),
                CellSpec(row + 1, "L", "原因分析及改造解决方案："),
                CellSpec(row + 1, "M",
                         (options.reasons.get(cam.camera) or "").strip()
                         or auto_reason(cam, camera, ranges),
                         align="left", size=11, wrap=True),
            ])
            layout.merges.append(f"B{row}:B{row + 1}")
            layout.merges.append(f"M{row + 1}:X{row + 1}")
        end_row = rows[block.cameras[-1].camera] + 1
        layout.merges.append(f"C{start_row}:C{end_row}")
        for letter in ("G", "H", "I", "J"):
            layout.merges.append(f"{letter}{start_row}:{letter}{end_row}")
        layout.cells.extend([
            CellSpec(start_row, "G", block.products, size=16, bold=True),
            CellSpec(start_row, "H", block.ng, size=16, bold=True),
            CellSpec(start_row, "I", block.visual_ng, size=16, bold=True),
            CellSpec(start_row, "J", block.other_ng, size=16, bold=True),
        ])

    # 底部汇总
    last = (max(rows.values()) + 1) if rows else 3
    layout.footer_start = last + 1
    for offset in (1, 2, 3):
        layout.merges.append(f"B{last + offset}:X{last + offset}")
        layout.rows[last + offset] = 20 if offset < 3 else 24
    layout.merges.append(f"B{last + 4}:X{last + 4}")
    layout.rows[last + 4] = 18
    rate = (stats.total_ng / stats.total_products * 100) if stats.total_products else 0.0
    layout.cells.extend([
        CellSpec(last + 1, "B", f"已调完:{stats.adjusted_count} 台", align="left", bold=True),
        CellSpec(last + 2, "B", f"剩下:{stats.pending_count} 台", align="left", bold=True),
        CellSpec(last + 3, "B",
                 f"合计：生产 {stats.total_products} 件，NG {stats.total_ng} 件（{rate:.1f}%）",
                 align="left", bold=True, size=14),
        CellSpec(last + 4, "B",
                 f"统计时段：{format_hours(ranges)}；口径：视觉NG=NG总数-其他NG（误判+外部原因）；"
                 f"生成时间：{created_at:%Y-%m-%d %H:%M}；数据库：{db_backend or '—'}",
                 align="left", size=9, color="808080", border=False, wrap=False),
    ])
    return layout


def build_audit_layout(stats: ReportStats, *, day: date,
                       cameras: list[CameraConfig] | None = None) -> LayoutModel:
    """第二张表「数据核对」：逐相机 OK/NG 与 tag 分布，供工程师核对改数。"""
    camera_map = {c.name: c for c in (cameras or [])}
    headers = ("日期", "工站", "相机", "总记录", "OK", "NG", "误判", "产品自身NG",
               "外部原因", "未标注", "产品数", "视觉NG", "其他NG")
    layout = LayoutModel(sheet_name="数据核对", title="数据核对",
                         columns=[ColumnSpec(_col_letter(i + 1), 16) for i in range(len(headers))],
                         freeze="A2")
    layout.rows[1] = ROW_HEIGHT_HEADER
    for index, label in enumerate(headers, start=1):
        layout.cells.append(CellSpec(1, _col_letter(index), label, bold=True, size=11,
                                     color="FFFFFF", fill=HEADER_FILL))
    row = 2
    for block in stats.stations:
        for cam in block.cameras:
            tagged = sum(cam.tags.get(t, 0) for t in ("misjudge", "product", "external"))
            values = (day.isoformat(), block.station, cam.camera, cam.total, cam.ok, cam.ng,
                      cam.tags.get("misjudge", 0), cam.tags.get("product", 0),
                      cam.tags.get("external", 0), max(0, cam.ng - tagged),
                      block.products, block.visual_ng, block.other_ng)
            layout.rows[row] = 20
            for index, value in enumerate(values, start=1):
                layout.cells.append(CellSpec(row, _col_letter(index), value, size=11))
            row += 1
    return layout


_TICKET_HEADERS = (
    ("工单号", 10), ("标题", 42), ("状态", 10), ("关闭原因", 14), ("类别", 12),
    ("工站", 12), ("责任人", 16), ("计划完成日", 14), ("创建时间", 18),
    ("更新时间", 18), ("改善措施摘要", 56),
)

STATUS_LABELS = {"open": "开启中", "in_progress": "进行中", "done": "已完成",
                 "closed": "关闭", "reopened": "已重开"}


def build_ticket_layout(tickets: list[dict], *, sheet_name: str = "工单跟踪") -> LayoutModel:
    """Sheet 2「工单跟踪」：本次报告关联的工单与改善措施。"""
    layout = LayoutModel(
        sheet_name=sheet_name, title=sheet_name,
        columns=[ColumnSpec(_col_letter(i + 1), width)
                 for i, (_, width) in enumerate(_TICKET_HEADERS)],
        freeze="A2",
    )
    layout.rows[1] = ROW_HEIGHT_HEADER
    for index, (label, _) in enumerate(_TICKET_HEADERS, start=1):
        layout.cells.append(CellSpec(1, _col_letter(index), label, bold=True, size=11,
                                     color="FFFFFF", fill=HEADER_FILL))
    row = 2
    for ticket in tickets:
        values = (
            f"#{ticket.get('id')}",
            ticket.get("title") or "",
            STATUS_LABELS.get(str(ticket.get("status") or ""), ticket.get("status") or ""),
            ticket.get("close_reason") or "",
            ticket.get("category") or "",
            ticket.get("station") or "",
            ticket.get("assignee") or "",
            ticket.get("due_date") or "",
            (ticket.get("created_at") or "")[:16].replace("T", " "),
            (ticket.get("updated_at") or "")[:16].replace("T", " "),
            (ticket.get("measure") or "").strip(),
        )
        layout.rows[row] = 36
        for index, value in enumerate(values, start=1):
            layout.cells.append(CellSpec(row, _col_letter(index), value, size=10,
                                         align="left" if index in (2, 11) else "center",
                                         wrap=True))
        row += 1
    return layout


_COL_CACHE: dict[int, str] = {}

def _col_letter(index: int) -> str:
    """1 → A，27 → AA。"""
    cached = _COL_CACHE.get(index)
    if cached:
        return cached
    letters = ""
    value = index
    while value > 0:
        value, rem = divmod(value - 1, 26)
        letters = chr(65 + rem) + letters
    _COL_CACHE[index] = letters
    return letters


# ---------- xlsx 输出 ----------

def layout_to_xlsx(layout: LayoutModel, extras: list[LayoutModel] | None = None,
                   *, images: list[ImageSpec] | None = None,
                   floating_images: bool = False) -> bytes:
    """把版面模型写成 xlsx。

    图片默认**嵌入单元格**（`DISPIMG`）：锚点单元格只留出带样式的空位，由
    `wpsdispimg.inject_dispimg` 在 zip 层写入 `_xlfn.DISPIMG` 公式。
    `floating_images=True` 仅用于注入失败时的兜底，会把图片写成浮动图片。
    """
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.cell.cell import MergedCell
    from openpyxl.drawing.image import Image as WSImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import range_boundaries
    from openpyxl.worksheet.properties import PageSetupProperties

    workbook = Workbook()
    workbook.remove(workbook.active)
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for model, picture_list in [(layout, images if images is not None else layout.images)] + [
            (extra, extra.images) for extra in (extras or [])]:
        ws = workbook.create_sheet(model.sheet_name)
        for column in model.columns:
            ws.column_dimensions[column.letter].width = column.width
            ws.column_dimensions[column.letter].hidden = column.hidden
        for row_index, height in model.rows.items():
            ws.row_dimensions[row_index].height = height
        for spec in model.cells:
            cell = ws[f"{spec.col}{spec.row}"]
            cell.value = spec.text if spec.text != "" else None
            cell.font = Font(name="微软雅黑", size=spec.size, bold=spec.bold,
                             color=spec.color)
            cell.alignment = Alignment(horizontal=spec.align, vertical=spec.valign,
                                       wrap_text=spec.wrap)
            if spec.border:
                cell.border = border
            if spec.fill:
                cell.fill = PatternFill("solid", fgColor=spec.fill)
        # 合并区域内的单元格也要有边框，否则 Excel 显示缺边
        for merge in model.merges:
            ws.merge_cells(merge)
            min_col, min_row, max_col, max_row = range_boundaries(merge)
            for row_index in range(min_row, max_row + 1):
                for col_index in range(min_col, max_col + 1):
                    cell = ws.cell(row=row_index, column=col_index)
                    cell.border = border
                    if (row_index, col_index) != (min_row, min_col) \
                            and not isinstance(cell, MergedCell):
                        cell.value = None
        for picture in picture_list:
            if not floating_images:
                # 嵌入单元格模式：只保留有样式的空单元格，供后续写 DISPIMG 公式
                cell = ws[f"{picture.col}{picture.row}"]
                cell.value = None
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = border
                continue
            try:
                image = WSImage(BytesIO(picture.data))
                if picture.width and picture.height:
                    image.width, image.height = picture.width, picture.height
                ws.add_image(image, f"{picture.col}{picture.row}")
            except Exception as exc:  # 图片损坏不应中断整份报告
                log.warning("嵌入图片失败 %s@%s：%s", picture.camera, picture.ts, exc)
        if model.freeze:
            ws.freeze_panes = model.freeze

    ws = workbook[layout.sheet_name]
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = 8          # A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "3:3"

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def xlsx_filename(stem: str, day: date) -> str:
    return f"{stem}_{day:%Y%m%d}.xlsx"


def pdf_filename(stem: str, day: date) -> str:
    return f"{stem}_{day:%Y%m%d}.pdf"


# ---------- 报告生成（内存产物，不写库/不落盘）----------

IMAGE_CELL = DEFAULT_IMAGE_CELL  # 默认图片单元格，仅作兜底
FILENAME_STEM = "视觉改善分析报告"


def build_daily_report(storage, cameras: list[CameraConfig], options: ReportOptions,
                       config: ReportConfig, *, project_name: str = "",
                       destination: str = "", line_name: str = "",
                       created_by: str = "", db_backend: str = "",
                       tickets: list[dict] | None = None,
                       include_ticket_sheet: bool = True,
                       progress=None, cancel=None) -> ReportArtifact:
    """统计 → 取图 → 建表 → DISPIMG → PDF，返回内存产物。

    `progress(phase, done, total, message)` 用于上报阶段进度；
    `cancel()` 返回 True 时在检查点抛 `tasks.CancelledError`（调用方据此收尾）。
    """
    from .tasks import CancelledError

    def tick(phase: str, done: int = 0, total: int = 0, message: str = "") -> None:
        if progress is not None:
            progress(phase, done, total, message)

    def check() -> None:
        if cancel is not None and cancel():
            raise CancelledError("任务已取消")

    now = datetime.now()
    warnings: list[str] = []

    camera_map = {c.name: c for c in cameras}

    def image_name(camera: str) -> str:
        cfg = camera_map.get(camera)
        cell = image_cell(cfg) if cfg is not None else None
        return cell.name if cell is not None else IMAGE_CELL

    tick("stats", 0, 1, "统计结果数据")
    stats = collect_stats(storage, cameras, options, config)
    warnings.extend(stats.warnings)
    check()

    layout = build_layout(stats, options, project_name=project_name,
                          destination=destination, line_name=line_name,
                          created_at=now, created_by=created_by,
                          db_backend=db_backend, cameras=cameras)
    extras: list[LayoutModel] = []
    if include_ticket_sheet and tickets:
        extras.append(build_ticket_layout(tickets))
    extras.append(build_audit_layout(stats, day=options.day, cameras=cameras))

    rows = camera_rows(stats)
    limit = max(1, min(MAX_NG_IMAGES, int(config.ng_images_per_station)))
    total_pics = sum(min(len(cam.picked), limit)
                     for block in stats.stations for cam in block.cameras)
    pictures: list[ImageSpec] = []
    done = 0
    for block in stats.stations:
        for cam in block.cameras:
            picks = cam.picked[:limit]
            if not picks:
                continue
            images = _load_ng_images(storage, cam.camera, picks, config, warnings,
                                     image_name(cam.camera))
            for index, ts in enumerate(picks):
                done += 1
                tick("images", done, total_pics, f"取图 {done}/{total_pics}")
                check()
                data = images.get(ts)
                if not data:
                    continue
                pictures.append(ImageSpec(
                    row=rows[cam.camera], col=_col_letter(13 + index), data=data,
                    camera=cam.camera, ts=ts))
    layout.images = pictures

    # OK 参考图（F 列，隐藏列）：取时段内 OK 记录时间中位数附近一帧
    tick("images", total_pics, total_pics, "取 OK 参考图")
    ok_count = 0
    iso = ranges_iso(hour_ranges(options.day, options.hours))
    for block in stats.stations:
        for cam in block.cameras:
            if cam.ok <= 0:
                continue
            camera_cfg = camera_map.get(cam.camera)
            result = result_cell(camera_cfg) if camera_cfg is not None else None
            if result is None:
                continue
            passes, fails = result_tokens(result)
            ok_times = storage.range_ok_timestamps(
                cam.camera, result.name, iso, pass_values=passes, fail_values=fails)
            if not ok_times:
                continue
            ok_ts = ok_times[len(ok_times) // 2]
            data = _load_frame(storage, cam.camera, ok_ts, config, warnings,
                               image_name(cam.camera))
            ok_count += 1
            if ok_count % 3 == 0:
                check()
            if not data:
                continue
            pictures.append(ImageSpec(row=rows[cam.camera], col="F", data=data,
                                      camera=cam.camera, ts=ok_ts))
            layout.images = pictures

    tick("xlsx", 0, 1, "生成表格")
    check()
    xlsx = layout_to_xlsx(layout, extras, images=pictures)
    check()

    if pictures:
        from .wpsdispimg import DispImage, inject_dispimg

        tick("dispimg", 0, len(pictures), "写入嵌入单元格图片")
        try:
            xlsx = inject_dispimg(
                xlsx,
                [DispImage(cell=f"{p.col}{p.row}", data=p.data) for p in pictures],
                sheet_name=layout.sheet_name)
        except Exception as exc:                 # 兜底：保证仍能出文件
            log.exception("DISPIMG 注入失败，降级为浮动图片")
            warnings.append(f"嵌入单元格图片失败，已降级为浮动图片：{exc}")
            xlsx = layout_to_xlsx(layout, extras, images=pictures,
                                  floating_images=True)
        check()

    tick("pdf", 0, 1, "渲染 PDF")
    check()
    from . import pdfrender

    pdf_bytes = b""
    try:
        pdf_bytes = pdfrender.render_pdf(
            layout, images=pictures, hours_text=format_hours(hour_ranges(
                options.day, options.hours)),
            generated_at=f"{now:%Y-%m-%d %H:%M}", warnings=warnings)
    except Exception as exc:
        log.exception("PDF 渲染失败")
        warnings.append(f"PDF 生成失败：{exc}")

    return ReportArtifact(
        xlsx_bytes=xlsx, pdf_bytes=pdf_bytes,
        filename_stem=f"{FILENAME_STEM}_{now:%Y%m%d%H%M%S}",
        stats=stats, layout=layout, warnings=warnings)


FRAME_WINDOW = 5.0         # NG 事件与证据帧的最大时差（秒）


def _load_ng_images(storage, camera: str, picks: list[str], config: ReportConfig,
                    warnings: list[str], image_cell_name: str = IMAGE_CELL) -> dict[str, bytes]:
    """按时间戳取 NG 渲染图（缺失回退原图），返回 ts → JPEG 字节。

    证据帧按 §4.5：取 `|frame_ts - event_ts|` 最小且 ≤ 5s 的图片单元格帧，同一帧只用一次。
    """
    try:
        start = datetime.fromisoformat(picks[0]) - timedelta(seconds=FRAME_WINDOW)
        end = datetime.fromisoformat(picks[-1]) + timedelta(seconds=FRAME_WINDOW)
    except ValueError:
        return {}
    try:
        from .videocodec import load_images

        rows = load_images(storage, camera, image_cell_name, start=start, end=end)
    except Exception as exc:
        log.warning("读取 %s 图像失败：%s", camera, exc)
        warnings.append(f"{camera} 图像读取失败：{exc}")
        return {}

    render, fallback = [], []
    for ts, kind, _mime, _width, _height, data in rows:
        try:
            stamp = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        (render if kind == "render" else fallback).append((stamp, str(ts), data))

    result: dict[str, bytes] = {}
    used: set[str] = set()
    missing = 0
    for ts in picks:
        try:
            center = datetime.fromisoformat(ts)
        except ValueError:
            missing += 1
            continue
        frame = _nearest_frame(render, center, used) or _nearest_frame(fallback, center, used)
        if frame is None:
            missing += 1
            continue
        used.add(frame[1])
        result[ts] = frame[2]
    if missing:
        warnings.append(f"{camera} 有 {missing} 张 NG 图片缺失")
    return result


def _nearest_frame(frames: list[tuple[datetime, str, bytes]], center: datetime,
                   used: set[str], window: float = FRAME_WINDOW
                   ) -> tuple[datetime, str, bytes] | None:
    best = None
    best_gap = None
    for frame in frames:
        if frame[1] in used:
            continue
        gap = abs((frame[0] - center).total_seconds())
        if gap > window or (best_gap is not None and gap >= best_gap):
            continue
        best, best_gap = frame, gap
    return best


def _load_frame(storage, camera: str, ts: str, config: ReportConfig,
                warnings: list[str], image_cell_name: str = IMAGE_CELL) -> bytes | None:
    """取该时刻最近的一帧（渲染图优先），优先 ±5s，其次逐级放宽窗口。"""
    from . import images as image_mod

    try:
        center = datetime.fromisoformat(ts)
    except ValueError:
        return None
    for window in (FRAME_WINDOW, 60.0, 300.0):
        try:
            from .videocodec import load_images

            rows = load_images(storage, camera, image_cell_name,
                               start=center - timedelta(seconds=window),
                               end=center + timedelta(seconds=window))
        except Exception as exc:
            log.warning("读取 %s 图像失败：%s", camera, exc)
            return None
        render, fallback = [], []
        for frame_ts, kind, _mime, _width, _height, data in rows:
            try:
                stamp = datetime.fromisoformat(str(frame_ts))
            except ValueError:
                continue
            (render if kind == "render" else fallback).append(
                (stamp, str(frame_ts), data))
        frame = _nearest_frame(render, center, set(), window) or \
            _nearest_frame(fallback, center, set(), window)
        if frame is None:
            continue
        try:
            return image_mod.to_jpeg(frame[2], max_width=config.image_max_edge)[0]
        except Exception as exc:
            log.warning("缩图失败 %s@%s：%s", camera, ts, exc)
            return None
    return None
