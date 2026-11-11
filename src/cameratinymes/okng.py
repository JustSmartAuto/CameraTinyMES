"""小时 OK/NG 率聚合：供网页工作台与独立看板共用的判定/统计逻辑。

判定口径与主程序报告一致（config.result_specs）：
每台相机优先用配置的结果单元格及其通过/不通过标记；未配置的相机回退
全局 OK/NG 文本（DEFAULT_PASS_VALUES / DEFAULT_FAIL_VALUES）。
samples.ok 列（阈值判定）始终优先。除精确文本匹配外，增加数值等价
兜底（相机输出 1.0 而配置为 1 时仍可判定）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import result_specs
from .storage import DEFAULT_FAIL_VALUES, DEFAULT_PASS_VALUES


def _as_float(text) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _token_nums(tokens) -> frozenset:
    return frozenset(f for f in (_as_float(t) for t in tokens) if f is not None)


def _judge_text(text, passes, fails, pass_nums, fail_nums) -> int | None:
    """按结果单元格标记判定：1=OK 0=NG None=未判定。"""
    t = str(text or "").strip().upper()
    if t in fails:
        return 0
    if t in passes:
        return 1
    f = _as_float(text)
    if f is not None:
        if f in fail_nums:
            return 0
        if f in pass_nums:
            return 1
    return None


def hourly_okng(storage, day: str, cameras=()) -> list[dict]:
    """某天各相机逐小时的 OK/NG 聚合（只统计可判定的样本）。

    cameras 为主程序加载的相机配置列表（config.load_config 结果）。
    返回按配置顺序排列的相机列表，每项：
        {"camera": 名称, "hours": {"08": {"ok": n, "ng": n}, ...},
         "total": 可判定总数, "ok": 总OK, "ng": 总NG}
    """
    start = f"{day}T00:00:00.000"
    end = (datetime.fromisoformat(day) + timedelta(days=1)).isoformat(
        timespec="milliseconds")
    rules: dict[str, tuple[str | None, tuple, tuple]] = {}
    for camera, cell, passes, fails in result_specs(cameras):
        rules[camera.name] = (cell.name, passes, fails)
    rule_nums = {name: (_token_nums(p), _token_nums(f))
                 for name, (_c, p, f) in rules.items()}

    # ok 列全表保留；文本行截断到 40 字符，排除文件名等长串（只影响文本判定）
    rows = storage._fetchall(
        "SELECT camera, cell, ok, text, substr(ts, 12, 2) AS hh, COUNT(*)"
        " FROM samples WHERE ts >= ? AND ts < ?"
        " AND (ok IS NOT NULL OR LENGTH(text) <= 40)"
        " GROUP BY camera, cell, ok, text, hh",
        (start, end))

    cams: dict[str, dict] = {}
    for camera, cell, ok, text, hh, n in rows:
        if not str(hh or "").isdigit():
            continue
        rule = rules.get(camera)
        if rule and rule[0]:
            if cell != rule[0]:
                continue  # 只按该相机配置的结果单元格判定
            _cell, passes, fails = rule
            pass_nums, fail_nums = rule_nums[camera]
        else:
            passes, fails = DEFAULT_PASS_VALUES, DEFAULT_FAIL_VALUES
            pass_nums, fail_nums = _token_nums(passes), _token_nums(fails)
        v = int(ok) if ok is not None else _judge_text(text, passes, fails,
                                                       pass_nums, fail_nums)
        if v is None:
            continue
        cam = cams.setdefault(camera, {"camera": camera, "hours": {}})
        bucket = cam["hours"].setdefault(f"{int(str(hh)) :02d}", {"ok": 0, "ng": 0})
        bucket["ok" if v == 1 else "ng"] += int(n)

    # 相机清单：启用配置的相机（即使无判定也显示空行）+ 库中出现但未配置的相机
    names = [c.name for c in cameras if getattr(c, "enabled", True)]
    names += [n for n in sorted(cams) if n not in names]
    result = []
    for name in names:
        cam = cams.get(name) or {"camera": name, "hours": {}}
        ok = sum(h["ok"] for h in cam["hours"].values())
        ng = sum(h["ng"] for h in cam["hours"].values())
        result.append({**cam, "camera": name, "total": ok + ng, "ok": ok, "ng": ng})
    return result
