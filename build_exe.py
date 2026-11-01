"""PyInstaller 打包：单文件 exe，命名 exe_{yyyyMMddHHmmss}。

用法：uv run python build_exe.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"CameraTinyMES_{stamp}"
    icon = ASSETS / "app_icon.ico"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--name", name,
        "--add-data", f"{ASSETS}{';'}assets",
        "--collect-all", "matplotlib",
        str(ROOT / "src" / "cellmonitor" / "app.py"),
    ]
    if icon.exists():
        cmd += ["--icon", str(icon)]
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    exe = ROOT / "dist" / f"{name}.exe"
    print(f"打包完成: {exe}")


if __name__ == "__main__":
    main()
