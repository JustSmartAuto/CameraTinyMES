"""渲染交叉验证：对同一台相机的同一批帧，分别用两套渲染逻辑出图。

- CameraHelperJimJack：CvsInSight 取图 + GraphicsHelper.DrawGraphics（GDI+，视口 1/2）
- CameraTinyMES：images.compose_render（相机 SVGLayer 优先，GraphicsLayer 兜底，640 宽）

用法：
    uv run python tests/render_compare/run_compare.py --host 192.168.10.55
    uv run python tests/render_compare/run_compare.py --host 192.168.10.145 --out tests/render_compare/out/cam145
输出：
    <out>/frame_N/  原图、graphics.json、layer.svg、meta.json、jimjack_N.png
    <out>/frame_N/ctm_svg.jpg、ctm_graphics.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cameratinymes import images  # noqa: E402

JIMJACK_RELEASE = Path(r"D:\JustStupid\CameraHelper相机显示_2606120922\CameraHelperJimJack\Release")
HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
DLLS = [
    "Cognex.InSight.Web.dll",
    "Cognex.InSight.Web.Controls.dll",
    "Newtonsoft.Json.dll",
    "websocket-sharp-standard.dll",
    "log4net.dll",
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.WinForms.dll",
    "Microsoft.Web.WebView2.Wpf.dll",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def build_jimjack() -> Path:
    if not CSC.exists():
        raise RuntimeError(f"未找到 csc.exe: {CSC}")
    if not JIMJACK_RELEASE.exists():
        raise RuntimeError(f"未找到 CameraHelperJimJack/Release: {JIMJACK_RELEASE}")

    BIN.mkdir(parents=True, exist_ok=True)
    for name in DLLS:
        source = JIMJACK_RELEASE / name
        if source.exists():
            shutil.copy2(source, BIN / name)

    exe = BIN / "JimJackRender.exe"
    refs = [
        "System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
        str(JIMJACK_RELEASE / "Cognex.InSight.Web.dll"),
        str(JIMJACK_RELEASE / "Cognex.InSight.Web.Controls.dll"),
        str(JIMJACK_RELEASE / "Newtonsoft.Json.dll"),
    ]
    cmd = [str(CSC), "/nologo", "/target:exe", f"/out:{exe}"]
    cmd += [f"/r:{ref}" for ref in refs]
    cmd.append(str(HERE / "JimJackRender.cs"))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("编译 JimJackRender 失败:\n" + (proc.stdout or "") + (proc.stderr or ""))
    return exe


def render_ctm(frame_dir: Path) -> None:
    meta = json.loads((frame_dir / "meta.json").read_text(encoding="utf-8-sig"))
    image_path = frame_dir / "image.bmp"
    if not image_path.exists():
        return
    data = image_path.read_bytes()
    view = (meta["viewport_width"], meta["viewport_height"])
    image_size = (meta.get("image_width", view[0]), meta.get("image_height", view[1]))

    svg_path = frame_dir / "layer.svg"
    if svg_path.exists():
        jpeg, w, h = images.compose_render(
            data, svg_text=svg_path.read_text(encoding="utf-8-sig"),
            view_size=view, image_size=image_size)
        (frame_dir / "ctm_svg.jpg").write_bytes(jpeg)
        log(f"  本软件 SVG     : {w}x{h}, {len(jpeg)} bytes")

    graphics_path = frame_dir / "graphics.json"
    if graphics_path.exists():
        graphics = json.loads(graphics_path.read_text(encoding="utf-8-sig"))
        jpeg, w, h = images.compose_render(
            data, graphics=graphics, view_size=view, image_size=image_size)
        (frame_dir / "ctm_graphics.jpg").write_bytes(jpeg)
        log(f"  本软件 Graphics: {w}x{h}, {len(jpeg)} bytes")


def main() -> int:
    parser = argparse.ArgumentParser(description="CameraTinyMES 与 CameraHelperJimJack 渲染对比")
    parser.add_argument("--host", default="192.168.10.55")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--cell", default="A0")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else HERE / "out" / stamp
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"输出目录: {out_dir}")

    exe = build_jimjack()
    cmd = [str(exe), "--host", args.host, "--port", str(args.port),
           "--count", str(args.count), "--cell", args.cell, "--out", str(out_dir)]
    log("运行 CameraHelperJimJack 取图/渲染 ...")
    proc = subprocess.run(cmd, cwd=str(BIN), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.stdout:
        log(proc.stdout.strip())
    if proc.returncode != 0:
        log(proc.stderr.strip() if proc.stderr else "未知错误")
        raise RuntimeError(f"JimJack 渲染失败，exit={proc.returncode}")

    frames = sorted(p for p in out_dir.glob("frame_*") if p.is_dir())
    for frame_dir in frames:
        log(f"\n[{frame_dir.name}] 本软件渲染")
        render_ctm(frame_dir)

    log("\n完成，生成文件：")
    for path in sorted(out_dir.rglob("*")):
        if path.is_file():
            log(f"  {path.relative_to(out_dir)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
