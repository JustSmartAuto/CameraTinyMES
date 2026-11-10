"""系统托盘：ctypes + Shell_NotifyIconW 的独立消息窗口线程实现。

参考 InternetProxyPy 的成熟方案，为 CameraTinyMES 适配：
- 独立窗口线程（CreateWindowExW + GetMessageW），回调经 tk.after 派回 Tk 主线程
- 左键：唤出主窗口；右键：显示菜单（显示 / 退出）
- 关闭按钮（WM_DELETE_WINDOW）→ 隐藏到托盘而非退出
- 托盘图标创建/删除必须走 shell32.Shell_NotifyIconW（不是 user32）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger("cameratinymes.tray")

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

# --- 64 位下必须显式声明 argtypes / restype，否则 HWND 等句柄会被截断 ---
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = ctypes.c_longlong
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.LoadImageW.argtypes = [
    wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT,
]
user32.LoadImageW.restype = wt.HICON
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadIconW.restype = wt.HICON
user32.DestroyIcon.argtypes = [wt.HICON]
user32.DestroyIcon.restype = wt.BOOL
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = wt.BOOL
user32.TrackPopupMenu.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
# Shell_NotifyIconW 属于 shell32！不是 user32！
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wt.BOOL

kernel32 = ctypes.windll.kernel32
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

# --- 常量 ---
WM_DESTROY = 0x0002
WM_APP_TRAY = 0x8000 + 2026  # 自定义消息
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_DELETE = 0x00000000, 0x00000002
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x0001, 0x0002, 0x0004
TRAY_ID = 2026
IDI_APPLICATION = 32512

MENU_SHOW = 5001
MENU_QUIT = 5002
MENU_REPORT = 5003


# --- 结构体 ---
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeout", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wt.HICON),
    ]


def _asset_bases() -> list[Path]:
    """资源查找目录：PyInstaller 解包 → 当前目录 → 源码根。"""
    bases = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bases.append(Path(bundle))
    bases += [Path.cwd(), Path(__file__).resolve().parents[2]]
    return bases


def _load_ico() -> wt.HICON:
    """加载 assets/app_icon.ico；找不到回退系统默认。"""
    for base in _asset_bases():
        path = base / "assets" / "app_icon.ico"
        if path.is_file():
            hicon = user32.LoadImageW(
                None, str(path), 1, 64, 64, 0x00000010  # IMAGE_ICON | LR_LOADFROMFILE
            )
            if hicon:
                return hicon
    return user32.LoadIconW(None, IDI_APPLICATION)


class TrayIcon:
    """托盘宿主：独立消息窗口线程，回调经 tk.after 派回 Tk 主线程。"""

    def __init__(self, tk, on_show, on_quit, on_report=None) -> None:
        self.tk = tk
        self.on_show = on_show   # 显示主窗口
        self.on_quit = on_quit   # 真正退出（封存 + 保存 + 销毁）
        self.on_report = on_report  # 打开改善报告工作台（可为空）
        self._hwnd: wt.HWND | None = None
        self._hicon: wt.HICON | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @property
    def available(self) -> bool:
        """托盘功能是否可用（非 Windows 则不可用）。"""
        return sys.platform == "win32"

    def start(self) -> None:
        if not self.available:
            return
        self._hicon = _load_ico()
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        """请求托盘线程退出。"""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    # --- 后台线程 ---

    def _loop(self) -> None:
        cls_name = "CameraTinyMESTrayWnd"
        wnd_cls = WNDCLASSW()
        wnd_cls.lpszClassName = cls_name
        wnd_cls.lpfnWndProc = WNDPROC(self._wndproc)
        wnd_cls.hInstance = kernel32.GetModuleHandleW(None)
        atom = user32.RegisterClassW(ctypes.byref(wnd_cls))
        if not atom:
            log.warning("托盘窗口类注册失败 err=%s", ctypes.GetLastError())
            return
        self._hwnd = user32.CreateWindowExW(
            0, cls_name, "CameraTinyMES Tray", 0, 0, 0, 0, 0, None, None,
            wnd_cls.hInstance, None,
        )
        if not self._hwnd:
            log.warning("托盘消息窗口创建失败 err=%s", ctypes.GetLastError())
            return

        nid = self._get_nid()
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not ok:
            log.warning("Shell_NotifyIconW(NIM_ADD) 失败 err=%s", ctypes.GetLastError())
            return

        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 清理
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        if self._hicon:
            try:
                user32.DestroyIcon(self._hicon)
            except Exception:
                pass

    def _get_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self._hwnd
        nid.uID = TRAY_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = "CameraTinyMES — 迷你 MES（制造执行系统）"
        return nid

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            if lparam == WM_LBUTTONUP:
                self.tk.after(0, self.on_show)
            elif lparam == WM_RBUTTONUP:
                self._popup_menu(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, MENU_SHOW, "显示主窗口")
        if self.on_report is not None:
            user32.AppendMenuW(menu, 0, MENU_REPORT, "改善报告工作台")
        user32.AppendMenuW(menu, 0, MENU_QUIT, "退出")
        user32.SetForegroundWindow(hwnd)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        chosen = user32.TrackPopupMenu(
            menu,
            0x0180,  # TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON
            pt.x, pt.y, 0, hwnd, None,
        )
        user32.DestroyMenu(menu)
        if chosen == MENU_SHOW:
            self.tk.after(0, self.on_show)
        elif chosen == MENU_REPORT and self.on_report is not None:
            self.tk.after(0, self.on_report)
        elif chosen == MENU_QUIT:
            self.tk.after(0, self.on_quit)
