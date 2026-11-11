"""改善报告存档：入库、库→磁盘落盘、重新导出、保留清理。

设计见 `docs/当日改善报告功能设计.md` §5.2 / §5.3：
- **先入库、后落盘**：`save_report` 单事务写入 `reports` + `report_files`（分块 BLOB）；
- 落盘按 1MB 分块写 `.tmp`，每块检查取消并上报字节进度，成功后 `os.replace` 原子改名；
- 落盘失败/取消**不删除库中存档**，可用 `export_report_to_disk` 重试（任务状态 `partial`）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .report import ReportArtifact, ReportOptions
from .tasks import CancelledError

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024          # 落盘分块 1MB


def reports_dir(data_dir: Path) -> Path:
    """报告落盘目录 `{data_dir}/reports/`（不存在则创建）。"""
    path = Path(data_dir) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest() if data else ""


def _mib(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def save_report(storage, artifact: ReportArtifact, options: ReportOptions,
                created_by: str = "", *, links: list[tuple[int, str]] | None = None,
                extra_payload: dict | None = None) -> int:
    """单事务写入报告元数据与文件分块，返回 `report_id`。"""
    stats = artifact.stats
    payload = {
        "hours": stats.hours,
        "reasons": options.reasons,
        "other_ng": options.other_ng,
        "picked_images": options.picked_images,
        "show_physical": options.show_physical,
        "overlay_annotations": options.overlay_annotations,
        "tickets": options.tickets,
        "warnings": artifact.warnings,
        "stations": [
            {"station": block.station, "station_name": block.station_name,
             "products": block.products, "ng": block.ng,
             "visual_ng": block.visual_ng, "other_ng": block.other_ng,
             "cameras": [c.camera for c in block.cameras]}
            for block in stats.stations
        ],
    }
    if extra_payload:
        payload.update(extra_payload)
    files = {"xlsx": artifact.xlsx_bytes}
    if artifact.pdf_bytes:
        files["pdf"] = artifact.pdf_bytes
    meta = {
        "day": options.day.isoformat(),
        "hours": json.dumps(sorted(set(int(h) for h in options.hours))),
        "title": artifact.layout.title if artifact.layout else "",
        "filename": artifact.filename_stem,
        "xlsx_size": len(artifact.xlsx_bytes),
        "pdf_size": len(artifact.pdf_bytes),
        "xlsx_sha256": _sha256(artifact.xlsx_bytes),
        "pdf_sha256": _sha256(artifact.pdf_bytes),
        "stations": len(stats.stations),
        "products": stats.total_products,
        "ng": stats.total_ng,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "created_at": datetime.now().isoformat(timespec="milliseconds"),
        "created_by": created_by or "",
    }
    return storage.insert_report(meta, files, links=links or [])


def export_report_to_disk(storage, report_id: int, out_dir: Path, *,
                          progress=None, cancel=None,
                          kind: str = "both") -> tuple[Path | None, Path | None]:
    """把库中存档写回磁盘（原子写 + 进度 + 可取消），返回 (xlsx, pdf) 路径。

    `kind` 可取 `both` / `xlsx` / `pdf`；某一文件不存在时返回 None。
    已完成一个文件不会因另一个失败而回滚，路径随写随回填数据库。
    """

    def tick(phase: str, done: int, total: int, message: str) -> None:
        if progress is not None:
            progress(phase, done, total, message)

    def check() -> None:
        if cancel is not None and cancel():
            raise CancelledError("任务已取消")

    report = storage.get_report(report_id)
    if report is None:
        raise LookupError(f"报告 #{report_id} 不存在")
    target = reports_dir(out_dir)
    stem = report.get("filename") or f"视觉改善分析报告_{report_id}"
    paths: dict[str, Path | None] = {"xlsx": None, "pdf": None}

    for file_kind, label in (("xlsx", "xlsx"), ("pdf", "pdf")):
        if kind != "both" and kind != file_kind:
            continue
        data = storage.load_report_file(report_id, file_kind)
        if not data:
            continue
        suffix = ".xlsx" if file_kind == "xlsx" else ".pdf"
        path = target / f"{stem}{suffix}"
        _atomic_write(path, data, phase=f"save_{file_kind}", label=label,
                      tick=tick, check=check)
        paths[file_kind] = path
        check()
        if file_kind == "xlsx":
            storage.mark_report_saved(report_id, xlsx_path=str(path))
        else:
            storage.mark_report_saved(report_id, pdf_path=str(path))
    return paths["xlsx"], paths["pdf"]


def _atomic_write(path: Path, data: bytes, *, phase: str, label: str,
                  tick, check) -> None:
    """分块写 `.tmp` 并原子改名；取消/失败删除半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    total = len(data)
    written = 0
    try:
        with open(tmp, "wb") as handle:
            while written < total:
                check()
                block = data[written:written + CHUNK]
                handle.write(block)
                written += len(block)
                tick(phase, written, total, f"落盘 {label} {_mib(written)} / {_mib(total)}")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError as exc:
            log.warning("删除临时文件失败 %s：%s", tmp, exc)
        raise


def list_reports(storage, day: str | None = None, limit: int = 100) -> list[dict]:
    """存档列表（含落盘状态），供网页「报告存档」页签。"""
    items = storage.list_reports(day=day, limit=limit)
    for item in items:
        item["saved_xlsx"] = bool(item.get("saved_xlsx_path"))
        item["saved_pdf"] = bool(item.get("saved_pdf_path"))
    return items


def load_report_bytes(storage, report_id: int, kind: str = "xlsx") -> bytes | None:
    """从库读整份文件（下载用，不依赖磁盘）。"""
    return storage.load_report_file(report_id, kind)


def purge_reports(storage, keep_days: int, keep_count: int) -> int:
    """按保留策略清理存档，并删除对应磁盘文件；返回删除条数。"""
    removed = storage.purge_reports(keep_days, keep_count)
    for item in removed:
        for key in ("saved_xlsx_path", "saved_pdf_path"):
            target = item.get(key)
            if not target:
                continue
            try:
                Path(target).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("删除存档文件失败 %s：%s", target, exc)
    return len(removed)
