"""LayoutModel → A3 横版单页 PDF（米黄纸底），供打印与客户交付。

与 xlsx 共用 `report.LayoutModel`：列宽/行高/合并/文字/图片框位都按同一坐标换算，
因此两边的数据与文字必然一致。设计见 `docs/当日改善报告功能设计.md` §5.4：

- 页面 A3 横向 420×297 mm，页边距 8 mm，整页铺米黄 `#FBF3DC`；
- 表格线 0.5pt `#C9C2A6`，外框 1pt 深灰；表头 `#00B050` 白字；
- 图片按单元格等比居中（白底留边），字节直接来自生成时的 JPEG；
- 隐藏列（实物图片等）不占宽度也不绘制；
- 恒为 1 页：缩放不足时先递减每站图片列数，再按最小 5pt 缩字号，仍溢出则记告警。
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .report import CellSpec, ImageSpec, LayoutModel

log = logging.getLogger(__name__)

PAGE = landscape(A3)                  # 420×297 mm
MARGIN = 8 * mm
FOOTER_H = 10 * mm
BACKGROUND = colors.HexColor("#FBF3DC")
GRID = colors.HexColor("#C9C2A6")
OUTER = colors.HexColor("#5A5A5A")
HEADER_FILL = colors.HexColor("#00B050")
PLACEHOLDER = colors.HexColor("#B8B2A0")
EMPTY_ROW_HEIGHT = 15.0               # 未显式设置行高的行按 Excel 默认 15pt
MIN_SCALE = 0.55                      # 低于此缩放先减图再缩字号
MIN_FONT = 5.0
IMAGE_PAD = 1.5

_FONT_NAME = ""                       # 注册后的中文字体族名（懒加载）
_CELL_RE = re.compile(r"^\$?([A-Z]+)\$?(\d+)$")


# ---------- 字体 ----------

def _assets_dir() -> Path:
    """开发环境取仓库 assets；PyInstaller 取 _MEIPASS 内打包的 assets。"""
    import sys

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "assets"
    return Path(__file__).resolve().parent.parent.parent / "assets"


def _font_candidates() -> list[Path]:
    assets = _assets_dir()
    candidates = [assets / "Smiley Sans Oblique.ttf", assets / "ZhuqueFangsong-Regular.ttf"]
    for name in ("msyh.ttc", "msyhl.ttc", "simhei.ttf", "simsun.ttc"):
        candidates.append(Path("C:/Windows/Fonts") / name)
    return candidates


def register_font() -> str:
    """注册中文字体并返回族名；全部缺失时回退 Helvetica（中文会显示方框）。"""
    global _FONT_NAME
    if _FONT_NAME:
        return _FONT_NAME
    for path in _font_candidates():
        if not path.exists():
            continue
        try:
            name = "ReportCN" if path.suffix.lower() == ".ttf" else "ReportCN"
            pdfmetrics.registerFont(TTFont(name, str(path)))
            _FONT_NAME = name
            log.debug("PDF 字体：%s", path)
            return name
        except Exception as exc:
            log.warning("PDF 字体注册失败 %s：%s", path, exc)
            continue
    log.warning("未找到可用中文字体，PDF 中文可能显示异常")
    _FONT_NAME = "Helvetica"
    return _FONT_NAME


# ---------- 版面几何 ----------

def _col_index(letter: str) -> int:
    value = 0
    for char in letter.upper():
        value = value * 26 + (ord(char) - 64)
    return value


def _col_width_pt(width: float) -> float:
    """Excel 字符宽度 → 磅（96 DPI 下 px = width*7+5，1px = 0.75pt）。"""
    return (max(0.0, width) * 7 + 5) * 0.75


def _max_row(layout: LayoutModel) -> int:
    rows = set(layout.rows)
    rows.update(cell.row for cell in layout.cells)
    rows.update(image.row for image in layout.images)
    for merge in layout.merges:
        for part in merge.split(":"):
            match = _CELL_RE.match(part.strip())
            if match:
                rows.add(int(match.group(2)))
    return max(rows) if rows else 1


def _merge_box(ref: str) -> tuple[int, int, int, int] | None:
    """`B4:C5` → (row1, col1, row2, col2)；非法引用返回 None。"""
    start, _, end = ref.partition(":")
    first = _CELL_RE.match(start.strip())
    second = _CELL_RE.match((end or start).strip())
    if not first or not second:
        return None
    row1, row2 = int(first.group(2)), int(second.group(2))
    col1, col2 = _col_index(first.group(1)), _col_index(second.group(1))
    return min(row1, row2), min(col1, col2), max(row1, row2), max(col1, col2)


class _Geometry:
    """像素列/行坐标（单位 pt，未缩放）；图片列按 `image_limit` 与实际用图数裁掉。"""

    def __init__(self, layout: LayoutModel, image_limit: int,
                 used_images: set[str] | None = None):
        self.image_letters: list[str] = []
        self.x: dict[str, float] = {}
        self.width: dict[str, float] = {}
        cursor = 0.0
        last_col = 0
        for column in layout.columns:
            last_col = max(last_col, _col_index(column.letter))
            if _is_image_col(column.letter):
                self.image_letters.append(column.letter)
        kept = set(self.image_letters[:max(1, image_limit)])
        if used_images:
            # 只有前 N 列真正放了图：其余尾部图片列在 PDF 里不占宽度
            last_used = max((self.image_letters.index(letter)
                             for letter in used_images if letter in self.image_letters),
                            default=-1)
            kept &= set(self.image_letters[:last_used + 1])
        for column in layout.columns:
            hidden = column.hidden or (_is_image_col(column.letter)
                                       and column.letter not in kept)
            width = 0.0 if hidden else _col_width_pt(column.width)
            self.x[column.letter] = cursor
            self.width[column.letter] = width
            cursor += width
        self.total_width = cursor
        self.max_col = last_col

        self.y: dict[int, float] = {}
        self.height: dict[int, float] = {}
        cursor = 0.0
        max_row = _max_row(layout)
        for row in range(1, max_row + 1):
            height = float(layout.rows.get(row, EMPTY_ROW_HEIGHT))
            self.y[row] = cursor
            self.height[row] = height
            cursor += height
        self.total_height = cursor
        self.kept_images = kept
        self.max_row = max_row

    def left(self, col: int) -> float:
        letter = _letter(col)
        return self.x.get(letter, 0.0)

    def right(self, col: int) -> float:
        letter = _letter(col)
        return self.x.get(letter, 0.0) + self.width.get(letter, 0.0)

    def top(self, row: int) -> float:
        return self.total_height - self.y.get(row, 0.0)

    def bottom(self, row: int) -> float:
        return self.top(row) - self.height.get(row, EMPTY_ROW_HEIGHT)


def _letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _is_image_col(letter: str) -> bool:
    index = _col_index(letter)
    return index >= 13     # M 起为图片列（模板 12 列，可按需向右扩展）


# ---------- 文本排版 ----------

def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """按可用宽度折行：中文逐字断行，英文尽量在空格处断。"""
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in str(text):
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + char
        if pdfmetrics.stringWidth(candidate, font, size) <= max_width or not current:
            current = candidate
            continue
        # 英文单词尽量整体换行
        cut = current.rfind(" ")
        if cut > 0 and char != " " and current[cut + 1:].strip():
            lines.append(current[:cut])
            current = current[cut + 1:] + char
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def _draw_cell_text(c: canvas.Canvas, spec: CellSpec, rect: tuple[float, float, float, float],
                    font: str, scale: float) -> None:
    left, bottom, right, top = rect
    text = "" if spec.text is None else str(spec.text)
    if not text.strip() or right - left <= 1 or top - bottom <= 1:
        return
    size = max(MIN_FONT, spec.size * min(scale, 1.0))
    pad = 2.0
    width = right - left - 2 * pad
    lines = _wrap(text, font, size, width)
    if not lines:
        return
    line_height = size * 1.22
    total = line_height * len(lines)
    if spec.valign == "top":
        y = top - pad - size
    elif spec.valign == "bottom":
        y = bottom + pad + total - size * 0.9
    else:
        y = top - max(pad, (top - bottom - total) / 2) - size * 0.88
    c.saveState()
    clip = c.beginPath()
    clip.rect(left, bottom, right - left, top - bottom)
    c.clipPath(clip, stroke=0, fill=0)
    c.setFont(font, size)
    c.setFillColor(colors.HexColor("#" + spec.color))
    for line in lines:
        if y < bottom - size:
            break
        if spec.align == "left":
            c.drawString(left + pad, y, line)
        elif spec.align == "right":
            c.drawRightString(right - pad, y, line)
        else:
            c.drawCentredString((left + right) / 2, y, line)
        y -= line_height
    c.restoreState()


# ---------- 主渲染 ----------

def _draw_background(c: canvas.Canvas) -> None:
    c.setFillColor(BACKGROUND)
    c.rect(0, 0, PAGE[0], PAGE[1], stroke=0, fill=1)


def _draw_footer(c: canvas.Canvas, font: str, *, title: str, middle: str,
                 note: str = "") -> None:
    c.setFillColor(colors.HexColor("#3A3A3A"))
    c.setFont(font, 7.5)
    baseline = MARGIN - 3 * mm
    c.drawString(MARGIN, baseline, title)
    if middle:
        c.drawCentredString(PAGE[0] / 2, baseline, middle)
    c.drawRightString(PAGE[0] - MARGIN, baseline, "第 1 页 / 共 1 页")
    if note:
        c.setFont(font, 6.5)
        c.setFillColor(colors.HexColor("#8A8168"))
        c.drawRightString(PAGE[0] - MARGIN, baseline + 4 * mm, note)


def render_pdf(layout: LayoutModel, *, images: list[ImageSpec] | None = None,
               hours_text: str = "", generated_at: str = "",
               title: str = "视觉改善分析报告",
               warnings: list[str] | None = None) -> bytes:
    """把主表版面渲染成 1 页 A3 横向 PDF；返回 PDF 字节。"""
    font = register_font()
    pictures = images if images is not None else layout.images
    warnings = warnings if warnings is not None else []

    avail_w = PAGE[0] - 2 * MARGIN
    avail_h = PAGE[1] - 2 * MARGIN - FOOTER_H
    used_images = {picture.col for picture in pictures}
    max_images = max(1, sum(1 for column in layout.columns if _is_image_col(column.letter)))

    geometry = None
    scale = 0.0
    image_limit = 0
    full_geometry = None
    full_scale = 0.0
    for limit in range(max_images, 0, -1):
        candidate = _Geometry(layout, limit, used_images)
        if candidate.total_width <= 0 or candidate.total_height <= 0:
            continue
        factor = min(avail_w / candidate.total_width, avail_h / candidate.total_height)
        if full_geometry is None:
            full_geometry, full_scale = candidate, factor
        geometry, scale, image_limit = candidate, factor, limit
        if factor >= MIN_SCALE:
            break
    if geometry is None:
        raise ValueError("版面为空，无法渲染 PDF")
    if scale < MIN_SCALE and full_geometry is not None and full_scale >= scale - 0.05:
        # 减图也救不回可读比例（通常是行数过多、受页高限制），保留全部图片列更有价值
        geometry, scale, image_limit = full_geometry, full_scale, max_images
    note = ""
    if scale < MIN_SCALE:
        note = "已压缩至 1 页"
        warnings.append(f"PDF 缩放比例 {scale:.2f} 低于 {MIN_SCALE}，已压缩至 1 页")
        if image_limit < max_images:
            warnings.append(f"图片列已压缩为每站 {image_limit} 列")
    elif image_limit < max_images:
        dropped = max_images - image_limit
        note = f"图片列超出单页宽度，已省略每站后 {dropped} 列"
        warnings.append(note)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=PAGE)
    pdf.setTitle(f"{title} {hours_text}".strip())
    _draw_background(pdf)

    origin_x = MARGIN + max(0.0, (avail_w - geometry.total_width * scale) / 2)
    origin_y = PAGE[1] - MARGIN - max(0.0, (avail_h - geometry.total_height * scale) / 2)

    def rect_of(row1: int, col1: int, row2: int, col2: int):
        left = origin_x + geometry.left(col1) * scale
        right = origin_x + geometry.right(col2) * scale
        top = origin_y - geometry.y[row1] * scale
        bottom = origin_y - (geometry.y[row2] + geometry.height[row2]) * scale
        return left, bottom, right, top

    # 合并区域：锚点单元格负责绘制，其余成员跳过
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for merge in layout.merges:
        box = _merge_box(merge)
        if not box:
            continue
        row1, col1, row2, col2 = box
        spans[(row1, col1)] = (row2 - row1 + 1, col2 - col1 + 1)
        for row in range(row1, row2 + 1):
            for col in range(col1, col2 + 1):
                if (row, col) != (row1, col1):
                    covered.add((row, col))

    pdf.setLineWidth(0.5 * max(scale, 0.6))
    for spec in layout.cells:
        col = _col_index(spec.col)
        if (spec.row, col) in covered:
            continue
        if geometry.width.get(spec.col, 0.0) <= 0:
            continue
        row_span, col_span = spans.get((spec.row, col), (1, 1))
        rect = rect_of(spec.row, col, spec.row + row_span - 1, col + col_span - 1)
        left, bottom, right, top = rect
        if right - left <= 0 or top - bottom <= 0:
            continue
        if spec.fill:
            pdf.setFillColor(colors.HexColor("#" + spec.fill))
            pdf.rect(left, bottom, right - left, top - bottom, stroke=0, fill=1)
        if spec.border:
            pdf.setStrokeColor(GRID)
            pdf.rect(left, bottom, right - left, top - bottom, stroke=1, fill=0)
        _draw_cell_text(pdf, spec, rect, font, scale)

    # 图片：白底 + 等比居中
    allowed = geometry.kept_images
    for picture in pictures:
        if picture.col not in allowed or geometry.width.get(picture.col, 0.0) <= 0:
            continue
        rect = rect_of(picture.row, _col_index(picture.col),
                       picture.row, _col_index(picture.col))
        left, bottom, right, top = rect
        pad = IMAGE_PAD * max(scale, 0.5)
        pdf.setFillColor(colors.white)
        pdf.rect(left + pad / 2, bottom + pad / 2,
                 max(0.0, right - left - pad), max(0.0, top - bottom - pad),
                 stroke=0, fill=1)
        try:
            reader = ImageReader(BytesIO(picture.data))
            width, height = reader.getSize()
            box_w = right - left - 2 * pad
            box_h = top - bottom - 2 * pad
            if width <= 0 or height <= 0 or box_w <= 0 or box_h <= 0:
                continue
            factor = min(box_w / width, box_h / height)
            draw_w, draw_h = width * factor, height * factor
            pdf.drawImage(reader, left + (right - left - draw_w) / 2,
                          bottom + (top - bottom - draw_h) / 2,
                          width=draw_w, height=draw_h, mask="auto")
        except Exception as exc:
            log.warning("PDF 绘制图片失败 %s@%s：%s", picture.camera, picture.ts, exc)

    # 外框
    table_top = origin_y
    table_bottom = origin_y - geometry.total_height * scale
    pdf.setStrokeColor(OUTER)
    pdf.setLineWidth(1.0)
    pdf.rect(origin_x, table_bottom, geometry.total_width * scale,
             table_top - table_bottom, stroke=1, fill=0)

    middle = " · ".join(part for part in (
        f"统计时段 {hours_text}" if hours_text else "",
        f"生成于 {generated_at}" if generated_at else "",
    ) if part)
    _draw_footer(pdf, font, title=title, middle=middle, note=note)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
