"""相机渲染图：GraphicsLayer/SVGLayer → SVG → WebView2 canvas 合成。

历史实现用 Pillow 逐类型手绘，容易因坐标空间、字号/锚点、旋转等细节产生错位。
这里改为与 SvgOverlayPy 相同的做法：

    1. 把检测图形转成 SVG（viewBox 使用相机 viewport 坐标空间）；
    2. 在 WebView2（pywebview）里用 <canvas> 先画底图、再画 SVG，
       canvas 像素尺寸 = 输出图尺寸（1 倍 DPI，不做 3 倍放大）；
    3. canvas.toDataURL('image/jpeg') 回传，写入数据库。

渲染在独立的子进程里进行（pywebview 需要自己的 GUI 线程），
主进程通过 stdin/stdout 逐行 JSON 通信；失败时调用方回退 Pillow 合成。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from html import escape

log = logging.getLogger(__name__)

JPEG_QUALITY = 90
RENDER_TIMEOUT = 20.0

# 相机图形字号（fontSize）→ 实际字形高度的换算系数。
# 实测相机自带 Web HMI 的文字墨迹高度约为 fontSize 的 1.72 倍，而本模块按 em
# 绘制时墨迹约为 em 的 0.72 倍，故 em 系数取 1.72 / 0.72 ≈ 2.4 才能与 HMI 一致。
# （旧值 1.333 是把它当 pt→px 换算，实测只有 HMI 的 0.56 倍。）
TEXT_SCALE = 2.4

# 浏览器内合成页面：canvas 尺寸由任务给定，不受窗口大小与 DPI 影响
_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:#000;overflow:hidden}
canvas{display:block}
</style></head><body>
<canvas id="c" width="8" height="8"></canvas>
<script>
window.__ctmRender = function (job) {
  (async function () {
    try {
      var img = new Image();
      img.src = job.image;
      await img.decode();
      var canvas = document.getElementById('c');
      canvas.width = job.width;
      canvas.height = job.height;
      var ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, job.width, job.height);
      if (job.svg) {
        var svg = new Image();
        svg.src = job.svg;
        await svg.decode();
        ctx.drawImage(svg, 0, 0, job.width, job.height);
      }
      var data = canvas.toDataURL('image/jpeg', (job.quality || 90) / 100);
      window.pywebview.api.deliver(job.id, data.split(',')[1], null);
    } catch (e) {
      window.pywebview.api.deliver(job.id, null, String(e));
    }
  })();
};
</script></body></html>
"""


# ---------------------------------------------------------------- SVG 生成


def _hex(color, default="#ff0000") -> str:
    try:
        return "#%06x" % (int(color) & 0xFFFFFF)
    except (TypeError, ValueError):
        return default


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _points(graphic: dict) -> list[tuple[float, float]]:
    pts = graphic.get("points") or []
    out = []
    for i in range(0, len(pts) - 1, 2):
        out.append((_f(pts[i]), _f(pts[i + 1])))
    return out


def _sub_shapes(graphic: dict):
    """展开复合图形：subregions 里的图形包在 shape 字段内，graphics 里则是图形本身。"""
    for sub in graphic.get("subregions") or []:
        if isinstance(sub, dict):
            shape = sub.get("shape")
            yield shape if isinstance(shape, dict) else sub
    for sub in graphic.get("graphics") or []:
        if isinstance(sub, dict):
            yield sub


def _bbox(graphic: dict) -> tuple[float, float, float, float] | None:
    """估算图形的坐标包围盒（用于推断坐标系与未知类型的兜底）。"""
    kind = graphic.get("$type")
    pts = _points(graphic)
    xs: list[float] = []
    ys: list[float] = []
    if pts:
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    for key in ("x", "x0", "x1", "cx"):
        if graphic.get(key) is not None:
            xs.append(_f(graphic.get(key)))
    for key in ("y", "y0", "y1", "cy"):
        if graphic.get(key) is not None:
            ys.append(_f(graphic.get(key)))
    for key in ("w", "width"):
        if graphic.get(key) is not None and graphic.get("x") is not None:
            xs.append(_f(graphic.get("x")) + _f(graphic.get(key)))
    for key in ("h", "height"):
        if graphic.get(key) is not None and graphic.get("y") is not None:
            ys.append(_f(graphic.get("y")) + _f(graphic.get(key)))
    r = graphic.get("radius")
    if r is not None and graphic.get("x") is not None:
        cx, cy = _f(graphic.get("x")), _f(graphic.get("y"))
        xs += [cx - _f(r), cx + _f(r)]
        ys += [cy - _f(r), cy + _f(r)]
    for sub in _sub_shapes(graphic):
        inner = _bbox(sub)
        if inner:
            xs += [inner[0], inner[2]]
            ys += [inner[1], inner[3]]
    if not xs or not ys or kind == "Text":
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _graphic_svg(graphic: dict, color: str, width: float) -> str:
    if not isinstance(graphic, dict):
        return ""
    kind = graphic.get("$type") or ""
    stroke = f'stroke="{color}" stroke-width="{max(1.0, width):.2f}" fill="none"'

    if kind == "Text" or (graphic.get("text") is not None and kind not in
                          ("Line", "Circle", "BlobChain", "CompositeRegion")):
        text = str(graphic.get("text") or "")
        if not text:
            return ""
        x, y = _f(graphic.get("x")), _f(graphic.get("y"))
        size = max(9.0, _f(graphic.get("fontSize") or 9) * TEXT_SCALE)
        lines = text.splitlines() or [""]
        parts = [f'<text x="{x:.2f}" y="{y:.2f}" fill="{color}" '
                 f'font-family="Microsoft YaHei, Arial, sans-serif" '
                 f'font-size="{size:.2f}" dominant-baseline="text-before-edge">']
        for i, line in enumerate(lines):
            dy = 0 if i == 0 else size * 1.2
            parts.append(f'<tspan x="{x:.2f}" dy="{dy:.2f}">{escape(line)}</tspan>')
        parts.append("</text>")
        return "".join(parts)

    if kind == "Line":
        return (f'<line x1="{_f(graphic.get("x0")):.2f}" y1="{_f(graphic.get("y0")):.2f}" '
                f'x2="{_f(graphic.get("x1")):.2f}" y2="{_f(graphic.get("y1")):.2f}" {stroke}/>')

    if kind == "LineList":
        segs = graphic.get("lines") or graphic.get("segments") or []
        out = []
        for seg in segs:
            if isinstance(seg, dict):
                out.append(_graphic_svg({**seg, "$type": "Line"}, color, width))
        return "".join(out)

    if kind == "Circle":
        cx, cy, r = _f(graphic.get("x")), _f(graphic.get("y")), _f(graphic.get("radius"))
        return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" {stroke}/>'

    if kind == "Annulus":
        cx, cy = _f(graphic.get("x")), _f(graphic.get("y"))
        r1 = _f(graphic.get("radius") or graphic.get("outerRadius"))
        r2 = _f(graphic.get("innerRadius") or 0)
        out = [f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r1:.2f}" {stroke}/>']
        if r2 > 0:
            out.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r2:.2f}" {stroke}/>')
        return "".join(out)

    if kind == "Arc":
        cx, cy = _f(graphic.get("x")), _f(graphic.get("y"))
        r = _f(graphic.get("radius"))
        a0 = _f(graphic.get("startAngle"))
        a1 = _f(graphic.get("endAngle") or 360)
        rad = math.radians(a0)
        rad2 = math.radians(a1)
        x0, y0 = cx + r * math.cos(rad), cy + r * math.sin(rad)
        x1, y1 = cx + r * math.cos(rad2), cy + r * math.sin(rad2)
        large = 1 if abs(a1 - a0) > 180 else 0
        return (f'<path d="M {x0:.2f} {y0:.2f} A {r:.2f} {r:.2f} 0 {large} 1 '
                f'{x1:.2f} {y1:.2f}" {stroke}/>')

    if kind == "Cross":
        cx, cy = _f(graphic.get("x")), _f(graphic.get("y"))
        size = _f(graphic.get("size") or graphic.get("radius") or graphic.get("w") or 20)
        return (f'<line x1="{cx - size:.2f}" y1="{cy:.2f}" x2="{cx + size:.2f}" y2="{cy:.2f}" {stroke}/>'
                f'<line x1="{cx:.2f}" y1="{cy - size:.2f}" x2="{cx:.2f}" y2="{cy + size:.2f}" {stroke}/>')

    if kind == "Point":
        cx, cy = _f(graphic.get("x")), _f(graphic.get("y"))
        return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="4" {stroke}/>'

    if kind in ("BlobChain", "Polygon", "Polyline", "PolylinePath", "BeadPath"):
        pts = _points(graphic)
        if len(pts) < 2:
            return ""
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        if kind in ("Polygon", "BeadPath"):
            return f'<polygon points="{d}" {stroke}/>'
        return f'<polyline points="{d}" {stroke}/>'

    if kind in ("Region", "Rectangle", "RectangleRegion", "SubRegion", "MaskedRegion", "FilledBox"):
        x, y = _f(graphic.get("x")), _f(graphic.get("y"))
        w = _f(graphic.get("w") or graphic.get("width"))
        h = _f(graphic.get("h") or graphic.get("height"))
        if not w or not h:
            return ""
        if kind == "FilledBox":
            return (f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
                    f'fill="{color}" fill-opacity="0.18" stroke="{color}" stroke-width="{max(1.0, width):.2f}"/>')
        dash = ' stroke-dasharray="6 4"' if kind == "MaskedRegion" else ""
        return (f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
                f'{stroke}{dash}/>')

    if kind == "Fixture":
        bbox = _bbox(graphic)
        if not bbox:
            return ""
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        size = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 2 or 20
        return (f'<line x1="{cx - size:.2f}" y1="{cy:.2f}" x2="{cx + size:.2f}" y2="{cy:.2f}" {stroke}/>'
                f'<line x1="{cx:.2f}" y1="{cy - size:.2f}" x2="{cx:.2f}" y2="{cy + size:.2f}" {stroke}/>')

    if kind in ("CompositeRegion", "MultiGraphics"):
        out = []
        for sub in _sub_shapes(graphic):
            out.append(_graphic_svg(
                sub,
                _hex(sub.get("color"), color),
                _f(sub.get("lineThickness") or width)))
        return "".join(out)

    # 未知类型：有包围盒就画虚线框，至少不丢位置
    bbox = _bbox(graphic)
    if bbox:
        x0, y0, x1, y1 = bbox
        return (f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" '
                f'{stroke} stroke-dasharray="4 4" stroke-opacity="0.7"/>')
    return ""


def graphics_bounds(graphics: list) -> tuple[float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for graphic in graphics or []:
        if isinstance(graphic, dict):
            bbox = _bbox(graphic)
            if bbox:
                xs += [bbox[0], bbox[2]]
                ys += [bbox[1], bbox[3]]
    if not xs or not ys:
        return None
    return max(xs), max(ys)


def pick_view_size(graphics: list, view_size, image_size) -> tuple[float, float] | None:
    """推断图形坐标所在的坐标空间：优先相机 viewport，其次图像像素尺寸。

    相机 viewport 与 ImageLayer 像素尺寸可能不同（如 816×684 vs 2448×2048）；
    图形坐标若落在 viewport 内则按 viewport 缩放，否则按图像尺寸。
    """
    bounds = graphics_bounds(graphics)
    view = tuple(float(v) for v in view_size) if view_size else None
    image = tuple(float(v) for v in image_size) if image_size else None
    if not bounds:
        return view or image
    bw, bh = bounds
    for space in (view, image):
        if space and bw <= space[0] * 1.02 and bh <= space[1] * 1.02:
            return space
    return view or image


def graphics_to_svg(graphics: list, view_w: float, view_h: float,
                    image_w: int, image_h: int) -> str:
    """把 GraphicsLayer JSON 转成 SVG；viewBox 用图形坐标空间，输出尺寸用图像像素。"""
    parts = []
    for graphic in graphics or []:
        if not isinstance(graphic, dict):
            continue
        color = _hex(graphic.get("color"))
        width = _f(graphic.get("lineThickness") or 1)
        body = _graphic_svg(graphic, color, width)
        if not body:
            continue
        angle = graphic.get("angle") or graphic.get("rotation")
        if angle:
            cx = _f(graphic.get("x")) + _f(graphic.get("w")) / 2
            cy = _f(graphic.get("y")) + _f(graphic.get("h")) / 2
            body = f'<g transform="rotate({_f(angle):.2f} {cx:.2f} {cy:.2f})">{body}</g>'
        parts.append(body)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{image_w}" height="{image_h}" '
            f'viewBox="0 0 {view_w:.2f} {view_h:.2f}">'
            f'{"".join(parts)}</svg>')


def svg_data_url(svg_text: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg_text.encode("utf-8")).decode("ascii")


def image_data_url(jpeg_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")


# ---------------------------------------------------------------- WebView 渲染进程


def _emit_raw(obj: dict) -> None:
    """向主进程写一行协议 JSON。

    打包成 windowed exe 后 sys.stdout 可能为 None（无控制台），此时退回 fd 1
    （主进程用 PIPE 接管 stdout，句柄仍然可用）。
    """
    out = sys.stdout
    if out is None:
        try:
            out = os.fdopen(1, "w", encoding="utf-8", buffering=1)
        except OSError:
            return
    try:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()
    except (OSError, ValueError):
        pass


def host_main() -> None:  # pragma: no cover - 子进程入口
    """子进程：启动隐藏 pywebview 窗口，按行处理渲染任务。"""
    try:
        import webview
    except Exception as exc:  # noqa: BLE001 - 缺库/加载失败都交主进程回退 Pillow
        # 不要抛异常：windowed exe 会弹「Unhandled exception in script」对话框
        _emit_raw({"ready": False, "error": f"pywebview 不可用: {exc}"})
        return

    results: dict[int, tuple] = {}
    cond = threading.Condition()

    class Api:
        def deliver(self, job_id, data, error=None):
            with cond:
                results[int(job_id)] = (data, error)
                cond.notify_all()
            return True

    api = Api()
    window = webview.create_window(
        "ctm-render", html=_HTML, js_api=api, hidden=True,
        width=320, height=240, background_color="#000000",
    )
    loaded = threading.Event()
    window.events.loaded += lambda: loaded.set()

    def emit(obj: dict) -> None:
        _emit_raw(obj)

    def loop() -> None:
        if not loaded.wait(30):
            emit({"ready": False, "error": "webview 初始化超时"})
            return
        emit({"ready": True})
        stream = sys.stdin
        if stream is None:  # windowed exe 下可能为 None，退回 fd 0（主进程用 PIPE 接管）
            try:
                stream = os.fdopen(0, "r", encoding="utf-8", buffering=1)
            except OSError:
                return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                job = json.loads(line)
            except json.JSONDecodeError:
                continue
            job_id = int(job.get("id") or 0)
            try:
                window.evaluate_js(f"window.__ctmRender({json.dumps(job)});")
            except Exception as exc:  # noqa: BLE001
                emit({"id": job_id, "ok": False, "error": f"evaluate_js: {exc}"})
                continue
            with cond:
                ok = cond.wait_for(lambda: job_id in results, timeout=RENDER_TIMEOUT)
                data, error = results.pop(job_id, (None, "timeout"))
            if ok and data:
                emit({"id": job_id, "ok": True, "data": data})
            else:
                emit({"id": job_id, "ok": False, "error": error or "timeout"})

    threading.Thread(target=loop, daemon=True).start()
    try:
        webview.start(gui="edgechromium", debug=False, private_mode=True)
    except Exception as exc:  # noqa: BLE001 - 例如系统缺 WebView2 运行时
        emit({"ready": False, "error": f"webview.start 失败: {exc}"})


class WebRenderer:
    """主进程侧渲染器：懒启动子进程，串行发送任务。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0
        self._ready = False
        self._disabled_until = 0.0

    def _spawn(self) -> bool:
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        creation = 0
        if sys.platform == "win32":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 打包成单文件 exe 后不能再 -m 模块，改为用同一 exe 带标志重启
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--renderer-host"]
        else:
            cmd = [sys.executable, "-m", "cameratinymes.renderer", "--host"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1, env=env, creationflags=creation,
            )
        except OSError as exc:
            log.warning("启动渲染进程失败: %s", exc)
            return False

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._lines.put(line.strip())
            self._lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        self._proc = proc
        # 等就绪消息；忽略 WebView/运行库可能打到 stdout 的非协议输出
        deadline = time.time() + 40
        while True:
            timeout = deadline - time.time()
            if timeout <= 0:
                log.warning("渲染进程未就绪: 超时")
                self._kill()
                return False
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                continue
            if line is None:
                log.warning("渲染进程未就绪: 已退出")
                self._kill()
                return False
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                log.debug("渲染进程输出: %s", line[:200])
                continue
            if not msg.get("ready"):
                log.warning("渲染进程初始化失败: %s", msg.get("error"))
                self._kill()
                return False
            break
        self._ready = True
        log.info("WebView 渲染进程就绪")
        return True

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._ready = False
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    def available(self) -> bool:
        return time.time() >= self._disabled_until

    def render(self, jpeg_bytes: bytes, svg_text: str,
               width: int, height: int, quality: int = JPEG_QUALITY) -> bytes | None:
        """返回 JPEG 字节；失败返回 None（调用方回退 Pillow）。"""
        if not self.available():
            return None
        with self._lock:
            if self._proc is None and not self._spawn():
                self._disabled_until = time.time() + 300
                return None
            self._seq += 1
            job_id = self._seq
            job = {
                "id": job_id,
                "image": image_data_url(jpeg_bytes),
                "svg": svg_data_url(svg_text),
                "width": int(width),
                "height": int(height),
                "quality": int(quality),
            }
            try:
                assert self._proc and self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                log.warning("渲染任务发送失败: %s", exc)
                self._kill()
                self._disabled_until = time.time() + 300
                return None
            deadline = time.time() + RENDER_TIMEOUT + 5
            msg = None
            while msg is None:
                timeout = deadline - time.time()
                if timeout <= 0:
                    log.warning("渲染任务超时")
                    self._kill()
                    self._disabled_until = time.time() + 300
                    return None
                try:
                    line = self._lines.get(timeout=timeout)
                except queue.Empty:
                    continue
                if line is None:  # 子进程已退出
                    self._kill()
                    self._disabled_until = time.time() + 300
                    return None
                try:
                    candidate = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    log.debug("渲染进程输出: %s", line[:200])
                    continue
                if candidate.get("id") == job_id:
                    msg = candidate
            if not msg.get("ok"):
                log.warning("渲染失败: %s", msg.get("error"))
                return None
            try:
                return base64.b64decode(msg["data"])
            except (KeyError, ValueError):
                return None

    def close(self) -> None:
        with self._lock:
            self._kill()


_renderer: WebRenderer | None = None


def get_renderer() -> WebRenderer:
    global _renderer
    if _renderer is None:
        _renderer = WebRenderer()
    return _renderer


def shutdown() -> None:
    global _renderer
    if _renderer is not None:
        _renderer.close()
        _renderer = None


if __name__ == "__main__":  # pragma: no cover
    if "--host" in sys.argv:
        host_main()
