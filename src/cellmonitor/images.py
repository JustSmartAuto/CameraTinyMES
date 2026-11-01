"""相机图像处理：原始图下载、缩放转码、A0 结果渲染图合成。

图像地址来源（按优先级）：
1. 单元格自身结果（HmiResult.cells 中对应条目）的 data：字符串 URL 或 data:base64；
2. 主采集视图 acqImageView.layers 中的 ImageLayer.url。

A0 的渲染图：取 acqImageView 中 GraphicsLayer 的 JSON，把检测图形按原图坐标
按比例叠加到缩放后的图像上（Pillow 合成），得到「图像 + 图形」的渲染图。

实现要点（实测于 In-Sight 固件 22.x）：
- 图像接口只返回 BMP，可用 ?sz=宽,高 请求缩放版本（原图 2448×2048 约 14MB）；
- GraphicsLayer JSON 坐标即原图像素坐标（Region 的 x/y 为左上角）；
- 文本图形需用中文字体渲染，否则中文显示为方块。
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request
from io import BytesIO

log = logging.getLogger(__name__)

# 入库与嵌入 xlsx 的图像最大宽度（原图 2448 宽，按此等比缩放，控制 BLOB 体积）
IMAGE_MAX_WIDTH = 640
JPEG_QUALITY = 85

_CJK_FONT_PATHS = (
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\simsun.ttc",   # 宋体
)
_LATIN_FONT_PATHS = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
)
_font_cache: dict[tuple[int, bool], object] = {}


def is_a0(name: str) -> bool:
    """判断是否为 A0（支持 A0 / $A$0 写法）。"""
    norm = (name or "").strip().upper().replace("$", "").strip()
    return norm == "A0"


def _layer_urls(hmi_result: dict, layer_type: str) -> list[str]:
    acq = (hmi_result or {}).get("acqImageView") or {}
    return [layer["url"] for layer in acq.get("layers") or []
            if isinstance(layer, dict) and layer.get("$type") == layer_type
            and isinstance(layer.get("url"), str) and layer["url"]]


def image_source_size(hmi_result: dict) -> tuple[int, int] | None:
    """主采集视图的原始图像尺寸（用于计算 ?sz= 缩放与图形绘制比例）。"""
    acq = (hmi_result or {}).get("acqImageView") or {}
    for layer in acq.get("layers") or []:
        if not isinstance(layer, dict) or layer.get("$type") != "ImageLayer":
            continue
        image = layer.get("image") if isinstance(layer.get("image"), dict) else layer
        width, height = image.get("width"), image.get("height")
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            return int(width), int(height)
    return None


def _cell_image_url(hmi_result: dict, cell_name: str) -> str | None:
    for item in (hmi_result or {}).get("cells") or []:
        if item.get("location") == cell_name or item.get("name") == cell_name:
            data = item.get("data")
            if isinstance(data, str) and data.startswith(("/", "http", "data:")):
                return data
            if isinstance(data, dict):
                for key in ("url", "value"):
                    val = data.get(key)
                    if isinstance(val, str) and val:
                        return val
    return None


def resolve_sources(hmi_result: dict, cell_name: str) -> dict[str, str]:
    """返回 {kind: url}：image=原始图；graphics=结果渲染图叠加数据（仅 A0）。"""
    raw = _cell_image_url(hmi_result, cell_name)
    if not raw:
        urls = _layer_urls(hmi_result, "ImageLayer")
        raw = urls[0] if urls else None
    out: dict[str, str] = {}
    if raw:
        out["image"] = raw
    if is_a0(cell_name):
        urls = _layer_urls(hmi_result, "GraphicsLayer")
        if urls:
            out["graphics"] = urls[0]
    return out


def scaled_url(path: str, src_width: int | None, src_height: int | None,
               max_width: int = IMAGE_MAX_WIDTH) -> str:
    """原图超过 max_width 时用 ?sz= 请求缩放版本（相机只返回 BMP，原图约 14MB）。"""
    if not src_width or not src_height or src_width <= max_width:
        return path
    scale = max_width / src_width
    return f"{path}{'&' if '?' in path else '?'}sz={max_width},{round(src_height * scale)}"


def _open_url(host: str, port: int, path: str, timeout: float):
    url = path if path.startswith("http") else f"http://{host}:{port}{path}"
    return urllib.request.urlopen(url, timeout=timeout)


def fetch_image(host: str, port: int, path: str, timeout: float = 10.0) -> tuple[bytes, str]:
    """下载图像，返回 (bytes, mime)；非图片/空响应抛异常。"""
    if path.startswith("data:"):
        head, _, b64 = path.partition(",")
        mime = head[5:].split(";")[0] or "image/png"
        return base64.b64decode(b64), mime
    with _open_url(host, port, path, timeout) as resp:
        mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read()
    if not data:
        raise ValueError(f"空响应: {path}")
    if not mime.startswith("image/"):
        raise ValueError(f"非图片响应（{mime or 'unknown'}）: {path}")
    return data, mime


def fetch_json(host: str, port: int, path: str, timeout: float = 10.0):
    """下载 JSON（用于 GraphicsLayer 图形数据）。"""
    with _open_url(host, port, path, timeout) as resp:
        return json.loads(resp.read())


def to_jpeg(data: bytes, max_width: int = IMAGE_MAX_WIDTH) -> tuple[bytes, int, int]:
    """解码（BMP/PNG/JPEG）→ 等比缩放到 max_width → JPEG，返回 (bytes, 宽, 高)。"""
    from PIL import Image

    with Image.open(BytesIO(data)) as im:
        im = im.convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, round(im.height * max_width / im.width)),
                           Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), im.width, im.height


def _rgb(value, default=(255, 0, 0)) -> tuple[int, int, int]:
    try:
        v = int(value) & 0xFFFFFF
    except (TypeError, ValueError):
        return default
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def _font(size: int, cjk: bool):
    key = (max(6, size), cjk)
    if key in _font_cache:
        return _font_cache[key]
    from PIL import ImageFont

    for path in (_CJK_FONT_PATHS if cjk else _LATIN_FONT_PATHS) + _LATIN_FONT_PATHS:
        try:
            font = ImageFont.truetype(path, key[0])
            _font_cache[key] = font
            return font
        except Exception:
            continue
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _has_cjk(text: str) -> bool:
    return any(ord(ch) > 0x2E7F for ch in text)


def _draw_graphic(draw, graphic: dict, k: float) -> bool:
    """按原图坐标比例 k 绘制单个图形；无法识别的类型返回 False。"""
    if not isinstance(graphic, dict):
        return False
    kind = graphic.get("$type")
    color = _rgb(graphic.get("color"))
    line_width = max(1, int(round(float(graphic.get("lineThickness") or 1) * k)))

    if kind == "Text" or (graphic.get("text") is not None and kind not in
                          ("Line", "Circle", "BlobChain", "CompositeRegion")):
        text = str(graphic.get("text") or "")
        if not text:
            return True  # 空文本，无需绘制
        # 相机图形字号以 pt 标注（1pt≈1.333px），缩放后限制最小 9px 保证可读
        size = max(9, int(round(float(graphic.get("fontSize") or 9) * 1.333 * k)))
        font = _font(size, _has_cjk(text))
        x = float(graphic.get("x") or 0) * k
        y = float(graphic.get("y") or 0) * k
        for line in text.splitlines() or [""]:
            draw.text((x, y), line, fill=color, font=font)
            y += size * 1.2
        return True

    if kind == "Line":
        draw.line([(float(graphic["x0"]) * k, float(graphic["y0"]) * k),
                   (float(graphic["x1"]) * k, float(graphic["y1"]) * k)],
                  fill=color, width=line_width)
        return True

    if kind == "Circle":
        cx, cy = float(graphic["x"]) * k, float(graphic["y"]) * k
        r = float(graphic["radius"]) * k
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=line_width)
        return True

    if kind == "BlobChain":
        pts = graphic.get("points") or []
        xy = [(float(pts[i]) * k, float(pts[i + 1]) * k)
              for i in range(0, len(pts) - 1, 2)]
        if len(xy) > 1:
            draw.line(xy, fill=color, width=line_width)
            return True
        return False

    if kind == "CompositeRegion":
        drawn = False
        for sub in graphic.get("subregions") or []:
            shape = (sub or {}).get("shape") or {}
            drawn |= _draw_graphic(draw, shape, k)
        return drawn

    # Region / Rectangle：x,y 为左上角，w,h 为宽高（angle 暂不旋转）
    if kind in ("Region", "Rectangle", "RectangleRegion", "Cross", "Point"):
        x, y = float(graphic.get("x") or 0) * k, float(graphic.get("y") or 0) * k
        w, h = float(graphic.get("w") or 0) * k, float(graphic.get("h") or 0) * k
        if not w or not h:
            return False
        draw.rectangle([x, y, x + w, y + h], outline=color, width=line_width)
        return True

    pts = graphic.get("points")
    if pts and len(pts) >= 4:  # 未识别类型但带点序列：按折线绘制
        xy = [(float(pts[i]) * k, float(pts[i + 1]) * k)
              for i in range(0, len(pts) - 1, 2)]
        draw.line(xy, fill=color, width=line_width)
        return True
    return False


def compose_render(data: bytes, graphics: list, src_width: int | None) -> tuple[bytes, int, int]:
    """把 GraphicsLayer 图形按比例叠加到图像上，返回 JPEG (bytes, 宽, 高)。"""
    from PIL import Image, ImageDraw

    with Image.open(BytesIO(data)) as base:
        image = base.convert("RGB")
    if not src_width:
        src_width = image.width
    k = image.width / src_width
    draw = ImageDraw.Draw(image)
    unsupported = set()
    for graphic in graphics or []:
        if not _draw_graphic(draw, graphic, k) and isinstance(graphic, dict):
            unsupported.add(graphic.get("$type"))
    if unsupported:
        log.warning("渲染图包含未支持的图形类型，已跳过: %s", sorted(unsupported))
    buf = BytesIO()
    image.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), image.width, image.height
