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


def _grade(ok: int, ng: int) -> str:
    """与网页一致的分档：none 无数据 / full 全 OK / warn 有 NG / bad NG≥20%。"""
    if not ok + ng:
        return "none"
    if ng <= 0:
        return "full"
    return "warn" if ok / (ok + ng) >= 0.8 else "bad"


def okng_xlsx(day: str, cameras: list[dict]) -> tuple[bytes, str]:
    """某天逐小时 OK/NG 的 Excel 报表，返回 (xlsx 内容, 文件名)。

    表 1「小时合格率」：相机 × 00~23 + 合计，底色按合格率分档（与网页一致）；
    表 2「逐小时明细」：长表（相机/小时/OK/NG/判定数/合格率），便于二次透视。
    """
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    hours = [f"{h:02d}" for h in range(24)]
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center")
    head_font = Font(bold=True, color="1F2D3D")
    head_fill = PatternFill("solid", fgColor="E6ECF2")
    fills = {"full": "E2F0D9", "warn": "FFF2CC", "bad": "FFC7CE", "none": "F5F5F5"}
    last_col = len(hours) + 2

    wb = Workbook()
    ws = wb.active
    ws.title = "小时合格率"
    ws.cell(row=1, column=1, value=f"小时 OK/NG 率 · {day}").font = Font(bold=True, size=14)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 24

    for col, text in enumerate(["相机", *hours, "合计"], start=1):
        cell = ws.cell(row=2, column=col, value=text)
        cell.font, cell.fill, cell.border, cell.alignment = head_font, head_fill, border, center
    ws.row_dimensions[2].height = 18

    row = 3
    g_ok = g_ng = 0
    for cam in cameras:
        ok_all, ng_all = int(cam.get("ok") or 0), int(cam.get("ng") or 0)
        g_ok += ok_all
        g_ng += ng_all
        name = ws.cell(row=row, column=1, value=cam.get("camera"))
        name.border = border
        for col, hh in enumerate(hours, start=2):
            bucket = (cam.get("hours") or {}).get(hh) or {}
            ok, ng = int(bucket.get("ok") or 0), int(bucket.get("ng") or 0)
            cell = ws.cell(row=row, column=col)
            cell.border, cell.alignment = border, center
            cell.fill = PatternFill("solid", fgColor=fills[_grade(ok, ng)])
            if ok + ng:
                cell.value = ok / (ok + ng)
                cell.number_format = "0.0%"
        cell = ws.cell(row=row, column=last_col)
        cell.border, cell.alignment = border, center
        if ok_all + ng_all:
            cell.value = ok_all / (ok_all + ng_all)
            cell.number_format = "0.0%"
        row += 1

    total = ws.cell(row=row, column=1, value="合计")
    total.font, total.fill, total.border, total.alignment = head_font, head_fill, border, center
    for col, hh in enumerate(hours, start=2):
        ok = sum(int(((c.get("hours") or {}).get(hh) or {}).get("ok") or 0) for c in cameras)
        ng = sum(int(((c.get("hours") or {}).get(hh) or {}).get("ng") or 0) for c in cameras)
        cell = ws.cell(row=row, column=col)
        cell.border, cell.alignment = border, center
        cell.fill = PatternFill("solid", fgColor=fills[_grade(ok, ng)])
        if ok + ng:
            cell.value = ok / (ok + ng)
            cell.number_format = "0.0%"
    cell = ws.cell(row=row, column=last_col)
    cell.font, cell.fill, cell.border, cell.alignment = head_font, head_fill, border, center
    if g_ok + g_ng:
        cell.value = g_ok / (g_ok + g_ng)
        cell.number_format = "0.0%"

    ws.column_dimensions["A"].width = 20
    for col in range(2, last_col + 1):
        ws.column_dimensions[get_column_letter(col)].width = 8 if col == last_col else 6.5
    ws.freeze_panes = "B3"

    detail = wb.create_sheet("逐小时明细")
    for col, text in enumerate(["相机", "小时", "OK", "NG", "判定数", "合格率"], start=1):
        cell = detail.cell(row=1, column=col, value=text)
        cell.font, cell.fill, cell.border, cell.alignment = head_font, head_fill, border, center
    line = 2
    for cam in cameras:
        for hh in hours:
            bucket = (cam.get("hours") or {}).get(hh) or {}
            ok, ng = int(bucket.get("ok") or 0), int(bucket.get("ng") or 0)
            if not ok + ng:
                continue
            values = [cam.get("camera"), hh, ok, ng, ok + ng, ok / (ok + ng)]
            for col, value in enumerate(values, start=1):
                cell = detail.cell(row=line, column=col, value=value)
                cell.border = border
                if col in (2, 3, 4, 5):
                    cell.alignment = center
                if col == 6:
                    cell.number_format = "0.0%"
            line += 1
    for col, width in zip("ABCDEF", (20, 8, 8, 8, 10, 10)):
        detail.column_dimensions[col].width = width
    detail.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"OKNG_{day}.xlsx"
