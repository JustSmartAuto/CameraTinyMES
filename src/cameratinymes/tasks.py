"""通用后台任务管理器（服务端内存，单线程串行执行）。

设计见 `docs/当日改善报告功能设计.md` §4.9：
- `submit()` 立即返回任务对象，HTTP 线程不做生成/落盘循环；
- 单线程 executor 串行执行，天然避免并发写同名文件；
- 任务带阶段进度、当前动作文案与取消标志；`dedupe_key` 用于「同一报告的重复导出」去重；
- 任务只存内存：保留最近 `keep` 条与 24 小时；服务重启后残留 running 视为 `interrupted`。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

log = logging.getLogger(__name__)

# 任务状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
PARTIAL = "partial"
FAILED = "failed"
CANCELED = "canceled"
INTERRUPTED = "interrupted"

ACTIVE_STATUSES = (PENDING, RUNNING)


class CancelledError(RuntimeError):
    """任务被用户取消（由阶段检查点抛出）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class Task:
    """一个后台任务的内存状态。"""

    task_id: str
    kind: str
    title: str
    dedupe_key: str = ""
    status: str = PENDING
    phase: str = ""
    done: int = 0
    total: int = 0
    percent: float = 0.0
    message: str = ""
    cancelable: bool = True
    result: dict | None = None
    error: str = ""
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    # ---------- 进度 / 取消 ----------

    def progress(self, phase: str = "", done: int | None = None, total: int | None = None,
                 message: str = "") -> None:
        """更新阶段与进度（线程安全：只写不可变原子值）。"""
        if phase:
            self.phase = phase
            if done is None:
                # 换阶段时进度归零，避免沿用上一阶段的百分比
                self.done, self.total, self.percent = 0, 0, 0.0
        if done is not None:
            self.done = int(done)
        if total is not None:
            self.total = int(total)
            if self.total > 0:
                self.percent = min(100.0, self.done / self.total * 100)
            else:
                self.percent = 0.0
        if message:
            self.message = message

    def check(self) -> None:
        """取消检查点：已取消则抛 `CancelledError`。"""
        if self.cancel_event.is_set():
            raise CancelledError("任务已取消")

    def cancel(self) -> bool:
        if self.status not in ACTIVE_STATUSES:
            return False
        self.cancel_event.set()
        self.message = "正在取消…"
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "percent": round(self.percent, 1),
            "message": self.message,
            "cancelable": self.cancelable and self.status in ACTIVE_STATUSES,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskManager:
    """单线程后台任务队列；桌面端与网页端共享同一实例。"""

    def __init__(self, keep: int = 20, max_workers: int = 1):
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="report-task")
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._lock = threading.Lock()
        self._seq = 0
        self._keep = max(4, int(keep))
        self._closed = False

    # ---------- 提交 / 查询 ----------

    def submit(self, kind: str, title: str, func: Callable[[Task], Any], *,
               dedupe_key: str = "") -> Task:
        """投递任务；`func(task)` 在后台线程执行，返回值写入 `task.result`。

        相同的 `dedupe_key` 若已有排队/运行中的任务，直接返回该任务（不重复执行）。
        """
        if dedupe_key:
            existing = self.find_active(dedupe_key)
            if existing is not None:
                return existing
        with self._lock:
            self._seq += 1
            task_id = f"t-{datetime.now():%Y%m%d%H%M%S}-{self._seq:02d}"
            task = Task(task_id=task_id, kind=kind, title=title, dedupe_key=dedupe_key)
            self._tasks[task_id] = task
            self._prune_locked()
        if self._closed:
            task.status = FAILED
            task.error = "服务已关闭"
            task.finished_at = _now()
            return task
        self._executor.submit(self._run, task, func)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, active_only: bool = False, limit: int = 20) -> list[dict]:
        with self._lock:
            tasks = list(self._tasks.values())
        if active_only:
            tasks = [t for t in tasks if t.status in ACTIVE_STATUSES]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.as_dict() for t in tasks[:limit]]

    def find_active(self, dedupe_key: str) -> Task | None:
        with self._lock:
            for task in self._tasks.values():
                if task.dedupe_key == dedupe_key and task.status in ACTIVE_STATUSES:
                    return task
        return None

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        return bool(task and task.cancel())

    # ---------- 生命周期 ----------

    def mark_interrupted(self) -> int:
        """启动时调用：把上次进程残留的排队/运行任务标记为中断。"""
        count = 0
        with self._lock:
            for task in self._tasks.values():
                if task.status in ACTIVE_STATUSES:
                    task.status = INTERRUPTED
                    task.cancelable = False
                    task.error = "服务重启，任务被中断"
                    task.finished_at = _now()
                    count += 1
        return count

    def shutdown(self, wait: bool = False) -> None:
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _run(self, task: Task, func: Callable[[Task], Any]) -> None:
        task.status = RUNNING
        task.started_at = _now()
        try:
            result = func(task)
            if task.cancel_event.is_set():
                task.status = CANCELED
                task.message = "已取消"
                task.result = None
            else:
                data = result if isinstance(result, dict) else {"value": result}
                task.result = data
                # 部分成功（如已入库但落盘失败/取消）由任务函数显式标记
                task.status = PARTIAL if data.get("partial") else DONE
                if task.status == PARTIAL:
                    task.message = data.get("message") or "部分完成"
                else:
                    task.message = task.message or "已完成"
                    task.percent = 100.0
        except CancelledError:
            task.status = CANCELED
            task.message = "已取消"
            log.info("任务 %s 已取消", task.task_id)
        except Exception as exc:                          # 后台任务不能吞掉日志
            task.status = FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            task.message = "失败"
            log.exception("任务 %s 失败", task.task_id)
        finally:
            task.cancelable = False
            task.finished_at = _now()

    def _prune_locked(self) -> None:
        """保留最近 N 条与 24 小时（调用方已持锁）。"""
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="milliseconds")
        finished = [(tid, t) for tid, t in self._tasks.items()
                    if t.status not in ACTIVE_STATUSES]
        for tid, task in finished:
            if task.created_at < cutoff:
                self._tasks.pop(tid, None)
        overflow = len(self._tasks) - self._keep
        if overflow <= 0:
            return
        for tid, task in list(self._tasks.items()):
            if overflow <= 0:
                break
            if task.status not in ACTIVE_STATUSES:
                self._tasks.pop(tid, None)
                overflow -= 1
