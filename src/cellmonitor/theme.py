"""BeOS 界面主题。

设计 token 取自 reference/retro-design-system-main/tokens/07-beos.css：
青绿色桌面背景、灰色窗口 chrome、黄色标题页签 + 红色圆点、
白色内容区、圆角药丸按钮（tkinter 中以立体边框近似）。
"""

from __future__ import annotations

import tkinter as tk

COLORS = {
    "bg": "#2a5556",           # 桌面背景
    "chrome": "#cfcfcf",       # 窗口边框/工具区
    "chrome_light": "#e8e8e8",
    "chrome_lighter": "#ffffff",  # 内容区
    "chrome_dark": "#888888",
    "chrome_darker": "#404040",
    "yellow": "#ffcc00",       # 标题页签
    "yellow_dark": "#cc9900",
    "red_dot": "#e04040",
    "teal": "#006666",         # 强调色
    "ink": "#000000",
}

FONT = ("Arial", 10)
FONT_BOLD = ("Arial", 10, "bold")
FONT_TITLE = ("Arial", 11, "bold")


def apply(root: tk.Misc) -> None:
    """将 BeOS 配色应用到根窗口与 ttk。"""
    root.configure(bg=COLORS["bg"])
    from tkinter import ttk

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=COLORS["chrome"])
    style.configure("TLabel", background=COLORS["chrome"], foreground=COLORS["ink"], font=FONT)
    style.configure("TNotebook", background=COLORS["chrome"])
    style.configure("White.TFrame", background=COLORS["chrome_lighter"])
    style.configure("White.TLabel", background=COLORS["chrome_lighter"], foreground=COLORS["ink"], font=FONT)
    style.configure("Gray.TLabel", background=COLORS["chrome_lighter"], foreground=COLORS["chrome_dark"], font=FONT)


def beos_button(master, text, command=None, default=False, **kw) -> tk.Button:
    """BeOS 药丸按钮近似：亮色底、深色描边、立体高光。"""
    bg = COLORS["yellow"] if default else COLORS["chrome_light"]
    active = COLORS["yellow_dark"] if default else COLORS["chrome_lighter"]
    return tk.Button(
        master, text=text, command=command, font=FONT_BOLD if default else FONT,
        bg=bg, activebackground=active, fg=COLORS["ink"],
        relief="raised", bd=1, highlightthickness=1,
        highlightbackground=COLORS["chrome_darker"], padx=10, pady=1, **kw
    )


def window_frame(master, **kw) -> tk.Frame:
    """BeOS 窗口外框：chrome 底 + 深色细边。"""
    return tk.Frame(
        master, bg=COLORS["chrome"], highlightthickness=1,
        highlightbackground=COLORS["chrome_darker"], bd=0, **kw
    )


def title_tab(master, title: str, right_text: str = "") -> tuple[tk.Frame, tk.Label]:
    """黄色标题页签（红点 + 标题），返回 (页签框, 右侧文本 Label)。"""
    bar = tk.Frame(master, bg=COLORS["yellow"])
    bar.pack(fill="x")
    tk.Label(bar, text="●", bg=COLORS["yellow"], fg=COLORS["red_dot"],
             font=("", 9)).pack(side="left", padx=(6, 2))
    tk.Label(bar, text=title, bg=COLORS["yellow"], fg=COLORS["ink"],
             font=FONT_TITLE).pack(side="left")
    right = tk.Label(bar, text=right_text, bg=COLORS["yellow"], fg=COLORS["ink"], font=FONT)
    right.pack(side="right", padx=6)
    return bar, right


def content_frame(master, **kw) -> tk.Frame:
    """白色内容区。"""
    return tk.Frame(master, bg=COLORS["chrome_lighter"], **kw)
