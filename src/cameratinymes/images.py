"""相机图像处理：原始图下载、缩放转码、A0 结果渲染图合成。

图像地址来源（按优先级）：
1. 单元格自身结果（HmiResult.cells 中对应条目）的 data：字符串 URL 或 data:base64；
2. 主采集视图 acqImageView.layers 中的 ImageLayer.url。

A0 的渲染图：优先取 acqImageView 的 SVGLayer（相机原生 SVG），否则把
GraphicsLayer JSON 转成 SVG，再在 WebView2 的 Canvas 里先画底图再画 SVG
（见 renderer.py），得到「图像 + 图形」的渲染图；WebView 不可用时回退 Pillow。

实现要点（实测于 In-Sight 固件 22.x）：
- 图像接口只返回 BMP，可用 ?sz=宽,高 请求缩放版本（原图 2448×2048 约 14MB）；
- 图形坐标属于 acqImageView.viewport 坐标系，与 ImageLayer 的像素尺寸不一定相同，
  按图像像素宽度换算比例会整体错位；
- 文本图形需用中文字体渲染，否则中文显示为方块（SVG 由 WebView 挑选字体）。
"""

from __future__ import annotations

import base64
import json
import logging
import re
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


def cell_is_image(hmi_result: dict, cell_name: str) -> bool:
    """该单元格自身是否为图像单元格。

    依据：单元格自己的值是图像占位标记（如「❒Image」）或自身携带图像 URL。
    注意不能看 acqImageView——那是整次拍照的采集视图，任何单元格的结果里都有，
    用它判断会把所有 auto 单元格误判为图像单元格。
    """
    for item in (hmi_result or {}).get("cells") or []:
        if item.get("location") != cell_name and item.get("name") != cell_name:
            continue
        data = item.get("data")
        candidates = []
        if isinstance(data, str):
            candidates.append(data)
        elif isinstance(data, dict):
            candidates.extend(v for v in (data.get("url"), data.get("value"))
                              if isinstance(v, str))
        for v in candidates:
            if v.startswith(("/", "http", "data:")) or "❒" in v:
                return True
        return False
    return False


def _layer_urls(hmi_result: dict, layer_type: str) -> list[str]:
    acq = (hmi_result or {}).get("acqImageView") or {}
    return [layer["url"] for layer in acq.get("layers") or []
            if isinstance(layer, dict) and layer.get("$type") == layer_type
            and isinstance(layer.get("url"), str) and layer["url"]]


def view_size(hmi_result: dict) -> tuple[int, int] | None:
    """主采集视图的 viewport 尺寸（图形坐标所在的坐标系，不等于图像像素尺寸）。"""
    acq = (hmi_result or {}).get("acqImageView") or {}
    vp = acq.get("viewport")
    if not isinstance(vp, dict):
        return None
    width, height = vp.get("width"), vp.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        return int(width), int(height)
    return None


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
    """返回 {kind: url}：image=原始图；svg/graphics=结果图形（仅 A0）。

    svg 为相机生成的 SVG 图层（与 HMI 显示一致，优先使用）；
    graphics 为图形 JSON 数组（SVG 缺失或下载失败时兜底）。
    """
    raw = _cell_image_url(hmi_result, cell_name)
    if not raw:
        urls = _layer_urls(hmi_result, "ImageLayer")
        raw = urls[0] if urls else None
    out: dict[str, str] = {}
    if raw:
        out["image"] = raw
    if is_a0(cell_name):
        urls = _layer_urls(hmi_result, "SVGLayer")
        if urls:
            out["svg"] = urls[0]
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


def fetch_text(host: str, port: int, path: str, timeout: float = 10.0) -> str:
    """下载文本（用于 SVGLayer 的 SVG 内容）。"""
    with _open_url(host, port, path, timeout) as resp:
        return resp.read().decode("utf-8", "replace")


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


_SVG_TAG_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_SVG_ATTR_RE = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _svg_attrs(tag: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2) if m.group(2) is not None else m.group(3))
            for m in _SVG_ATTR_RE.finditer(tag)]


def _rewrite_svg(svg_text: str, out_w: int, out_h: int,
                 space: tuple[int, int] | None) -> str | None:
    """把相机 SVG 的输出尺寸改为 out_w×out_h，并保证存在 viewBox。

    相机 SVG 的坐标空间由其 viewBox（其次 width/height 属性）声明；
    两者都没有时按 viewport/image 尺寸兜底。同时移除内嵌的底图 <image>，
    避免与 canvas 先画好的图像重复叠加。
    """
    match = _SVG_TAG_RE.search(svg_text or "")
    if not match:
        return None
    tag = match.group(0)
    pairs = _svg_attrs(tag)
    attrs = {k.lower(): v for k, v in pairs}
    view_box = attrs.get("viewbox")
    if not view_box:
        try:
            cw = float(attrs["width"].removesuffix("px"))
            ch = float(attrs["height"].removesuffix("px"))
        except (KeyError, ValueError):
            cw, ch = space or (out_w, out_h)
        view_box = f"0 0 {cw:g} {ch:g}"
    others = "".join(f' {k}="{v}"' for k, v in pairs
                     if k.lower() not in ("width", "height", "viewbox"))
    if "xmlns" not in attrs:
        others = ' xmlns="http://www.w3.org/2000/svg"' + others
    new_tag = f'<svg{others} width="{out_w}" height="{out_h}" viewBox="{view_box}">'
    body = svg_text[match.end():]
    body = re.sub(r"<image\b[^>]*?/>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<image\b[^>]*>.*?</image>", "", body,
                  flags=re.IGNORECASE | re.DOTALL)
    return new_tag + body


def compose_render(data: bytes, graphics: list | None = None,
                   view_size: tuple[int, int] | None = None,
                   image_size: tuple[int, int] | None = None,
                   svg_text: str | None = None) -> tuple[bytes, int, int]:
    """把检测图形叠加到图像上，返回 JPEG (bytes, 宽, 高)。

    优先用 WebView2（Canvas）合成：图形转 SVG 后与底图一起按输出像素绘制，
    与相机 HMI 的渲染路径一致，可避免字号、锚点、旋转等逐类型手绘的错位；
    WebView 不可用时回退 Pillow（坐标按 viewport 缩放）。
    """
    from PIL import Image, ImageDraw
    from . import renderer

    jpeg, width, height = to_jpeg(data)
    space = None
    svg = None
    if svg_text:
        svg = _rewrite_svg(svg_text, width, height, view_size or image_size)
    if not svg and graphics:
        space = renderer.pick_view_size(graphics, view_size, image_size)
        if space:
            svg = renderer.graphics_to_svg(graphics, space[0], space[1], width, height)
    if svg:
        rendered = renderer.get_renderer().render(jpeg, svg, width, height, JPEG_QUALITY)
        if rendered:
            return rendered, width, height
        log.info("WebView 渲染不可用，回退 Pillow 合成：%s×%s", width, height)
    if not graphics:
        return jpeg, width, height

    with Image.open(BytesIO(jpeg)) as base:
        image = base.convert("RGB")
    if not space:
        space = (float(image.width), float(image.height))
    k = image.width / space[0] if space[0] else 1.0
    draw = ImageDraw.Draw(image)
    unsupported = set()
    for graphic in graphics:
        if not _draw_graphic(draw, graphic, k) and isinstance(graphic, dict):
            unsupported.add(graphic.get("$type"))
    if unsupported:
        log.warning("渲染图包含未支持的图形类型，已跳过: %s", sorted(unsupported))
    buf = BytesIO()
    image.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), image.width, image.height
