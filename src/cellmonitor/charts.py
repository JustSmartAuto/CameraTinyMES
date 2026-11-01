"""matplotlib 图表：折线图 / 散点图 / 趋势图，中文字体内嵌自 assets/*.ttf。

虚线基线 = 参考值/阈值，红色段 = 超差。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


def _assets_dir() -> Path:
    """开发环境取仓库 assets；PyInstaller 取 _MEIPASS 内打包的 assets。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "assets"
    return Path(__file__).resolve().parent.parent.parent / "assets"


ASSETS = _assets_dir()


def setup_fonts() -> None:
    """从 assets 加载中文字体（不依赖系统字体）。"""
    loaded = False
    for ttf in ("Smiley Sans Oblique.ttf", "ZhuqueFangsong-Regular.ttf"):
        path = ASSETS / ttf
        if path.exists():
            try:
                font_manager.fontManager.addfont(str(path))
                loaded = True
            except Exception:
                continue
    if loaded:
        names = {f.name for f in font_manager.fontManager.ttflist if "Smiley" in f.name or "Zhuque" in f.name}
        if names:
            plt.rcParams["font.family"] = sorted(names)[0]
    plt.rcParams["axes.unicode_minus"] = False


setup_fonts()

NG_COLOR = "#d62728"
OK_COLOR = "#1f77b4"
LINE_COLOR = "#7f7f7f"


def plot_trend(
    points: list[tuple[str, float | None, str, int | None]],
    title: str,
    lower: float | None = None,
    upper: float | None = None,
    out_path: Path | None = None,
):
    """趋势图：散点（OK 蓝 / NG 红）+ 折线 + 阈值虚线基线。points=[(ts, value, text, ok)]。"""
    xs: list[datetime] = []
    ys: list[float] = []
    ng_x: list[datetime] = []
    ng_y: list[float] = []
    for ts, value, _text, ok in points:
        if value is None:
            continue
        t = datetime.fromisoformat(ts)
        xs.append(t)
        ys.append(value)
        if ok == 0:
            ng_x.append(t)
            ng_y.append(value)

    fig, ax = plt.subplots(figsize=(10, 5), dpi=100)
    if xs:
        ax.plot(xs, ys, color=LINE_COLOR, linewidth=0.8, zorder=1)
        ax.scatter(xs, ys, s=14, color=OK_COLOR, zorder=2, label="OK")
    if ng_x:
        ax.scatter(ng_x, ng_y, s=28, color=NG_COLOR, zorder=3, label="NG")
    if lower is not None:
        ax.axhline(lower, color=NG_COLOR, linestyle="--", linewidth=1, label=f"下限 {lower:g}")
    if upper is not None:
        ax.axhline(upper, color=NG_COLOR, linestyle="--", linewidth=1, label=f"上限 {upper:g}")
    ax.set_title(title)
    ax.set_xlabel("时间")
    ax.set_ylabel("数值")
    if lower is not None or upper is not None or ng_x:
        ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path)
    return fig


def plot_scatter(
    points: list[tuple[str, float | None, str, int | None]],
    title: str,
    lower: float | None = None,
    upper: float | None = None,
    out_path: Path | None = None,
):
    """散点图：X=采样序号，Y=数值（按超差着色）。"""
    xs, ys, colors = [], [], []
    for i, (_ts, value, _text, ok) in enumerate(points):
        if value is None:
            continue
        xs.append(i)
        ys.append(value)
        colors.append(NG_COLOR if ok == 0 else OK_COLOR)
    fig, ax = plt.subplots(figsize=(10, 5), dpi=100)
    ax.scatter(xs, ys, s=18, c=colors)
    if lower is not None:
        ax.axhline(lower, color=NG_COLOR, linestyle="--", linewidth=1)
    if upper is not None:
        ax.axhline(upper, color=NG_COLOR, linestyle="--", linewidth=1)
    ax.set_title(f"{title}（散点）")
    ax.set_xlabel("采样序号")
    ax.set_ylabel("数值")
    fig.tight_layout()
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path)
    return fig
