"""HMI 截图与本地渲染的文本像素高度对比。

用相机画面上的红色圆（GraphicsLayer 里 Circle 的 radius）作为比例尺，
把三方渲染都折算到「原始视口像素」再比较文字大小。

用法：
    uv run python tests/render_compare/measure_text_size.py \
        --hmi C:\\...\\cognex-imageandgraphics-reloaded.jpg \
        --local tests/render_compare/out/cam146/frame_1/ctm_svg.jpg \
        --jimjack tests/render_compare/out/cam146/frame_1/jimjack_1.png \
        --view 1920x1080 --radius 289.067 \
        --out tests/render_compare/out/compare_text/cam146.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def load(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB")).astype(np.int16)


def red_mask(a: np.ndarray) -> np.ndarray:
    return (a[..., 0] > 150) & (a[..., 1] < 80) & (a[..., 2] < 80)


def green_mask(a: np.ndarray) -> np.ndarray:
    # 相机画面里的亮部会带少量绿色噪点，必须收紧阈值只保留纯绿文字
    return (a[..., 1] > 150) & (a[..., 0] < 90) & (a[..., 2] < 90)


def largest_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    """返回面积最大的连通域包围盒 (x0, x1, y0, y1)；图形比文字大，天然选中作为比例尺。"""
    seen = mask.copy()
    h, w = mask.shape
    best = (-1.0, 0, 0, 0, 0)
    for y, x in zip(*np.nonzero(mask)):
        if not seen[y, x]:
            continue
        stack = [(int(y), int(x))]
        seen[y, x] = False
        ys: list[int] = []
        xs: list[int] = []
        while stack:
            cy, cx = stack.pop()
            ys.append(cy)
            xs.append(cx)
            for ny in (cy - 1, cy, cy + 1):
                if ny < 0 or ny >= h:
                    continue
                for nx in (cx - 1, cx, cx + 1):
                    if 0 <= nx < w and seen[ny, nx]:
                        seen[ny, nx] = False
                        stack.append((ny, nx))
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        area = (x1 - x0) * (y1 - y0)
        if area > best[0]:
            best = (float(area), x0, x1, y0, y1)
    if best[0] < 0:
        raise ValueError("未找到参考图形，无法标定比例")
    return int(best[1]), int(best[2]), int(best[3]), int(best[4])


def text_band_height(mask: np.ndarray, top: float = 0.3, left: float = 0.0,
                     right: float = 0.75) -> tuple[int, int]:
    """取指定区域内第一条绿色文字行带（时间戳行），返回 (高度像素, 起始y)。"""
    h, w = mask.shape
    region = mask[: int(h * top), int(w * left): int(w * right)]
    rows = region.sum(axis=1) >= 8
    y = 0
    while y < len(rows):
        if rows[y]:
            y0 = y
            while y < len(rows) and rows[y]:
                y += 1
            if y - y0 >= 4:
                return y - y0, y0
        else:
            y += 1
    return 0, 0


def measure(path: Path, view_w: int, ref: str = "", ref_native: float = 0.0,
            ref_axis: str = "max") -> dict:
    a = load(path)
    h, w = a.shape[:2]
    info: dict = {"file": str(path), "size": [w, h]}

    green_h, green_y = text_band_height(green_mask(a))
    info["green_text_px"] = green_h
    info["green_text_y"] = green_y

    if ref_native > 0:
        x0, x1, y0, y1 = largest_bbox(red_mask(a) if ref == "red" else green_mask(a))
        extent = {"w": x1 - x0, "h": y1 - y0}.get(ref_axis, max(x1 - x0, y1 - y0))
        scale = (extent + 1) / ref_native
        info["ref_px"] = extent + 1
        info["ref_box"] = [x0, y0, x1, y1]
    else:
        scale = w / view_w
    info["scale"] = round(scale, 4)
    info["frame_px"] = [round(view_w * scale), round(view_w * scale * h / w)]

    if info["green_text_px"]:
        info["green_text_native"] = round(info["green_text_px"] / scale, 1)
        info["green_text_at_640"] = round(info["green_text_px"] * 640 / (view_w * scale), 1)
    return info


def label(img: Image.Image, text: str) -> Image.Image:
    bar = Image.new("RGB", (img.width, 26), (255, 255, 255))
    d = ImageDraw.Draw(bar)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 16)
    except Exception:
        font = ImageFont.load_default()
    d.text((6, 4), text, fill=(0, 0, 0), font=font)
    out = Image.new("RGB", (img.width, img.height + bar.height), (255, 255, 255))
    out.paste(bar, (0, 0))
    out.paste(img, (0, bar.height))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hmi", required=True)
    ap.add_argument("--local", required=True)
    ap.add_argument("--jimjack", default="")
    ap.add_argument("--view", default="1920x1080")
    ap.add_argument("--ref", default="", choices=["", "red", "green"],
                    help="HMI 截图里用来标定比例尺的图形颜色")
    ap.add_argument("--ref-native", type=float, default=0.0,
                    help="该参考图形在相机坐标系里的尺寸（圆直径/矩形宽），单位=视口像素")
    ap.add_argument("--ref-axis", default="max", choices=["max", "w", "h"])
    ap.add_argument("--crop-height", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    view_w, view_h = (int(x) for x in args.view.lower().split("x"))
    hmi = measure(Path(args.hmi), view_w, args.ref, args.ref_native, args.ref_axis)
    local = measure(Path(args.local), view_w)
    jim = measure(Path(args.jimjack), view_w) if args.jimjack else None

    result = {"view": [view_w, view_h], "hmi": hmi, "local": local}
    if jim:
        result["jimjack"] = jim
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 统一到 HMI 实际显示的画幅宽度，裁剪同样高度，纵向拼接
    target_w = round(view_w * hmi["scale"])
    target_h = args.crop_height or round(target_w * 0.24)

    rows = []
    rows.append(("HMI 浏览器截图 显示比例{:.2f}x, 文字{}px (折算640宽≈{}px)".format(
        hmi["scale"], hmi["green_text_px"], hmi.get("green_text_at_640", 0)),
        Image.open(args.hmi).convert("RGB")))
    loc = Image.open(args.local).convert("RGB")
    loc = loc.resize((target_w, round(loc.height * target_w / loc.width)), Image.LANCZOS)
    rows.append(("本软件渲染 {}px 宽, 文字{}px (折算640宽≈{}px)".format(
        local["size"][0], local["green_text_px"], local.get("green_text_at_640", 0)), loc))
    if args.jimjack:
        jj = Image.open(args.jimjack).convert("RGB")
        jj = jj.resize((target_w, round(jj.height * target_w / jj.width)), Image.LANCZOS)
        rows.append(("CameraHelperJimJack {}px 宽, 文字{}px (折算640宽≈{}px)".format(
            jim["size"][0], jim["green_text_px"], jim.get("green_text_at_640", 0)), jj))

    tiles = []
    for text, img in rows:
        img = img.crop((0, 0, target_w, min(target_h, img.height)))
        tiles.append(label(img, text))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (target_w, sum(t.height for t in tiles)), (255, 255, 255))
    y = 0
    for t in tiles:
        canvas.paste(t, (0, y))
        y += t.height
    canvas.save(out)
    print(f"\n对比图: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
