"""开机自启：在 shell:startup 目录创建 .lnk 快捷方式。

为什么选 shell:startup 而非注册表 Run / 计划任务：
- 本软件 GUI 主进程已经常驻（托盘），开机只需一条静默启动入口
- shell:startup 是当前用户启动文件夹，无需管理员权限、不会弹 UAC
- PyInstaller --onefile exe 默认无 --uac-admin 清单，注册表 Run 每次登录都弹 UAC

用法：
    from cameratinymes import autostart
    autostart.set(True, log=print)    # 开启
    autostart.set(False, log=print)   # 关闭
    autostart.is_enabled()            # 查询
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

log = logging.getLogger("cameratinymes.autostart")

LNK_NAME = "CameraTinyMES.lnk"


def _startup_dir() -> Path:
    """当前用户的 shell:startup 目录（Windows 自带 Shell 文件夹）。"""
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _lnk_path() -> Path:
    return _startup_dir() / LNK_NAME


def _target_args() -> tuple[str, str]:
    """开机要执行的 (target_path, arguments)。

    PyInstaller 打包后 frozen=True，target 就是 exe 本身；
    开发时 target 是 python.exe，arguments 是 main.py 路径。
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    return sys.executable, str(Path(sys.argv[0]).resolve())


def _create_lnk(target: str, arguments: str = "", desc: str = "") -> bool:
    """用 WScript.Shell COM 创建 .lnk。失败自动退回 PowerShell + CreateShortcut。"""
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # type: ignore[import-not-found]

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(_lnk_path()))
        shortcut.TargetPath = target
        if arguments:
            shortcut.Arguments = arguments
        shortcut.WorkingDirectory = str(Path(target).parent)
        shortcut.Description = desc or "CameraTinyMES — 迷你 MES"
        shortcut.IconLocation = target
        shortcut.Save()
        return True
    except Exception:
        pass

    # 退回：PowerShell + New-Object -ComObject WScript.Shell
    try:
        import subprocess
        ps = (
            f'$s = (New-Object -ComObject WScript.Shell).CreateShortcut("{_lnk_path()}"); '
            f'$s.TargetPath = "{target}"; '
        )
        if arguments:
            ps += f'$s.Arguments = "{arguments}"; '
        ps += (
            f'$s.WorkingDirectory = "{Path(target).parent}"; '
            f'$s.Description = "{desc or "CameraTinyMES"}"; '
            f'$s.IconLocation = "{target}"; '
            f'$s.Save()'
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            check=True,
        )
        return True
    except Exception as exc:
        log.error("创建快捷方式失败: %s", exc)
        return False


def is_enabled() -> bool:
    return _lnk_path().is_file()


def set(enabled: bool, log_fn: Callable[[str], None] | None = None) -> bool:
    """把系统侧自启状态同步为 enabled。"""
    log_fn = log_fn or log.info
    path = _lnk_path()

    if enabled:
        target, args = _target_args()
        _startup_dir().mkdir(parents=True, exist_ok=True)
        if _create_lnk(target, args):
            log_fn(f"✓ 已创建开机自启快捷方式 → {path}")
            return True
        log_fn(f"✗ 创建开机自启失败（需要 WScript.Shell 或 pythoncom 可用）")
        return False
    else:
        if path.exists():
            try:
                path.unlink()
                log_fn(f"✓ 已删除开机自启快捷方式 → {path}")
            except OSError as exc:
                log_fn(f"✗ 删除失败: {exc}")
                return False
        else:
            log_fn("（快捷方式不存在，无需删除）")
        return True
