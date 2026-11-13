"""tkinter 多相机宫格界面（BeOS 主题，见 theme.py）。

布局：顶部工具栏（连接/断开、添加相机、导出），中部相机宫格面板，
底部日志。asyncio 事件循环运行于独立线程，通过队列与 UI 线程通信。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import unicodedata
import webbrowser
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import charts, stats, theme
from .config import (
    AppSettings,
    CameraConfig, CellConfig,
    CELL_DATA_TYPES, CELL_RECORD_MODES, CELL_ROLES, NUMERIC_DATA_TYPES,
    load_config,
    result_specs,
    save_config, save_settings,
)
from .storage import export_xlsx, timestamp_suffix, open_storage
from .tray import TrayIcon
from .worker import Monitor

from cameratinymes import __version__

log = logging.getLogger(__name__)

C = theme.COLORS

STATUS_TEXT = {
    "offline": "离线",
    "connecting": "连接中",
    "connected": "已连接",
    "disconnected": "已断开",
    "error": "错误",
}


def _display_width(text: str) -> int:
    """按显示宽度计算字符数（全角/CJK 记 2，其余记 1）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _elide(text: str, limit: int) -> str:
    """把过长文本截断为 limit 个显示宽度，末尾加省略号，避免撑破面板布局。"""
    if _display_width(text) <= limit:
        return text
    width, out = 0, []
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + w > limit - 1:
            break
        width += w
        out.append(ch)
    return "".join(out) + "…"


class Tooltip:
    """鼠标悬停时显示完整文本（配合被截断的标签）。"""

    def __init__(self, widget: tk.Widget):
        self.widget = widget
        self.text = ""
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if not self.text or self._tip:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, bg="#ffffe1", fg=C["ink"],
                 font=theme.FONT, justify="left", anchor="w", wraplength=460,
                 relief="solid", bd=1, padx=4, pady=2).pack()

    def _hide(self, _event=None) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


def _apply_icon(win: tk.Tk | tk.Toplevel) -> None:
    """给窗口设置 assets/app_icon.ico（源码运行与 PyInstaller 打包均适用）。"""
    icon = charts.ASSETS / "app_icon.ico"
    if not icon.exists():
        return
    try:
        win.iconbitmap(str(icon))
    except tk.TclError:
        pass


class _IconToplevel(tk.Toplevel):
    """对话框基类：统一图标，避免 Windows 上显示 Tk 默认羽毛图标。"""

    def __init__(self, master: tk.Misc | None = None, **kw):
        super().__init__(master, **kw)
        _apply_icon(self)


class CameraPanel(tk.Frame):
    """单相机面板（BeOS 窗口）：黄色标题页签、白色内容区、药丸按钮。"""

    def __init__(self, master, app: "MonitorApp", cfg: CameraConfig):
        super().__init__(master, bg=C["chrome"], highlightthickness=1,
                         highlightbackground=C["chrome_darker"], bd=0)
        self.app = app
        self.cfg = cfg
        self.status = "offline"
        self.value_labels: dict[str, tk.Label] = {}
        self._value_tips: dict[str, Tooltip] = {}

        tab_text = f"{cfg.name}  ({cfg.host}:{cfg.port})"
        bar, self.status_label = theme.title_tab(self, tab_text, STATUS_TEXT["offline"])

        content = theme.content_frame(self)
        content.pack(fill="both", expand=True, padx=2, pady=2)

        if not cfg.cells:
            tk.Label(content, text="（未配置单元格）", bg=C["chrome_lighter"],
                     fg=C["chrome_dark"], font=theme.FONT).pack(anchor="w", padx=6, pady=4)
        for cell in cfg.cells:
            row = tk.Frame(content, bg=C["chrome_lighter"])
            row.pack(fill="x", padx=6, pady=1)
            type_tag = f"[{cell.data_type}]" if cell.data_type != "auto" else ""
            mode_tag = "记录" if cell.is_simple_record else ""
            prefix = " ".join(t for t in (type_tag, mode_tag) if t)
            label_text = f"{cell.name} {prefix}".strip()
            if cell.note:
                label_text += f"（{cell.note}）"
            name_label = tk.Label(row, text=_elide(label_text, 16), width=16, anchor="w",
                                  bg=C["chrome_lighter"], fg=C["ink"], font=theme.FONT)
            name_label.pack(side="left")
            name_tip = Tooltip(name_label)
            name_tip.text = label_text
            value = tk.Label(row, text="—", width=14, anchor="w", bg=C["chrome_lighter"],
                             fg=C["teal"], font=("Arial", 11, "bold"))
            value.pack(side="left")
            self.value_labels[cell.name] = value
            self._value_tips[cell.name] = Tooltip(value)
            if cell.is_image:
                tk.Label(row, text="图像", bg=C["chrome_lighter"],
                         fg=C["chrome_dark"], font=theme.FONT).pack(side="right")
            elif cell.is_pointcloud:
                tk.Label(row, text="点云", bg=C["chrome_lighter"],
                         fg=C["chrome_dark"], font=theme.FONT).pack(side="right")
            elif cell.is_simple_record:
                tk.Label(row, text="仅记录", bg=C["chrome_lighter"],
                         fg=C["chrome_dark"], font=theme.FONT).pack(side="right")
            elif cell.is_numeric:
                limits = ""
                if cell.lower is not None:
                    limits += f"≥{cell.lower:g}"
                if cell.upper is not None:
                    limits += (" " if limits else "") + f"≤{cell.upper:g}"
                if limits:
                    tk.Label(row, text=limits, bg=C["chrome_lighter"],
                             fg=C["chrome_dark"], font=theme.FONT).pack(side="right")
            elif cell.data_type in ("string", "bool", "other"):
                tk.Label(row, text="无阈值", bg=C["chrome_lighter"],
                         fg=C["chrome_dark"], font=theme.FONT).pack(side="right")

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(fill="x", padx=2, pady=(0, 2))
        theme.beos_button(btns, "连接", lambda: self.app.connect_camera(cfg)).pack(side="left", padx=3)
        theme.beos_button(btns, "断开", lambda: self.app.disconnect_camera(cfg.name)).pack(side="left", padx=3)
        theme.beos_button(btns, "趋势图", lambda: self.app.show_chart(cfg)).pack(side="left", padx=3)
        theme.beos_button(btns, "导出xlsx", lambda: self.app.export_camera(cfg)).pack(side="left", padx=3)
        theme.beos_button(btns, "编辑", lambda: self.app.edit_camera_cells(cfg)).pack(side="left", padx=3)

    def set_status(self, status: str, detail: str = "") -> None:
        self.status = status
        colors = {"offline": C["chrome_darker"], "connecting": C["yellow_dark"],
                  "connected": C["teal"], "disconnected": C["chrome_darker"],
                  "error": C["red_dot"]}
        self.status_label.config(text=STATUS_TEXT.get(status, status),
                                 fg=colors.get(status, C["ink"]))

    def set_value(self, cell: str, value) -> None:
        label = self.value_labels.get(cell)
        if label:
            text = "—" if value is None else str(value)
            label.config(text=_elide(text, 14))
            tip = self._value_tips.get(cell)
            if tip:
                tip.text = text


class ReportEntryDialog(_IconToplevel):
    """「生成改善分析报告」入口：选日期 + 勾小时 + 展示访问地址。"""

    def __init__(self, parent, storage, cameras, url_provider,
                 default_day: str | None = None,
                 default_hours: list[int] | None = None):
        super().__init__(parent)
        self.title("生成改善分析报告")
        self.result: tuple[str, list[int]] | None = None
        self._storage = storage
        self._cameras = cameras
        self._urls = url_provider
        self._specs = result_specs([c for c in cameras if c.enabled])
        self.configure(bg=C["chrome"])
        self.maxsize(780, 560)
        theme.title_tab(self, "生成改善分析报告")

        cells = [cell.name for _cam, cell, _passes, _fails in self._specs]
        dates = [str(d) for d in storage.days_with_results_specs(cells, limit=365)]
        today = date.today().isoformat()
        if today not in dates:
            dates.insert(0, today)
        self._dates = dates

        top = tk.Frame(self, bg=C["chrome"])
        top.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(top, text="统计日期", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT_BOLD).pack(side="left")
        self._day_var = tk.StringVar(value=default_day or today)
        combo = ttk.Combobox(top, textvariable=self._day_var, values=dates,
                             state="readonly", width=14)
        combo.pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_day(None))
        tk.Label(top, text="（只列有产线记录的日期，倒序）", bg=C["chrome"],
                 fg=C["chrome_dark"], font=theme.FONT).pack(side="left")

        hours_frame = tk.LabelFrame(self, text="统计时段（勾选小时）", bg=C["chrome"],
                                    fg=C["ink"], font=theme.FONT, labelanchor="nw")
        hours_frame.pack(fill="x", padx=10, pady=4)
        self._hour_vars: list[tuple[tk.BooleanVar, tk.Checkbutton]] = []
        for hour in range(24):
            var = tk.BooleanVar(value=False)
            check = tk.Checkbutton(hours_frame, text=str(hour), variable=var,
                                   bg=C["chrome"], fg=C["ink"], font=theme.FONT,
                                   activebackground=C["chrome"], selectcolor="#ffffff",
                                   command=self._refresh)
            check.grid(row=hour // 12, column=hour % 12, sticky="w", padx=2, pady=1)
            self._hour_vars.append((var, check))

        quick = tk.Frame(self, bg=C["chrome"])
        quick.pack(fill="x", padx=10, pady=2)
        for label, command in (("全选", self._select_all),
                               ("0点-当前小时", self._select_to_now),
                               ("清空", self._clear),
                               ("仅NG时段", self._select_ng)):
            theme.beos_button(quick, label, command).pack(side="left", padx=3)

        self._hint = tk.Label(self, text="", bg=C["chrome"], fg=C["teal"],
                              font=theme.FONT, anchor="w", justify="left")
        self._hint.pack(fill="x", padx=10, pady=2)

        addr = tk.LabelFrame(self, text="访问地址", bg=C["chrome"], fg=C["ink"],
                             font=theme.FONT, labelanchor="nw")
        addr.pack(fill="x", padx=10, pady=4)
        self._local_var = tk.StringVar()
        self._lan_var = tk.StringVar()
        tk.Label(addr, textvariable=self._local_var, bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT, anchor="w").pack(fill="x", padx=6, pady=1)
        tk.Label(addr, textvariable=self._lan_var, bg=C["chrome"], fg=C["chrome_darker"],
                 font=theme.FONT, anchor="w").pack(fill="x", padx=6, pady=1)
        tk.Label(addr, text="其他工控机可直接打开局域网地址；首次监听 0.0.0.0 时"
                            "请放行专用网络该端口。", bg=C["chrome"],
                 fg=C["chrome_dark"], font=theme.FONT, anchor="w").pack(
            fill="x", padx=6, pady=(0, 4))

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(fill="x", padx=10, pady=8)
        theme.beos_button(btns, "复制局域网地址", self._copy_lan).pack(side="left", padx=3)
        theme.beos_button(btns, "取消", self.destroy).pack(side="right", padx=3)
        self._open_btn = theme.beos_button(btns, "打开工作台", self._ok, default=True)
        self._open_btn.pack(side="right", padx=3)

        self._apply_day(default_hours)
        self.grab_set()
        parent.wait_window(self)

    # --- 小时状态 ---

    def _allowed_hours(self) -> set[int]:
        day_text = self._day_var.get()
        if day_text == date.today().isoformat():
            return set(range(datetime.now().hour + 1))
        return set(range(24))

    def _apply_day(self, hours: list[int] | None) -> None:
        allowed = self._allowed_hours()
        if hours is None:
            hours = sorted(allowed)
        selected = {int(h) for h in hours if int(h) in allowed}
        for hour, (var, check) in enumerate(self._hour_vars):
            var.set(hour in selected)
            check.config(state="normal" if hour in allowed else "disabled")
        self._refresh()

    def _current_hours(self) -> list[int]:
        return [hour for hour, (var, _check) in enumerate(self._hour_vars) if var.get()]

    def _select_all(self) -> None:
        for hour, (var, check) in enumerate(self._hour_vars):
            if str(check["state"]) != "disabled":
                var.set(True)
        self._refresh()

    def _select_to_now(self) -> None:
        limit = datetime.now().hour
        for hour, (var, check) in enumerate(self._hour_vars):
            if str(check["state"]) != "disabled":
                var.set(hour <= limit)
        self._refresh()

    def _clear(self) -> None:
        for var, _check in self._hour_vars:
            var.set(False)
        self._refresh()

    def _select_ng(self) -> None:
        try:
            specs = [(cell.name, passes, fails)
                     for _cam, cell, passes, fails in self._specs]
            ng_hours = set(int(h) for h in
                           self._storage.ng_hours_specs(self._day_var.get(), specs))
        except Exception:
            ng_hours = set()
        if not ng_hours:
            self._select_all()
            return
        for hour, (var, check) in enumerate(self._hour_vars):
            if str(check["state"]) != "disabled":
                var.set(hour in ng_hours)
        self._refresh()

    def _station_count(self) -> int:
        from .report import station_of

        grouped = {station_of(cam)[0] for cam in self._cameras if cam.enabled}
        return len(grouped)

    def _refresh(self) -> None:
        hours = self._current_hours()
        day = date.fromisoformat(self._day_var.get())
        urls = self._urls(day, hours) if hours else self._urls(day, [])
        self._local_var.set("本机    " + urls["local"])
        self._lan_var.set("局域网  " + (urls["lan"] or "（未检测到局域网地址）"))
        self._lan_url = urls["lan"] or urls["local"]
        if hours:
            self._hint.config(
                text=f"已勾选 {len(hours)} 小时，涉及 {self._station_count()} 个工站；"
                     f"生成时按这些时段统计与取图。", fg=C["teal"])
        else:
            self._hint.config(text="尚未勾选统计小时，无法生成报告。", fg=C["red_dot"])
        self._open_btn.config(state="normal" if hours else "disabled")

    def _copy_lan(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._lan_url)
        self._hint.config(text="已复制局域网地址到剪贴板。", fg=C["teal"])

    def _ok(self) -> None:
        hours = self._current_hours()
        if not hours:
            messagebox.showwarning("生成改善分析报告", "请至少勾选 1 个统计小时。",
                                   parent=self)
            return
        self.result = (self._day_var.get(), hours)
        self.destroy()


class MonitorApp:
    def __init__(self, data_dir, cameras: list[CameraConfig],
                 settings: AppSettings | None = None,
                 extra_startup: list | None = None):
        self.data_dir = data_dir
        self.cameras = cameras
        self.settings = settings or AppSettings()
        self.extra_startup = extra_startup or []
        self.storage = open_storage(data_dir)
        self.monitor = Monitor(self.storage)
        self.panels: dict[str, CameraPanel] = {}
        self._db_queues: list[queue.Queue[tuple[bool, float | None]]] = []
        self._db_status_q: queue.Queue[tuple[bool, float | None]] = queue.Queue(maxsize=10)
        self._db_queues.append(self._db_status_q)
        self._db_stop = threading.Event()
        self._db_ok: bool | None = None
        self._compress_tick = 0  # 压缩状态刷新计时器（每 50 × 100ms = 5s）

        self.root = tk.Tk()
        self._apply_window_icon()
        self.root.title("CameraTinyMES — Cognex In-Sight 迷你制造执行系统")
        self.root.geometry("1100x700")
        self.root.minsize(1060, 600)
        theme.apply(self.root)
        self._build_ui()
        endpoint = getattr(self.storage, "endpoint", getattr(self.storage, "path", ""))
        self._log(f"存储后端: {self.storage.backend}  {endpoint}")
        self._start_db_monitor()
        self._start_asyncio_thread()

        # 托盘图标 + 启动时最小化
        self._tray = TrayIcon(self.root, on_show=self._show_window,
                              on_quit=self._really_quit,
                              on_report=self.open_report_workbench)
        self._tray.start()
        if self.settings.minimize_to_tray_on_start and self._tray.available:
            self.root.after(200, self._hide_to_tray)

        # 后台 AV1 压缩守护线程（扫描 images 表，批量打包成 30 帧 GOP 的 AV1 分片）
        self.monitor.start_compressor()

        # 改善报告工作台（Web 服务随主程序启动，端口被占用时自动顺延）
        self._report_server = None
        self.root.after(300, self._start_report_server)

    # ---------- UI 构建 ----------

    def _apply_window_icon(self) -> None:
        """设置主窗口图标，并尝试把 ICO 设为后续 Toplevel 的默认图标。"""
        _apply_icon(self.root)
        icon = charts.ASSETS / "app_icon.ico"
        if not icon.exists():
            return
        try:
            self.root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        toolbar = theme.window_frame(self.root)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        theme.title_tab(toolbar, "CameraTinyMES", "控制面板")
        bar = tk.Frame(toolbar, bg=C["chrome"])
        bar.pack(fill="x", padx=2, pady=2)
        theme.beos_button(bar, "连接全部", self.connect_all, default=True).pack(side="left", padx=3, pady=3)
        theme.beos_button(bar, "断开全部", self.disconnect_all).pack(side="left", padx=3)
        theme.beos_button(bar, "添加相机", self.add_camera).pack(side="left", padx=3)
        theme.beos_button(bar, "配置单元格", self.edit_camera).pack(side="left", padx=3)
        theme.beos_button(bar, "打开数据目录", self.open_data_dir).pack(side="left", padx=3)
        theme.beos_button(bar, "改善报告", self.open_report_workbench).pack(side="left", padx=3)
        self.db_status = tk.Label(bar, text="数据库 连接中…", bg=C["chrome"],
                                  fg=C["chrome_dark"], font=theme.FONT_BOLD)
        self.db_status.pack(side="right", padx=8)
        self._compress_status = tk.Label(bar, text="AV1压缩 待命", bg=C["chrome"],
                                         fg=C["chrome_darker"], font=theme.FONT)
        self._compress_status.pack(side="right", padx=8)
        tk.Label(bar, text=f"数据目录: {self.data_dir}", bg=C["chrome"],
                 fg=C["chrome_darker"], font=theme.FONT).pack(side="right", padx=6)

        # 网格放在 Canvas 中，纵向可滚动（相机数量多时不会裁切）
        self.grid_outer = tk.Frame(self.root, bg=C["bg"])
        self.grid_outer.pack(fill="both", expand=True, padx=6, pady=4)
        self.grid_canvas = tk.Canvas(self.grid_outer, bg=C["bg"], highlightthickness=0, bd=0)
        self.grid_vbar = ttk.Scrollbar(self.grid_outer, orient="vertical",
                                       command=self.grid_canvas.yview)
        self.grid_canvas.configure(yscrollcommand=self.grid_vbar.set)
        self.grid_canvas.pack(side="left", fill="both", expand=True)
        self.grid_vbar.pack(side="right", fill="y")
        self.grid_frame = tk.Frame(self.grid_canvas, bg=C["bg"])
        self._grid_window = self.grid_canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", self._on_grid_configure)
        self.grid_canvas.bind("<Configure>", self._on_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self._rebuild_grid()

        logbox = theme.window_frame(self.root)
        logbox.pack(fill="x", padx=8, pady=(4, 8))
        theme.title_tab(logbox, "日志")
        self.log_text = tk.Text(logbox, height=6, state="disabled", bg=C["chrome_lighter"],
                                fg=C["ink"], font=("Consolas", 9), relief="flat",
                                highlightthickness=1, highlightbackground=C["chrome_darker"])
        self.log_text.pack(fill="x", padx=2, pady=(0, 2))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_menubar()
        self.root.after(100, self._poll_events)

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self.root, bg=C["chrome"], fg=C["ink"], font=theme.FONT)
        data_menu = tk.Menu(menubar, tearoff=0)
        data_menu.add_command(label="删除相机数据", command=self.delete_camera_data)
        data_menu.add_command(label="导出", command=self.export_data)
        data_menu.add_separator()
        data_menu.add_command(label="生成改善报告…", command=self.open_report_workbench)
        menubar.add_cascade(label="数据", menu=data_menu)
        web_menu = tk.Menu(menubar, tearoff=0)
        web_menu.add_command(label="任务进度页…", command=self.open_web_backend)
        web_menu.add_command(label="OK/NG 率页…", command=self.open_okng_page)
        web_menu.add_command(label="复制访问地址", command=self.copy_web_backend_url)
        menubar.add_cascade(label="网页后台", menu=web_menu)
        sys_menu = tk.Menu(menubar, tearoff=0)
        sys_menu.add_command(label="设置…", command=self.show_settings)
        sys_menu.add_separator()
        sys_menu.add_command(label="显示 / 隐藏主窗口", command=self._toggle_window)
        sys_menu.add_command(label="退出", command=self._really_quit)
        menubar.add_cascade(label="系统", menu=sys_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="使用说明", command=self.show_help)
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menubar)

    def show_help(self) -> None:
        text = (
            "CameraTinyMES 使用说明\n\n"
            "1. 添加相机：点击「添加相机」，输入名称、IP、端口\n"
            "   （固件 22.2+ 默认 80，6.x+ 默认 8087）。\n"
            "2. 配置单元格：点击工具栏「配置单元格」下拉选择相机，\n"
            "   或点击相机面板上的「编辑」按钮直接打开编辑器：\n"
            "   - 添加/编辑/删除单元格，可上移下移调整顺序\n"
            "   - 数据类型：auto（未知·运行时推断）/ int / float / string / bool / image / other\n"
            "   - 记录模式：simple（仅记录·不判定，默认）/ advanced（记录+阈值判定）\n"
            "   - 上下限仅在 advanced + 数值类型 时生效；simple 模式恒为 NULL\n"
            "   - 也可点「文本编辑」用 CSV 格式批量编辑（含回退兼容旧格式）\n"
            "   - 点「取消」放弃修改，不影响现有配置\n"
            "3. 连接：点击相机面板上的「连接」或工具栏「连接全部」。\n"
            "4. 趋势图：点击「趋势图」，文本框输入或勾选单元格（可多选批量绘图），\n"
            "   查看历史数据折线图（红色段 = 超差，虚线 = 阈值基线）。\n"
            "5. 导出：点击相机面板「导出xlsx」按相机生成报表。\n"
            "6. 数据菜单：\n"
            "   - 删除相机数据：选择相机并勾选单元格（不勾选则删全部），删除历史数据\n"
            "   - 导出：按相机 + 单元格 + 时间范围选择性导出 xlsx\n\n"
            "数据目录：" + str(self.data_dir) + "\n"
            "  ├─ db/    SQLite 数据库（所有采样点）\n"
            "  ├─ xlsx/  导出的报表\n"
            "  └─ charts/ 生成的趋势图 PNG"
        )
        messagebox.showinfo("使用说明", text)

    def show_about(self) -> None:
        text = (
            "CameraTinyMES\n"
            f"版本 {__version__}\n\n"
            "Cognex In-Sight 迷你制造执行系统\n"
            "（CogSocket 订阅 + SQLite/xlsx 落库 + 趋势图表）\n\n"
            "开源协议：MIT\n"
            "仓库：https://github.com/JustSmartAuto/CameraTinyMES"
        )
        messagebox.showinfo("关于", text)

    def _rebuild_grid(self) -> None:
        # 重建前快照各相机的连接状态与最新值，避免编辑配置后其他相机误显示为离线
        saved: dict[str, tuple[str, dict[str, str]]] = {}
        for name, panel in self.panels.items():
            values = {cell: tip.text for cell, tip in panel._value_tips.items() if tip.text}
            saved[name] = (panel.status, values)
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.panels.clear()
        n = max(len(self.cameras), 1)
        cols = min(3, n)
        for i, cfg in enumerate(self.cameras):
            panel = CameraPanel(self.grid_frame, self, cfg)
            panel.grid(row=i // cols, column=i % cols, sticky="nsew", padx=5, pady=5)
            self.panels[cfg.name] = panel
            if cfg.name in saved:
                status, values = saved[cfg.name]
                if status != "offline":
                    panel.set_status(status)
                for cell, text in values.items():
                    if cell in panel.value_labels:
                        panel.set_value(cell, text)
        for c in range(cols):
            self.grid_frame.columnconfigure(c, weight=1, uniform="camcol")
        self.grid_canvas.yview_moveto(0)
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    # ---------- 网格滚动 ----------

    def _on_grid_configure(self, _event=None) -> None:
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        # 让内层网格宽度始终等于画布可视宽度，三列随窗口自适应
        self.grid_canvas.itemconfigure(self._grid_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        # 仅当鼠标位于网格区域（含滚动条）内才滚动，避免抢走日志区的滚轮
        target = self.root.winfo_containing(event.x_root, event.y_root)
        while target is not None and target not in (self.grid_canvas, self.grid_outer):
            target = getattr(target, "master", None)
        if target is None:
            return
        self.grid_canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---------- asyncio 线程 ----------

    def _start_asyncio_thread(self) -> None:
        self.loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        for coro in self.extra_startup:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---------- 相机操作 ----------

    def connect_camera(self, cfg: CameraConfig) -> None:
        self.panels[cfg.name].set_status("connecting")
        self._submit(self.monitor.start_camera(cfg))

    def disconnect_camera(self, name: str) -> None:
        self._submit(self.monitor.stop_camera(name))

    def connect_all(self) -> None:
        for cfg in self.cameras:
            if cfg.enabled:
                self.connect_camera(cfg)

    def disconnect_all(self) -> None:
        self._submit(self.monitor.stop_all())

    # ---------- Web 后端公开桥接方法 ----------

    def find_camera(self, name: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.name == name), None)

    def find_camera_idx(self, name: str) -> int:
        for i, c in enumerate(self.cameras):
            if c.name == name:
                return i
        return -1

    def connect_camera_by_name(self, name: str) -> bool:
        cfg = self.find_camera(name)
        if cfg is None:
            return False
        self.connect_camera(cfg)
        return True

    def disconnect_camera_by_name(self, name: str) -> bool:
        if self.find_camera(name) is None:
            return False
        self.disconnect_camera(name)
        return True

    def reload_config(self) -> None:
        """读取 cameras.json 最新内容，Worker diff stop/start，重建面板。
        必须在 tk 主线程执行：Web 后端通过 root.after(0, ...) 调度。"""
        try:
            new_list = load_config(self.data_dir)
        except Exception as exc:
            self._log(f"reload_config 读 cameras.json 失败: {exc}")
            return

        # 停掉"不再存在"或"被禁用"或"单元格集合变化"的 Worker
        new_by_name = {c.name: c for c in new_list}
        old_by_name = {c.name: c for c in self.cameras}
        stop_names: set[str] = set()
        for name, old_cfg in old_by_name.items():
            new_cfg = new_by_name.get(name)
            if new_cfg is None:
                stop_names.add(name)
            elif (old_cfg.enabled and not new_cfg.enabled):
                stop_names.add(name)
            elif ([(c.name, c.data_type, c.sample_mode) for c in old_cfg.cells]
                  != [(c.name, c.data_type, c.sample_mode) for c in new_cfg.cells]):
                # 单元格集合变化 → session 已按旧列表打开，必须重开
                stop_names.add(name)

        if stop_names:
            async def _stop_them():
                for name in stop_names:
                    await self.monitor.stop_camera(name)
            self._submit(_stop_them())

        # 替换内存状态 + 立即重建面板（Worker 异步重连，面板显示"连接中"）
        self.cameras = new_list
        self._rebuild_grid()

        # 自动启动新启用的相机
        for cfg in self.cameras:
            if cfg.enabled and cfg.name not in old_by_name:
                self.connect_camera(cfg)
        # 被禁用/变更的相机：等 Worker 停完后由用户或系统自动重连（若仍 enabled）
        for cfg in self.cameras:
            if cfg.enabled and cfg.name in stop_names:
                self.connect_camera(cfg)

        self._report_server.cameras = list(self.cameras) if self._report_server else None
        self._log(f"配置已热重载：{len(self.cameras)} 台相机，停 {len(stop_names)} 台")

    # ---------- 事件轮询 ----------

    def _poll_events(self) -> None:
        try:
            while True:
                ok, size = self._db_status_q.get_nowait()
                self._apply_db_status(ok, size)
        except Exception:
            pass
        try:
            while True:
                camera, kind, payload = self.monitor.events.get_nowait()
                panel = self.panels.get(camera)
                if kind in ("connected", "disconnected", "error"):
                    if panel:
                        panel.set_status("connected" if kind == "connected" else kind,
                                         str(payload or ""))
                    self._log(f"[{camera}] {kind} {payload or ''}")
                elif kind == "result" and panel:
                    for cell, value in (payload or {}).items():
                        panel.set_value(cell, value)
                elif kind == "image" and panel:
                    for cell, text in (payload or {}).items():
                        panel.set_value(cell, text)
                elif kind == "job":
                    self._log(f"[{camera}] 当前作业 {payload or '-'}")
        except Exception:
            pass
        # 每 5 秒刷新一次压缩状态
        self._compress_tick += 1
        if self._compress_tick >= 50:
            self._compress_tick = 0
            self._apply_compress_status()
        self.root.after(100, self._poll_events)

    def _apply_compress_status(self) -> None:
        """把 BackgroundImageCompressor 的 stats 更新到工具栏 Label。"""
        compressor = self.monitor.compressor
        if not compressor.enabled:
            self._compress_status.config(text="AV1压缩 ffmpeg未就绪",
                                          fg=C["red_dot"])
            return
        stats = compressor.stats
        runs = stats["runs"]
        frames = stats["frames"]
        saved_mb = stats["saved_bytes"] / 1024 / 1024
        new_mb = stats["new_bytes"] / 1024 / 1024
        if runs == 0:
            self._compress_status.config(text="AV1压缩 待命（待攒30帧）",
                                          fg=C["chrome_darker"])
        else:
            ratio = (stats["orig_bytes"] / stats["new_bytes"]) if stats["new_bytes"] > 0 else 0
            text = (f"AV1压缩 第{runs}轮·{frames}帧·"
                    f"{stats['orig_bytes']/1024/1024:.1f}→{new_mb:.1f}MB·"
                    f"{ratio:.1f}x·省{saved_mb:.1f}MB")
            self._compress_status.config(text=text, fg=C["teal"])

    def _start_db_monitor(self) -> None:
        """后台线程每 5 秒检查一次连接与数据尺寸，结果扇出到所有 db 订阅者。"""
        def loop() -> None:
            while not self._db_stop.is_set():
                ok = self.storage.health_check()
                payload = (ok, self.storage.database_size_mb() if ok else None)
                for q in list(self._db_queues):
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        pass
                self._db_stop.wait(5)

        threading.Thread(target=loop, name="db-health", daemon=True).start()

    def subscribe_db(self, maxsize: int = 10) -> queue.Queue:
        """追加一个数据库健康订阅者 queue（Web 后端 WS 循环用）。"""
        q: queue.Queue[tuple[bool, float | None]] = queue.Queue(maxsize=maxsize)
        self._db_queues.append(q)
        return q

    def _apply_db_status(self, ok: bool, size_mb: float | None) -> None:
        name = {"sqlite": "SQLite", "mariadb": "MariaDB"}.get(self.storage.backend,
                                                            self.storage.backend)
        size_text = f"{size_mb:.2f} MB" if size_mb is not None else "-- MB"
        if ok:
            self.db_status.config(text=f"数据库 {name} 已连接 · {size_text}", fg=C["teal"])
        else:
            self.db_status.config(text=f"数据库 {name} 未连接 · {size_text}", fg=C["red_dot"])
        if self._db_ok is not None and self._db_ok != ok:
            self._log("数据库连接已恢复" if ok else "数据库连接断开")
        self._db_ok = ok

    def _log(self, msg: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ---------- 配置 ----------

    def add_camera(self) -> None:
        name = simpledialog.askstring("添加相机", "相机名称：", parent=self.root)
        if not name:
            return
        host = simpledialog.askstring("添加相机", "相机 IP：", parent=self.root)
        if not host:
            return
        port = simpledialog.askinteger("添加相机", "端口（22.2+ 默认 80，6.x+ 默认 8087）：",
                                       initialvalue=80, parent=self.root)
        if not port:
            return
        cells = self._ask_cells() or []  # 取消视为暂不配置单元格，可后续再编辑
        cfg = CameraConfig(name=name, host=host, port=port, cells=cells)
        self.cameras.append(cfg)
        save_config(self.data_dir, self.cameras)
        self._rebuild_grid()

    def edit_camera(self) -> None:
        if not self.cameras:
            messagebox.showinfo("提示", "暂无相机，请先添加。")
            return
        names = [c.name for c in self.cameras]
        dlg = CameraSelectDialog(self.root, names)
        name = dlg.result
        if not name:
            return
        cfg = next((c for c in self.cameras if c.name == name), None)
        if not cfg:
            messagebox.showerror("错误", f"未找到相机 {name}")
            return
        self.edit_camera_cells(cfg)

    def edit_camera_cells(self, cfg: CameraConfig) -> None:
        """直接打开指定相机的单元格编辑器并保存；取消则不改动现有配置。"""
        cells = self._ask_cells(cfg.cells)
        if cells is None:
            return
        cfg.cells = cells
        save_config(self.data_dir, self.cameras)
        self._rebuild_grid()

    def _ask_cells(self, current: list[CellConfig] | None = None) -> list[CellConfig] | None:
        """打开交互式单元格编辑器（表格 + 表单），可回退到文本编辑。取消返回 None。"""
        cells = [CellConfig(**c.__dict__) for c in (current or [])]
        dlg = CellEditorDialog(self.root, cells)
        return dlg.result

    # ---------- 图表 / 导出 ----------

    def show_chart(self, cfg: CameraConfig) -> None:
        if not cfg.cells:
            messagebox.showinfo("提示", "该相机未配置单元格。")
            return
        dlg = ChartCellSelectDialog(self.root, cfg.name, self.storage)
        if dlg.result is None:
            return
        text_cell, selected_cells = dlg.result

        # 文本框与多选框取并集：文本框填写的单元格 + 勾选的单元格
        targets: list[str] = []
        if text_cell:
            targets.append(text_cell)
        for c in selected_cells:
            if c not in targets:
                targets.append(c)
        for cell_name in targets:
            self._plot_cell_chart(cfg, cell_name)

    def _plot_cell_chart(self, cfg: CameraConfig, cell_name: str) -> None:
        """绘制单个单元格的趋势图并统计。"""
        cell_cfg = next((c for c in cfg.cells if c.name == cell_name), None)
        if cell_cfg and cell_cfg.is_image:
            messagebox.showinfo("提示", f"单元格「{cell_name}」为图像类型，没有数值趋势可绘制。")
            return
        if cell_cfg and cell_cfg.is_pointcloud:
            messagebox.showinfo("提示", f"单元格「{cell_name}」为点云类型，没有数值趋势可绘制。")
            return
        if cell_cfg and cell_cfg.data_type in ("string", "bool", "other"):
            messagebox.showinfo("提示",
                                f"单元格「{cell_name}」为 {cell_cfg.data_type} 类型，"
                                "无数值趋势可绘制。")
            return
        points = self.storage.query(cfg.name, cell_name)
        if not points:
            messagebox.showinfo("提示", f"单元格「{cell_name}」暂无数据。")
            return
        path = self.data_dir / "charts" / f"{cfg.name}_{cell_name}{timestamp_suffix()}.png"
        title = f"{cfg.name} / {cell_name}"
        if cell_cfg and cell_cfg.note:
            title += f"（{cell_cfg.note}）"
        fig = charts.plot_trend(points, title,
                                cell_cfg.lower if cell_cfg else None,
                                cell_cfg.upper if cell_cfg else None, path)
        self._log(f"图表已保存: {path}")
        ChartWindow(self.root, fig, f"{title} 趋势图")

        values = [r[1] for r in points if r[1] is not None]
        if values:
            s = stats.capability(values, cell_cfg.lower if cell_cfg else None,
                                 cell_cfg.upper if cell_cfg else None)
            self._log(
                f"[{cfg.name}/{cell_name}] n={s['n']} 均值={s['mean']:.4f} "
                f"σ={s['stdev']:.4f} 超差率={s.get('out_of_spec_rate', 0)*100:.2f}%"
                + (f" CPK={s['cpk']:.3f}" if "cpk" in s else "")
            )

    def export_camera(self, cfg: CameraConfig):
        notes = {c.name: c.note for c in cfg.cells}
        image_cells = {c.name for c in cfg.cells if c.is_image}
        self._start_export_async(
            camera=cfg.name, out_dir=self.data_dir / "xlsx",
            notes=notes, image_cells=image_cells,
        )

    def _start_export_async(self, *, camera: str, out_dir: Path,
                            start=None, end=None, notes=None, image_cells=None,
                            cells=None) -> None:
        """后台线程执行 export_xlsx，进度通过 queue 传给 ExportProgressDialog。"""
        import threading as _t
        from collections import namedtuple

        Props = namedtuple("Props", ["text", "fg", "bg"])
        # 复用 theme 颜色
        C_P = getattr(__import__("cameratinymes.theme", fromlist=["COLORS"]),
                      "COLORS")

        cancelled = _t.Event()
        q: queue.Queue = queue.Queue()

        dlg = ExportProgressDialog(self.root, camera, on_cancel=lambda: cancelled.set())

        def worker() -> None:
            def prog(done: int, total: int, msg: str) -> None:
                try:
                    q.put(("progress", done, total, msg))
                except Exception:
                    pass
            try:
                path = export_xlsx(
                    self.storage, camera, out_dir,
                    start=start, end=end, notes=notes,
                    image_cells=image_cells, cells=cells,
                    progress_callback=prog, cancelled=cancelled,
                )
                q.put(("done", str(path)))
            except Exception as exc:
                if cancelled.is_set():
                    q.put(("cancelled", None))
                else:
                    q.put(("error", str(exc)))

        th = _t.Thread(target=worker, name=f"export-xlsx-{camera}", daemon=True)
        th.start()
        dlg.attach_queue(q, on_done=lambda p: self._log(f"xlsx 已导出: {p}"))
        dlg.focus_set()

    def _camera_names(self) -> list[str]:
        """合并配置中的相机名与数据库中有数据的相机名。"""
        names = list(dict.fromkeys(
            [c.name for c in self.cameras] + self.storage.cameras()
        ))
        return names

    def delete_camera_data(self) -> None:
        """数据 → 删除相机数据：选择相机 + 勾选单元格删除。"""
        if not self._camera_names():
            messagebox.showinfo("提示", "暂无相机数据。")
            return
        dlg = DeleteDataDialog(self.root, self._camera_names(), self.storage)
        if dlg.result is not None:
            camera, cells = dlg.result
            total = 0
            for cell in cells:
                total += self.storage.delete_samples(camera, cell)
                total += self.storage.delete_images(camera, cell)
            if not cells:
                # 未勾选单元格 → 删除该相机全部数据
                total += self.storage.delete_samples(camera)
                total += self.storage.delete_images(camera)
            self._log(f"已删除相机「{camera}」{len(cells) or '全部'} 个单元格数据，"
                      f"共 {total} 条")

    def export_data(self) -> None:
        """数据 → 导出：选择相机 + 单元格 + 时间范围，选择性导出 xlsx。"""
        if not self._camera_names():
            messagebox.showinfo("提示", "暂无相机数据。")
            return
        dlg = ExportDialog(self.root, self._camera_names(), self.storage)
        if dlg.result is None:
            return
        camera, cells, start, end = dlg.result
        cfg = next((c for c in self.cameras if c.name == camera), None)
        notes = {c.name: c.note for c in cfg.cells} if cfg else {}
        image_cells = {c.name for c in cfg.cells if c.is_image} if cfg else set()
        self._start_export_async(
            camera=camera, out_dir=self.data_dir / "xlsx",
            start=start, end=end, notes=notes,
            image_cells=image_cells, cells=cells if cells else None,
        )

    def open_data_dir(self) -> None:
        import os
        os.startfile(self.data_dir)  # noqa: S606 - Windows only

    # ---------- 改善报告工作台 ----------

    def _start_report_server(self) -> None:
        """启动报告工作台 Web 服务（失败只记日志，不影响采集主流程）。"""
        from . import web

        try:
            port = web.start_web_server(
                self.storage, self.cameras, self.settings, self.data_dir,
                on_open_entry=self._open_entry_from_web,
                monitor=self.monitor, monitor_app=self)
            self._report_server = web.get_web_server()
            urls = self._report_server.urls()
            self._log(f"改善报告工作台已启动：{urls['local']}")
            if urls["lan"]:
                self._log(f"局域网访问：{urls['lan']}")
        except Exception as exc:
            log.exception("报告工作台启动失败")
            self._report_server = None
            self._log(f"改善报告工作台启动失败：{exc}（可在设置中修改 web_port）")

    def open_report_workbench(self) -> None:
        """入口对话框 → 记住日期/小时 → 打开浏览器工作台。"""
        if self._report_server is None:
            self._start_report_server()
        if self._report_server is None:
            messagebox.showerror("改善报告", "报告工作台服务未启动，请检查日志或端口占用。",
                                 parent=self.root)
            return
        self._report_server.cameras = list(self.cameras)
        dialog = ReportEntryDialog(
            self.root, self.storage, self.cameras,
            url_provider=self._report_server.urls)
        if not dialog.result:
            return
        day_text, hours = dialog.result
        self._show_window()
        urls = self._report_server.urls(date.fromisoformat(day_text), hours)
        webbrowser.open(urls["local"])
        self._log(f"打开改善报告工作台：{urls['local']}")

    def _web_base_url(self) -> str | None:
        """确保网页服务已启动并返回本机地址；失败时提示并返回 None。"""
        if self._report_server is None:
            self._start_report_server()
        if self._report_server is None:
            messagebox.showerror("网页后台", "网页后台服务未启动，请检查日志或端口占用。",
                                 parent=self.root)
            return None
        self._report_server.cameras = list(self.cameras)
        return f"http://127.0.0.1:{self._report_server.port}"

    def open_web_backend(self) -> None:
        """在浏览器打开网页后台的任务进度页（服务未启动则先启动）。"""
        base = self._web_base_url()
        if base is None:
            return
        url = f"{base}/?view=tasks"
        webbrowser.open(url)
        self._log(f"打开网页后台任务进度：{url}")

    def open_okng_page(self) -> None:
        """在浏览器打开网页后台的 OK/NG 率看板。"""
        base = self._web_base_url()
        if base is None:
            return
        url = f"{base}/?view=okng"
        webbrowser.open(url)
        self._log(f"打开 OK/NG 率看板：{url}")

    def copy_web_backend_url(self) -> None:
        """复制网页后台访问地址（优先局域网，便于工控机/手机查看进度）。"""
        if self._report_server is None:
            self._start_report_server()
        if self._report_server is None:
            messagebox.showerror("网页后台", "网页后台服务未启动，请检查日志或端口占用。",
                                 parent=self.root)
            return
        urls = self._report_server.urls()
        url = urls["lan"] or urls["local"]
        self.clipboard_clear()
        self.clipboard_append(url)
        self._log(f"已复制网页后台地址：{url}")

    def _open_entry_from_web(self, day_text: str, hours: list[int]) -> None:
        """网页点「重新选择」→ 在 Tk 主线程弹出同一入口对话框，结果回传网页。"""
        done = threading.Event()
        outcome: dict = {}

        def show() -> None:
            try:
                self._show_window()
                dialog = ReportEntryDialog(
                    self.root, self.storage, self.cameras,
                    url_provider=self._report_server.urls,
                    default_day=day_text, default_hours=hours)
                outcome["value"] = dialog.result
            except Exception:
                log.exception("网页触发入口对话框失败")
            finally:
                done.set()

        self.root.after(0, show)
        done.wait(600)
        value = outcome.get("value")
        if value:
            self._report_server.entry_result = {"date": value[0], "hours": value[1]}

    # ---------- 生命周期 & 托盘 ----------

    def on_close(self) -> None:
        """关闭按钮 → 隐藏到托盘（如果可用）；否则直接退出。"""
        if self._tray.available:
            self._hide_to_tray()
        else:
            self._really_quit()

    def _hide_to_tray(self) -> None:
        """隐藏主窗口到任务栏和 Alt+Tab，进程继续在托盘后台运行。"""
        self.root.withdraw()
        self._log("窗口已隐藏到托盘（右键托盘图标可退出）")

    def _show_window(self) -> None:
        """从托盘恢复主窗口到前台。"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _toggle_window(self) -> None:
        """系统菜单 / 托盘显示"显示/隐藏主窗口"切换。"""
        if self.root.state() == "withdrawn":
            self._show_window()
        else:
            self._hide_to_tray()

    def _really_quit(self) -> None:
        """真正退出：断开相机 → 封存 AV1 → 保存配置 → 销毁窗口。"""
        self.disconnect_all()
        self.root.after(300, self._finish_close)

    def _finish_close(self) -> None:
        self._db_stop.set()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.monitor.stop_compressor()  # 停止后台 AV1 压缩守护线程
        try:
            from . import web

            web.stop_web_server()  # 关闭报告工作台（含后台任务）
        except Exception:
            log.debug("关闭报告工作台失败", exc_info=True)
        self.storage.close()
        save_config(self.data_dir, self.cameras)
        save_settings(self.data_dir, self.settings)
        self._tray.stop()
        from cameratinymes import renderer

        renderer.shutdown()  # 关闭 WebView 渲染子进程
        self.root.destroy()

    # ---------- 设置 ----------

    def show_settings(self) -> None:
        before = (self.settings.web_host, self.settings.web_port, self.settings.web_token)
        dlg = SettingsDialog(self.root, self.settings, self.data_dir)
        if dlg.result:
            self.settings = dlg.result
            save_settings(self.data_dir, self.settings)
            after = (self.settings.web_host, self.settings.web_port, self.settings.web_token)
            if after != before:
                from . import web

                web.stop_web_server()
                self._report_server = None
                self._start_report_server()
            self._log(f"设置已保存 → {self.settings}")

    def run(self) -> None:
        self.root.mainloop()


# ---------- 单元格文本格式 ----------
# 名称,数据类型,记录模式,event|poll,轮询间隔秒,下限,上限,单位,用途,通过值,不通过值,注解
_CELL_TEXT_HINT = (
    "每行一个单元格：名称,数据类型,记录模式,event|poll,轮询间隔秒,下限,上限,单位,"
    "用途,通过值,不通过值,注解\n"
    "数据类型：auto（未知·运行时推断）/ int / float / string / bool / image（图像）/ other\n"
    "记录模式：simple（仅记录·不判定）/ advanced（记录+阈值判定）\n"
    "用途：auto（按名称推断）/ data（普通数据）/ result（判定结果）/ image（报告图片）\n"
    "上下限仅对 advanced + int/float/auto 有效；simple 模式忽略阈值\n"
    "通过值/不通过值仅用途为 result 时生效，多个值用 / 分隔（默认 OK / NG）\n"
    "image 类型：从相机下载图像，按 AV1 帧序列分片入库并嵌入 xlsx；无 ffmpeg 时回退逐张 BLOB；"
    "报告图片单元格另外保存结果渲染图\n"
    "示例：C36,float,advanced,event,0,175,182,mm,data,,,焊缝宽度\n"
    "      A0,image,simple,event,0,,,,image,,,\n"
    "      G24,auto,simple,event,0,,,,result,OK,NG,判定结果"
)


def _cells_to_text(cells: list[CellConfig]) -> str:
    lines = []
    for c in cells:
        lines.append(",".join([
            c.name, c.data_type, c.record_mode, c.sample_mode,
            f"{c.poll_interval:g}",
            "" if c.lower is None else f"{c.lower:g}",
            "" if c.upper is None else f"{c.upper:g}",
            c.unit, c.role,
            " / ".join(c.pass_values), " / ".join(c.fail_values),
            c.note,
        ]))
    return "\n".join(lines)


def _text_to_cells(text: str) -> list[CellConfig]:
    cells = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0]
        if not name:
            continue

        def num(i):
            if len(parts) > i and parts[i] not in ("", None):
                try:
                    return float(parts[i])
                except ValueError:
                    return None
            return None

        # v1 旧格式（无 data_type/record_mode/unit）：第 2 段为 event/poll
        if len(parts) > 1 and parts[1] in ("event", "poll"):
            data_type = "auto"
            record_mode = "advanced"
            mode = parts[1]
            interval = num(2) or 1.0
            lower, upper = num(3), num(4)
            unit = ""
            note = ",".join(parts[5:]) if len(parts) > 5 else ""
        # v2 中间格式（有 data_type，无 record_mode）：第 2 段为数据类型且第 3 段为 event/poll
        elif (len(parts) > 2 and parts[1] in CELL_DATA_TYPES
              and parts[2] in ("event", "poll")):
            data_type = parts[1]
            record_mode = "advanced"
            mode = parts[2]
            interval = num(3) or 1.0
            lower, upper = num(4), num(5)
            unit = parts[6] if len(parts) > 6 else ""
            note = ",".join(parts[7:]) if len(parts) > 7 else ""
        # v4 新格式：名称,数据类型,记录模式,采样模式,间隔,下限,上限,单位,用途,通过值,不通过值,注解
        elif (len(parts) > 8 and parts[8] in CELL_ROLES):
            data_type = parts[1] if parts[1] else "auto"
            if data_type not in CELL_DATA_TYPES:
                data_type = "auto"
            record_mode = parts[2] if parts[2] else "simple"
            if record_mode not in CELL_RECORD_MODES:
                record_mode = "simple"
            mode = parts[3] if parts[3] else "event"
            if mode not in ("event", "poll"):
                mode = "event"
            interval = num(4) or 1.0
            lower, upper = num(5), num(6)
            unit = parts[7]
            role = parts[8]
            passes = _split_tokens(parts[9] if len(parts) > 9 else "")
            fails = _split_tokens(parts[10] if len(parts) > 10 else "")
            note = ",".join(parts[11:]) if len(parts) > 11 else ""
            cells.append(CellConfig(name=name, data_type=data_type,
                                    record_mode=record_mode, sample_mode=mode,
                                    poll_interval=interval, lower=lower, upper=upper,
                                    unit=unit, note=note, role=role,
                                    pass_values=passes, fail_values=fails))
            continue
        # v3 新格式：名称,数据类型,记录模式,采样模式,...
        else:
            data_type = parts[1] if len(parts) > 1 and parts[1] else "auto"
            if data_type not in CELL_DATA_TYPES:
                data_type = "auto"
            record_mode = parts[2] if len(parts) > 2 and parts[2] else "simple"
            if record_mode not in CELL_RECORD_MODES:
                record_mode = "simple"
            mode = parts[3] if len(parts) > 3 and parts[3] else "event"
            if mode not in ("event", "poll"):
                mode = "event"
            interval = num(4) or 1.0
            lower, upper = num(5), num(6)
            unit = parts[7] if len(parts) > 7 else ""
            note = ",".join(parts[8:]) if len(parts) > 8 else ""

        cells.append(CellConfig(name=name, data_type=data_type, record_mode=record_mode,
                                sample_mode=mode, poll_interval=interval,
                                lower=lower, upper=upper, unit=unit, note=note))
    return cells


# 单元格用途：界面显示中文，配置仍存英文枚举
_ROLE_LABELS = {
    "auto": "自动（按名称推断）",
    "data": "普通数据",
    "result": "判定结果（OK/NG）",
    "image": "报告图片",
}


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, _ROLE_LABELS["auto"])


# 表格中的短标签
_ROLE_SHORT = {
    "auto": "自动",
    "data": "数据",
    "result": "判定结果",
    "image": "报告图片",
}


def _role_value(label: str) -> str:
    for role, text in _ROLE_LABELS.items():
        if text == label:
            return role
    return label if label in CELL_ROLES else "auto"


def _split_tokens(text: str) -> list[str]:
    """把 "OK / GOOD" 之类的输入拆成大写去重列表。"""
    tokens: list[str] = []
    for part in str(text or "").replace("、", "/").replace("|", "/").split("/"):
        token = part.strip().upper()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


class CellFormDialog(_IconToplevel):
    """单单元格编辑表单：上下限仅对数值类型可用。"""

    def __init__(self, parent, cell: CellConfig | None = None):
        super().__init__(parent)
        self.title("单元格设置")
        self.result: CellConfig | None = None
        self.transient(parent)
        self.configure(bg=C["chrome"])
        theme.title_tab(self, "单元格设置")

        cell = cell or CellConfig(name="", record_mode="simple")  # 新增单元格默认 simple 仅记录
        self._cell = CellConfig(**cell.__dict__)

        body = tk.Frame(self, bg=C["chrome"])
        body.pack(fill="both", expand=True, padx=8, pady=6)

        self._name_var = tk.StringVar(value=cell.name)
        self._type_var = tk.StringVar(value=cell.data_type)
        self._record_var = tk.StringVar(value=cell.record_mode)
        self._mode_var = tk.StringVar(value=cell.sample_mode)
        self._interval_var = tk.StringVar(value=f"{cell.poll_interval:g}")
        self._lower_var = tk.StringVar(value="" if cell.lower is None else f"{cell.lower:g}")
        self._upper_var = tk.StringVar(value="" if cell.upper is None else f"{cell.upper:g}")
        self._unit_var = tk.StringVar(value=cell.unit)
        self._note_var = tk.StringVar(value=cell.note)
        self._role_var = tk.StringVar(value=_role_label(cell.role))
        self._pass_var = tk.StringVar(value=" / ".join(cell.pass_values))
        self._fail_var = tk.StringVar(value=" / ".join(cell.fail_values))

        def row(r, label, widget):
            tk.Label(body, text=label, bg=C["chrome"], fg=C["ink"],
                     font=theme.FONT).grid(row=r, column=0, sticky="e", padx=4, pady=3)
            widget.grid(row=r, column=1, sticky="we", padx=4, pady=3)

        self._name_entry = tk.Entry(body, textvariable=self._name_var, width=30)
        row(0, "单元格名称", self._name_entry)

        self._type_combo = ttk.Combobox(body, textvariable=self._type_var, width=28,
                                        values=CELL_DATA_TYPES, state="readonly")
        self._type_combo.bind("<<ComboboxSelected>>", lambda e: self._update_limit_state())
        row(1, "数据类型", self._type_combo)

        self._record_combo = ttk.Combobox(body, textvariable=self._record_var, width=28,
                                          values=CELL_RECORD_MODES, state="readonly")
        self._record_combo.bind("<<ComboboxSelected>>", lambda e: self._update_limit_state())
        row(2, "记录模式", self._record_combo)

        self._mode_combo = ttk.Combobox(body, textvariable=self._mode_var, width=28,
                                        values=("event", "poll"), state="readonly")
        self._mode_combo.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        row(3, "采样模式", self._mode_combo)

        self._interval_entry = tk.Entry(body, textvariable=self._interval_var, width=30)
        row(4, "轮询间隔(秒)", self._interval_entry)

        self._lower_entry = tk.Entry(body, textvariable=self._lower_var, width=30)
        row(5, "下限", self._lower_entry)

        self._upper_entry = tk.Entry(body, textvariable=self._upper_var, width=30)
        row(6, "上限", self._upper_entry)

        self._unit_entry = tk.Entry(body, textvariable=self._unit_var, width=30)
        row(7, "单位", self._unit_entry)

        self._note_entry = tk.Entry(body, textvariable=self._note_var, width=30)
        row(8, "注解", self._note_entry)

        self._role_combo = ttk.Combobox(body, textvariable=self._role_var, width=28,
                                        values=[_ROLE_LABELS[r] for r in CELL_ROLES],
                                        state="readonly")
        self._role_combo.bind("<<ComboboxSelected>>", lambda e: self._update_role_state())
        row(9, "用途", self._role_combo)

        self._pass_entry = tk.Entry(body, textvariable=self._pass_var, width=30)
        row(10, "通过值", self._pass_entry)

        self._fail_entry = tk.Entry(body, textvariable=self._fail_var, width=30)
        row(11, "不通过值", self._fail_entry)

        self._hint_label = tk.Label(body, text="", bg=C["chrome"], fg=C["teal"],
                                    font=theme.FONT, justify="left", wraplength=320)
        self._hint_label.grid(row=12, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))

        body.columnconfigure(1, weight=1)

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=6)
        theme.beos_button(btns, "确定", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self._update_limit_state()
        self._update_role_state()
        self._on_mode_change()

        self.grab_set()
        parent.wait_window(self)

    def _update_limit_state(self) -> None:
        """根据数据类型与记录模式决定上下限是否可用，并更新提示。"""
        simple = self._record_var.get() == "simple"
        numeric = self._type_var.get() in NUMERIC_DATA_TYPES
        limits_enabled = (not simple) and numeric
        for ent in (self._lower_entry, self._upper_entry):
            ent.config(state="normal" if limits_enabled else "disabled")

        if simple:
            self._hint_label.config(
                text="简单记录模式：仅记录单元格数值，不做上下限阈值判定，判定结果恒为 NULL。",
                fg=C["teal"])
        elif self._type_var.get() == "image":
            self._hint_label.config(
                text="图像类型：从相机下载图像存入数据库并嵌入 xlsx；"
                     "报告图片单元格另外保存结果渲染图。",
                fg=C["teal"])
        elif numeric:
            self._hint_label.config(
                text="高级模式·数值类型：上下限阈值生效。auto 类型在运行时按实际值推断。",
                fg=C["teal"])
        else:
            self._hint_label.config(
                text=f"高级模式·非数值类型（{self._type_var.get()}）：上下限不生效，判定结果为 NULL。",
                fg=C["red_dot"])

    def _update_role_state(self) -> None:
        """按用途联动：图像用途强制 image 类型，仅判定结果用途可编辑通过/不通过值。"""
        role = _role_value(self._role_var.get())
        if role == "image" and self._type_var.get() != "image":
            self._type_var.set("image")
            self._update_limit_state()
        result = role == "result"
        for ent in (self._pass_entry, self._fail_entry):
            ent.config(state="normal" if result else "disabled")
        if result and not self._pass_var.get().strip() and not self._fail_var.get().strip():
            self._pass_var.set("OK")
            self._fail_var.set("NG")

    def _on_mode_change(self) -> None:
        poll = self._mode_var.get() == "poll"
        self._interval_entry.config(state="normal" if poll else "disabled")

    def _ok(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("错误", "请填写单元格名称。", parent=self)
            return
        try:
            interval = float(self._interval_var.get()) if self._interval_var.get() else 1.0
        except ValueError:
            messagebox.showerror("错误", "轮询间隔必须是数字。", parent=self)
            return

        def parse_num(var):
            s = var.get().strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        simple = self._record_var.get() == "simple"
        lower = None if simple else parse_num(self._lower_var)
        upper = None if simple else parse_num(self._upper_var)
        role = _role_value(self._role_var.get())
        passes = _split_tokens(self._pass_var.get())
        fails = _split_tokens(self._fail_var.get())
        if role == "result":
            if not passes or not fails:
                messagebox.showerror("错误", "判定结果单元格必须填写通过值与不通过值，"
                                             "多个值用 / 分隔（如 OK / GOOD）。", parent=self)
                return
            common = sorted(set(passes) & set(fails))
            if common:
                messagebox.showerror("错误", "通过值与不通过值不能重复："
                                             + "、".join(common), parent=self)
                return
        data_type = self._type_var.get()
        if role == "image":
            data_type = "image"
        self.result = CellConfig(
            name=name,
            data_type=data_type,
            record_mode=self._record_var.get(),
            sample_mode=self._mode_var.get(),
            poll_interval=interval,
            lower=lower, upper=upper,
            unit=self._unit_var.get().strip(),
            note=self._note_var.get().strip(),
            role=role,
            pass_values=passes,
            fail_values=fails,
        )
        self.destroy()


class CameraSelectDialog(_IconToplevel):
    """相机选择对话框：下拉框可直接选择，也可手动输入相机名。"""

    def __init__(self, parent, camera_names: list[str]):
        super().__init__(parent)
        self.title("配置单元格")
        self.result: str | None = None
        self.transient(parent)
        self.configure(bg=C["chrome"])
        theme.title_tab(self, "配置单元格")

        body = tk.Frame(self, bg=C["chrome"])
        body.pack(fill="x", padx=12, pady=10)

        tk.Label(body, text="选择或输入相机名：", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).pack(anchor="w", pady=(0, 4))

        self._var = tk.StringVar()
        self._combo = ttk.Combobox(body, textvariable=self._var, values=camera_names,
                                   width=32)
        self._combo.pack(fill="x")
        if camera_names:
            self._combo.current(0)
        self._combo.bind("<Return>", lambda e: self._ok())

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=(0, 10))
        theme.beos_button(btns, "确定", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self.grab_set()
        self._combo.focus_set()
        parent.wait_window(self)

    def _ok(self) -> None:
        val = self._var.get().strip()
        if val:
            self.result = val
        self.destroy()


def _parse_dt(text: str) -> datetime | None:
    """解析日期时间文本，支持 YYYY-MM-DD[ HH:MM:SS]，空串返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("/", "-"))
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


class _CellChecklist(tk.Frame):
    """单元格勾选列表：可全选/反选，返回勾选项。"""

    def __init__(self, master, storage, camera_getter, title: str = "单元格"):
        super().__init__(master, bg=C["chrome_lighter"])
        self._storage = storage
        self._camera_getter = camera_getter
        self._vars: dict[str, tk.BooleanVar] = {}

        header = tk.Frame(self, bg=C["chrome_lighter"])
        header.pack(fill="x", padx=4, pady=2)
        self._all_var = tk.BooleanVar(value=False)
        self._all_cb = tk.Checkbutton(header, text="全选", variable=self._all_var,
                                      command=self._toggle_all, bg=C["chrome_lighter"],
                                      fg=C["ink"], font=theme.FONT)
        self._all_cb.pack(side="left")
        self._count_label = tk.Label(header, text="", bg=C["chrome_lighter"],
                                     fg=C["chrome_dark"], font=theme.FONT)
        self._count_label.pack(side="right")

        self._body = tk.Frame(self, bg=C["chrome_lighter"])
        self._body.pack(fill="both", expand=True, padx=4, pady=2)

    def _toggle_all(self) -> None:
        val = self._all_var.get()
        for v in self._vars.values():
            v.set(val)

    def refresh(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        self._vars.clear()
        camera = self._camera_getter()
        cells = self._storage.cells(camera) if camera else []
        for cell in cells:
            var = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(self._body, text=cell, variable=var,
                                bg=C["chrome_lighter"], fg=C["ink"], font=theme.FONT,
                                anchor="w")
            cb.pack(fill="x", anchor="w")
            self._vars[cell] = var
        self._count_label.config(text=f"共 {len(cells)} 个单元格" if cells else "该相机暂无数据")
        self._all_var.set(False)

    def selected(self) -> list[str]:
        return [c for c, v in self._vars.items() if v.get()]


class DeleteDataDialog(_IconToplevel):
    """删除相机数据：选择相机 + 勾选单元格。未勾选单元格则删除该相机全部数据。"""

    def __init__(self, parent, camera_names: list[str], storage):
        super().__init__(parent)
        self.title("删除相机数据")
        self.result: tuple[str, list[str]] | None = None
        self._storage = storage
        self.transient(parent)
        self.configure(bg=C["chrome"])
        self.maxsize(480, 560)
        theme.title_tab(self, "删除相机数据")

        top = tk.Frame(self, bg=C["chrome"])
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="选择或输入相机名：", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).pack(anchor="w")
        self._cam_var = tk.StringVar()
        self._combo = ttk.Combobox(top, textvariable=self._cam_var, values=camera_names,
                                   width=30)
        self._combo.pack(fill="x", pady=(2, 0))
        if camera_names:
            self._combo.current(0)
        self._combo.bind("<<ComboboxSelected>>", lambda e: self._cells.refresh())
        self._combo.bind("<KeyRelease>", lambda e: self._cells.refresh())

        self._cells = _CellChecklist(self, storage, lambda: self._cam_var.get().strip())
        self._cells.pack(fill="both", expand=True, padx=10, pady=2)
        self._cells.refresh()

        hint = tk.Label(self, text="提示：不勾选任何单元格将删除该相机全部数据。",
                        bg=C["chrome"], fg=C["red_dot"], font=theme.FONT)
        hint.pack(anchor="w", padx=10, pady=(0, 2))

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=8)
        theme.beos_button(btns, "删除", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self.grab_set()
        parent.wait_window(self)

    def _ok(self) -> None:
        camera = self._cam_var.get().strip()
        if not camera:
            messagebox.showerror("错误", "请选择或输入相机名。", parent=self)
            return
        cells = self._cells.selected()
        msg = (f"确定删除相机「{camera}」的"
               f"{('全部' if not cells else str(len(cells)) + ' 个单元格')}数据？\n"
               "此操作不可恢复。")
        if not messagebox.askyesno("确认删除", msg, parent=self):
            return
        self.result = (camera, cells)
        self.destroy()


class ExportDialog(_IconToplevel):
    """选择性导出：相机 + 单元格 + 时间范围。"""

    def __init__(self, parent, camera_names: list[str], storage):
        super().__init__(parent)
        self.title("导出数据")
        self.result: tuple[str, list[str], datetime | None, datetime | None] | None = None
        self._storage = storage
        self.transient(parent)
        self.configure(bg=C["chrome"])
        self.maxsize(520, 640)
        theme.title_tab(self, "导出数据")

        top = tk.Frame(self, bg=C["chrome"])
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="选择或输入相机名：", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).pack(anchor="w")
        self._cam_var = tk.StringVar()
        self._combo = ttk.Combobox(top, textvariable=self._cam_var, values=camera_names,
                                   width=30)
        self._combo.pack(fill="x", pady=(2, 0))
        if camera_names:
            self._combo.current(0)
        self._combo.bind("<<ComboboxSelected>>", lambda e: self._cells.refresh())
        self._combo.bind("<KeyRelease>", lambda e: self._cells.refresh())

        self._cells = _CellChecklist(self, storage, lambda: self._cam_var.get().strip())
        self._cells.pack(fill="both", expand=True, padx=10, pady=2)
        self._cells.refresh()

        time_frame = tk.LabelFrame(self, text="时间范围（可选，留空不限）",
                                   bg=C["chrome"], fg=C["ink"], font=theme.FONT,
                                   labelanchor="nw")
        time_frame.pack(fill="x", padx=10, pady=4)
        tk.Label(time_frame, text="起：", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self._start_var = tk.StringVar()
        tk.Entry(time_frame, textvariable=self._start_var, width=22).grid(
            row=0, column=1, sticky="w", padx=4, pady=3)
        tk.Label(time_frame, text="止：", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self._end_var = tk.StringVar()
        tk.Entry(time_frame, textvariable=self._end_var, width=22).grid(
            row=1, column=1, sticky="w", padx=4, pady=3)
        tk.Label(time_frame, text="格式：YYYY-MM-DD HH:MM:SS", bg=C["chrome"],
                 fg=C["chrome_dark"], font=theme.FONT).grid(
            row=0, column=2, rowspan=2, sticky="w", padx=8)

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=8)
        theme.beos_button(btns, "导出", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self.grab_set()
        parent.wait_window(self)

    def _ok(self) -> None:
        camera = self._cam_var.get().strip()
        if not camera:
            messagebox.showerror("错误", "请选择或输入相机名。", parent=self)
            return
        start = _parse_dt(self._start_var.get())
        end = _parse_dt(self._end_var.get())
        if self._start_var.get().strip() and start is None:
            messagebox.showerror("错误", "起始时间格式错误，应为 YYYY-MM-DD HH:MM:SS。",
                                 parent=self)
            return
        if self._end_var.get().strip() and end is None:
            messagebox.showerror("错误", "截止时间格式错误，应为 YYYY-MM-DD HH:MM:SS。",
                                 parent=self)
            return
        cells = self._cells.selected()
        self.result = (camera, cells, start, end)
        self.destroy()


class ChartCellSelectDialog(_IconToplevel):
    """趋势图单元格选择：文本输入框（单个）+ 多选框（批量）。"""

    def __init__(self, parent, camera_name: str, storage):
        super().__init__(parent)
        self.title("趋势图")
        self.result: tuple[str, list[str]] | None = None
        self.transient(parent)
        self.configure(bg=C["chrome"])
        self.maxsize(480, 560)
        theme.title_tab(self, f"趋势图 - {camera_name}")

        top = tk.Frame(self, bg=C["chrome"])
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="输入单元格名（单个）：", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).pack(anchor="w")
        self._text_var = tk.StringVar()
        tk.Entry(top, textvariable=self._text_var, width=30).pack(fill="x", pady=(2, 0))

        self._cells = _CellChecklist(self, storage, lambda: camera_name)
        self._cells.pack(fill="both", expand=True, padx=10, pady=2)
        self._cells.refresh()

        hint = tk.Label(
            self,
            text="提示：文本框填写则绘制该单元格；勾选多个则逐个绘制趋势图；两者可同时使用。",
            bg=C["chrome"], fg=C["teal"], font=theme.FONT, justify="left", wraplength=440)
        hint.pack(anchor="w", padx=10, pady=(0, 2))

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=8)
        theme.beos_button(btns, "确定", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self.grab_set()
        parent.wait_window(self)

    def _ok(self) -> None:
        text = self._text_var.get().strip()
        selected = self._cells.selected()
        if not text and not selected:
            messagebox.showinfo("提示", "请输入单元格名或勾选单元格。", parent=self)
            return
        self.result = (text, selected)
        self.destroy()


class CellEditorDialog(_IconToplevel):
    """交互式单元格编辑器：表格 + 增删改 + 文本编辑回退。"""

    COLUMNS = ("name", "role", "type", "record", "mode", "interval",
               "lower", "upper", "unit", "pass", "fail", "note")
    HEADINGS = ("名称", "用途", "类型", "记录", "模式", "间隔",
                "下限", "上限", "单位", "通过值", "不通过值", "注解")
    WIDTHS = (95, 60, 55, 55, 50, 50, 55, 55, 45, 60, 65, 110)

    def __init__(self, parent, cells: list[CellConfig]):
        super().__init__(parent)
        self.title("订阅单元格")
        self.result: list[CellConfig] | None = None
        self.transient(parent)
        self.configure(bg=C["chrome"])
        self.maxsize(820, 600)  # 限制窗口最大宽度，避免表格过宽
        theme.title_tab(self, "订阅单元格", f"{len(cells)} 个")

        self._cells: list[CellConfig] = [CellConfig(**c.__dict__) for c in cells]

        top = tk.Frame(self, bg=C["chrome"])
        top.pack(fill="both", expand=True, padx=8, pady=6)

        self._tree = ttk.Treeview(top, columns=self.COLUMNS, show="headings", height=8)
        for col, head, w in zip(self.COLUMNS, self.HEADINGS, self.WIDTHS):
            self._tree.heading(col, text=head)
            self._tree.column(col, width=w, anchor="w")
        self._tree.pack(side="left", fill="both", expand=True)
        self._tree.bind("<Double-1>", lambda e: self._edit())

        sb = ttk.Scrollbar(top, orient="vertical", command=self._tree.yview)
        sb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=sb.set)

        side = tk.Frame(self, bg=C["chrome"])
        side.pack(fill="x", padx=8, pady=(0, 4))
        theme.beos_button(side, "添加", self._add).pack(side="left", padx=3)
        theme.beos_button(side, "编辑", self._edit).pack(side="left", padx=3)
        theme.beos_button(side, "删除", self._delete).pack(side="left", padx=3)
        theme.beos_button(side, "上移", self._move_up).pack(side="left", padx=3)
        theme.beos_button(side, "下移", self._move_down).pack(side="left", padx=3)
        theme.beos_button(side, "文本编辑", self._text_edit).pack(side="left", padx=3)

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=6)
        theme.beos_button(btns, "确定", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self._refresh()
        self.grab_set()
        parent.wait_window(self)

    def _refresh(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        for c in self._cells:
            self._tree.insert("", "end", values=(
                c.name, _ROLE_SHORT.get(c.role, _ROLE_SHORT["auto"]),
                c.data_type, c.record_mode, c.sample_mode, f"{c.poll_interval:g}",
                "" if c.lower is None else f"{c.lower:g}",
                "" if c.upper is None else f"{c.upper:g}",
                c.unit, " / ".join(c.pass_values), " / ".join(c.fail_values), c.note,
            ))
        theme.title_tab(self, "订阅单元格", f"{len(self._cells)} 个")

    def _selected_index(self) -> int | None:
        sel = self._tree.selection()
        if not sel:
            return None
        return self._tree.index(sel[0])

    def _add(self) -> None:
        dlg = CellFormDialog(self)
        if dlg.result:
            self._cells.append(dlg.result)
            self._refresh()

    def _edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选择一个单元格。", parent=self)
            return
        dlg = CellFormDialog(self, self._cells[idx])
        if dlg.result:
            self._cells[idx] = dlg.result
            self._refresh()

    def _delete(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        if messagebox.askyesno("确认", f"删除单元格「{self._cells[idx].name}」？", parent=self):
            del self._cells[idx]
            self._refresh()

    def _move_up(self) -> None:
        idx = self._selected_index()
        if idx and idx > 0:
            self._cells[idx], self._cells[idx - 1] = self._cells[idx - 1], self._cells[idx]
            self._refresh()
            self._tree.selection_set(self._tree.get_children()[idx - 1])

    def _move_down(self) -> None:
        idx = self._selected_index()
        if idx is not None and idx < len(self._cells) - 1:
            self._cells[idx], self._cells[idx + 1] = self._cells[idx + 1], self._cells[idx]
            self._refresh()
            self._tree.selection_set(self._tree.get_children()[idx + 1])

    def _text_edit(self) -> None:
        text = CellDialog(self, "文本编辑单元格", _CELL_TEXT_HINT,
                          _cells_to_text(self._cells)).result
        if text is not None:
            self._cells = _text_to_cells(text)
            self._refresh()

    def _ok(self) -> None:
        self.result = self._cells
        self.destroy()


class CellDialog(_IconToplevel):
    """多行文本编辑对话框。"""

    def __init__(self, parent, title: str, hint: str, initial: str):
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.transient(parent)
        self.configure(bg=C["chrome"])
        theme.title_tab(self, title)
        tk.Label(self, text=hint, justify="left", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT).pack(anchor="w", padx=8, pady=4)
        self.text = tk.Text(self, width=60, height=10, bg=C["chrome_lighter"], fg=C["ink"],
                            font=("Consolas", 9), relief="flat", highlightthickness=1,
                            highlightbackground=C["chrome_darker"])
        self.text.pack(padx=8, pady=4)
        self.text.insert("1.0", initial)
        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=6)
        theme.beos_button(btns, "确定", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)
        self.grab_set()
        parent.wait_window(self)

    def _ok(self):
        self.result = self.text.get("1.0", "end")
        self.destroy()


class ChartWindow(_IconToplevel):
    """内嵌 matplotlib 图表窗口。"""

    def __init__(self, parent, fig, title: str):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=C["chrome"])
        theme.title_tab(self, title)
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=2, pady=2)


class SettingsDialog(_IconToplevel):
    """应用设置：启动时最小化、开机自启。"""

    def __init__(self, parent, settings: AppSettings, data_dir):
        super().__init__(parent)
        self.title("应用设置")
        self.result: AppSettings | None = None
        self.transient(parent)
        self.configure(bg=C["chrome"])
        theme.title_tab(self, "应用设置")
        self._data_dir = data_dir
        self._settings = settings

        body = tk.Frame(self, bg=C["chrome"])
        body.pack(fill="both", expand=True, padx=12, pady=10)

        # 启动时最小化到托盘
        self._min_var = tk.BooleanVar(value=settings.minimize_to_tray_on_start)
        cb_min = tk.Checkbutton(
            body, text="启动时最小化到系统托盘图标",
            variable=self._min_var,
            bg=C["chrome"], fg=C["ink"], font=theme.FONT, anchor="w",
        )
        cb_min.pack(anchor="w", pady=2)
        tk.Label(
            body,
            text="勾选后下次启动会自动隐藏主窗口，通过右键托盘图标唤出或退出。",
            bg=C["chrome"], fg=C["chrome_dark"], font=theme.FONT,
            justify="left", wraplength=420,
        ).pack(anchor="w", padx=24)

        tk.Frame(body, bg=C["chrome"], height=1).pack(fill="x", pady=10)

        # 开机自启
        self._auto_var = tk.BooleanVar(value=settings.autostart_enabled)
        cb_auto = tk.Checkbutton(
            body, text="开机自启（创建快捷方式到启动文件夹）",
            variable=self._auto_var,
            bg=C["chrome"], fg=C["ink"], font=theme.FONT, anchor="w",
        )
        cb_auto.pack(anchor="w", pady=2)
        startup_dir = str(Path.home() / "AppData" / "Roaming" / "Microsoft" /
                          "Windows" / "Start Menu" / "Programs" / "Startup")
        tk.Label(
            body,
            text=f"快捷方式位置：{startup_dir}",
            bg=C["chrome"], fg=C["chrome_dark"], font=theme.FONT,
            justify="left", wraplength=420,
        ).pack(anchor="w", padx=24)
        tk.Label(
            body,
            text="（仅创建当前用户的 shell:startup 快捷方式，无需管理员权限）",
            bg=C["chrome"], fg=C["chrome_dark"], font=theme.FONT,
        ).pack(anchor="w", padx=24)

        # 改善报告工作台
        tk.Frame(body, bg=C["chrome"], height=1).pack(fill="x", pady=10)
        tk.Label(body, text="改善报告工作台", bg=C["chrome"], fg=C["ink"],
                 font=theme.FONT_BOLD, anchor="w").pack(anchor="w")

        grid = tk.Frame(body, bg=C["chrome"])
        grid.pack(fill="x", padx=12, pady=4)
        self._host_var = tk.StringVar(value=settings.web_host or "0.0.0.0")
        self._port_var = tk.StringVar(value=str(settings.web_port or 18110))
        self._token_var = tk.StringVar(value=settings.web_token or "")
        self._project_var = tk.StringVar(value=settings.project_name or "")
        self._dest_var = tk.StringVar(value=settings.destination or "")
        self._line_var = tk.StringVar(value=settings.line_name or "")
        rows = (
            ("监听地址", self._host_var, 16),
            ("端口", self._port_var, 8),
            ("访问口令", self._token_var, 16),
            ("项目名称", self._project_var, 20),
            ("目的地", self._dest_var, 14),
            ("产线", self._line_var, 14),
        )
        for index, (label, var, width) in enumerate(rows):
            tk.Label(grid, text=label, bg=C["chrome"], fg=C["ink"],
                     font=theme.FONT, width=8, anchor="e").grid(
                row=index, column=0, sticky="e", padx=(0, 6), pady=2)
            tk.Entry(grid, textvariable=var, width=width, font=theme.FONT).grid(
                row=index, column=1, sticky="w", pady=2)
        tk.Label(body, text="监听 0.0.0.0 允许局域网其他工控机访问；口令留空表示不校验。"
                            "修改监听地址/端口后会自动重启工作台。",
                 bg=C["chrome"], fg=C["chrome_dark"], font=theme.FONT,
                 justify="left", wraplength=440).pack(anchor="w", padx=24, pady=(2, 0))

        # 实时状态
        from . import autostart as _as
        system_on = _as.is_enabled()
        status_text = (
            "系统侧已存在开机自启快捷方式 ✓" if system_on
            else "系统侧当前无开机自启快捷方式"
        )
        self._status_label = tk.Label(
            body, text=f"状态：{status_text}",
            bg=C["chrome"], fg=C["teal"] if system_on else C["chrome_dark"],
            font=theme.FONT,
        )
        self._status_label.pack(anchor="w", pady=(6, 0))

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=10)
        theme.beos_button(btns, "确定", self._ok, default=True).pack(side="left", padx=4)
        theme.beos_button(btns, "取消", self.destroy).pack(side="left", padx=4)

        self.grab_set()
        parent.wait_window(self)

    def _ok(self) -> None:
        from dataclasses import replace

        from . import autostart as _as

        try:
            port = int(self._port_var.get().strip() or self._settings.web_port)
        except ValueError:
            messagebox.showerror("设置", "端口必须是数字。", parent=self)
            return
        if not 1024 <= port <= 65535:
            messagebox.showerror("设置", "端口需在 1024~65535 之间。", parent=self)
            return
        new = replace(
            self._settings,
            minimize_to_tray_on_start=self._min_var.get(),
            autostart_enabled=self._auto_var.get(),
            web_host=self._host_var.get().strip() or "0.0.0.0",
            web_port=port,
            web_token=self._token_var.get().strip(),
            project_name=self._project_var.get().strip(),
            destination=self._dest_var.get().strip(),
            line_name=self._line_var.get().strip(),
        )
        # 同步系统侧自启状态
        if new.autostart_enabled:
            ok = _as.set(True, log_fn=lambda m: self._log_status("✓ " + m))
            if not ok:
                messagebox.showerror(
                    "失败",
                    "开机自启快捷方式创建失败。\n"
                    "请确认当前用户对 shell:startup 目录有写权限，\n"
                    "或手动创建 WScript.Shell COM 组件可用（win32com 模块可选）。",
                    parent=self,
                )
                return
        else:
            _as.set(False, log_fn=lambda m: self._log_status("• " + m))

        self.result = new
        self.destroy()

    def _log_status(self, msg: str) -> None:
        self._status_label.config(text=msg, fg=C["teal"])


class ExportProgressDialog(_IconToplevel):
    """xlsx 导出进度对话框：进度条 + 当前步骤 + 取消按钮。"""

    def __init__(self, parent, camera: str, on_cancel=None):
        super().__init__(parent)
        self.title(f"导出 xlsx - {camera}")
        self.transient(parent)
        self.configure(bg=C["chrome"])
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._on_cancel_cb = on_cancel
        self._on_done_cb = None
        self._closed = False

        theme.title_tab(self, f"导出 xlsx - {camera}")

        body = tk.Frame(self, bg=C["chrome"])
        body.pack(fill="x", padx=12, pady=10)

        self._msg = tk.Label(body, text="准备中…", bg=C["chrome"], fg=C["ink"],
                             font=theme.FONT, anchor="w", justify="left")
        self._msg.pack(fill="x", pady=(0, 6))

        self._bar = ttk.Progressbar(body, orient="horizontal", mode="determinate",
                                   length=360, maximum=100)
        self._bar.pack(fill="x")

        btns = tk.Frame(self, bg=C["chrome"])
        btns.pack(pady=8)
        self._cancel_btn = theme.beos_button(btns, "取消", self._on_close)
        self._cancel_btn.pack(side="left", padx=4)

        # 尝试 grab_set 但不强求（parent 可能是 MonitorApp 主窗口）
        try:
            self.grab_set()
        except Exception:
            pass

    def attach_queue(self, q: queue.Queue, on_done=None) -> None:
        """绑定后台线程的进度队列并启动 UI 轮询。"""
        self._q = q
        self._on_done_cb = on_done
        self._tick()

    def _tick(self) -> None:
        """每 150ms 从 queue 取一条消息更新 UI。"""
        if self._closed:
            return
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, msg = item
                    pct = int(done / max(total, 1) * 100)
                    self._bar["value"] = pct
                    self._msg.config(text=f"{msg}  ({pct}%)")
                elif kind == "done":
                    path = item[1]
                    self._msg.config(text=f"✅ 已完成 → {Path(path).name}", fg=C["teal"])
                    self._bar["value"] = 100
                    self._set_close_btn()
                    if self._on_done_cb:
                        self._on_done_cb(path)
                    return  # 结束轮询，不再 tick
                elif kind == "cancelled":
                    self._msg.config(text="⏹ 已取消", fg=C["chrome_dark"])
                    self._set_close_btn()
                    return
                elif kind == "error":
                    self._msg.config(text=f"❌ 失败：{item[1]}", fg=C["red_dot"])
                    self._set_close_btn()
                    return
        except queue.Empty:
            pass
        self.after(150, self._tick)

    def _set_close_btn(self) -> None:
        """完成/取消/失败后把「取消」按钮换成「关闭」。"""
        self._cancel_btn.config(text="关闭", command=self._destroy)

    def _on_close(self) -> None:
        """用户点取消（运行中）或关闭按钮（已结束）。"""
        # 若是运行中：回调外部 cancelled.set()，然后关窗
        if self._on_cancel_cb and self._cancel_btn.cget("text") == "取消":
            self._on_cancel_cb()
            self._on_cancel_cb = None
            self._msg.config(text="正在取消…", fg=C["chrome_dark"])
            # 等后台线程发 cancelled/done 消息后再自动关窗
            # 如果用户再次按关闭则强制关
            return
        self._destroy()

    def _destroy(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
