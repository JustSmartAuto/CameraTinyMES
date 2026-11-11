"""PyInstaller 打包：单文件 exe，命名 exe_{yyyyMMddHHmmss}。

用法：uv run python build_exe.py
"""

from __future__ import annotations

import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
WEBUI = ROOT / "src" / "cameratinymes" / "webui"
COOKIE_MAGIC = b"MEI\014\013\012\013\016"  # 单文件 exe 末尾 PKG cookie 的魔数
COOKIE_FORMAT = "!8sIIII64s"  # magic、archive_length、toc_offset、toc_length、pyvers、pylib_name
COOKIE_LENGTH = struct.calcsize(COOKIE_FORMAT)


def verify_onefile(exe: Path) -> None:
    """校验产物完整性：磁盘写满或构建中断会留下缺少 cookie 的截断 exe。"""
    size = exe.stat().st_size
    with exe.open("rb") as f:
        f.seek(max(0, size - 1024))
        tail = f.read()
    index = tail.rfind(COOKIE_MAGIC)
    if index < 0 or index + COOKIE_LENGTH > len(tail):
        raise SystemExit(
            f"打包产物校验失败：{exe}（{size} 字节）缺少 PyInstaller PKG cookie，"
            "文件可能被截断，请检查磁盘剩余空间后重试")
    _, archive_length, _, _, _, _ = struct.unpack(
        COOKIE_FORMAT, tail[index:index + COOKIE_LENGTH])
    # cookie 完整落在文件末尾，且声明的载荷不超过文件大小
    if size - len(tail) + index + COOKIE_LENGTH != size or archive_length > size:
        raise SystemExit(
            f"打包产物校验失败：{exe}（{size} 字节）内嵌载荷不完整"
            f"（archive_length={archive_length}），请检查磁盘剩余空间后重试")
    print(f"校验通过: {exe}（{size / 1048576:.1f} MB，载荷 {archive_length / 1048576:.1f} MB）")


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"CameraTinyMES_{stamp}"
    icon = ASSETS / "app_icon.ico"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--name", name,
        "--add-data", f"{ASSETS}{';'}assets",
        "--add-data", f"{WEBUI}{';'}cameratinymes/webui",
        "--collect-all", "matplotlib",
        # 渲染子进程内才 import webview，静态分析看不到，必须显式收集；
        # 同时带上 pythonnet/clr 与 WebView2 原生 DLL（webview/lib）
        "--collect-all", "webview",
        "--collect-all", "pythonnet",
        "--collect-all", "clr_loader",
        "--hidden-import", "clr",
        str(ROOT / "src" / "cameratinymes" / "app.py"),
    ]
    if icon.exists():
        cmd += ["--icon", str(icon)]
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    exe = ROOT / "dist" / f"{name}.exe"
    verify_onefile(exe)
    print(f"打包完成: {exe}")


if __name__ == "__main__":
    main()
