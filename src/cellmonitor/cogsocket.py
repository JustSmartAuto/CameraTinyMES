"""CogSocket 客户端：WebSocket + JSON-RPC（GET/POST/PUT/DELETE + 事件订阅）。

实现 HMI 会话流程：connect → openSession → login → listen(resultChanged) → ready → keepAlive。
单相机掉线自动重连，互不影响（每台相机由独立 CameraWorker 驱动）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable

import websockets

log = logging.getLogger(__name__)

ROOT_V3 = "cam0/hmi"
ROOT_LEGACY = "system"
ERROR_ACCESS_DENIED = -1610612728

# HmiResult.cells 数组中各结果类型共用的取值字段顺序
_RESULT_VALUE_FIELDS = ("value", "data")


class CogSocketError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class CogSocketClient:
    """异步 CogSocket 客户端（单连接）。"""

    def __init__(self, host: str, port: int, username: str = "admin", password: str = ""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.root = ROOT_V3
        self.ws: websockets.ClientConnection | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: dict[str, list[Callable]] = {}
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self.session_id: str | None = None

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def connect(self, timeout: float = 10.0) -> None:
        self.ws = await websockets.connect(
            f"ws://{self.host}:{self.port}/ws", open_timeout=timeout
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._hello()
        except CogSocketError as exc:
            # 旧固件可能不支持 @/hello 握手，不致命
            log.warning("%s:%s hello unsupported: %s", self.host, self.port, exc)
        self.root = await self._detect_root()
        log.info("%s:%s root path = %s", self.host, self.port, self.root)

    async def _hello(self) -> None:
        await self._request("post", "@/hello", {"name": "CellMonitor", "model": "CellMonitor"})

    async def _detect_root(self) -> str:
        try:
            await self._request("get", f"{ROOT_V3}/info")
            return ROOT_V3
        except CogSocketError:
            await self._request("get", f"{ROOT_LEGACY}/info")
            return ROOT_LEGACY

    async def get(self, path: str):
        return await self._request("get", path)

    async def post(self, path: str, body=None):
        return await self._request("post", path, body)

    async def put(self, path: str, body=None):
        return await self._request("put", path, body)

    async def listen(self, path: str, callback: Callable) -> None:
        await self._request("listen", path)
        self._listeners.setdefault(path, []).append(callback)

    async def unlisten(self, path: str) -> None:
        await self._request("unlisten", path)
        self._listeners.pop(path, None)

    async def open_session(self, cell_names: list[str]) -> str:
        info = {"$type": "HmiSessionInfo", "cellNames": cell_names}
        sid = await self.post(f"{self.root}/openSession", info)
        self.session_id = sid
        # 登录失败不致命：部分相机允许匿名只读
        try:
            await self.post(f"{sid}/login", [self.username, self.password, False])
        except CogSocketError as exc:
            log.warning("%s login failed: %s", self.host, exc)
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        return sid

    async def ready(self) -> None:
        if self.session_id:
            await self.post(f"{self.session_id}/ready", "")

    async def close(self) -> None:
        for task in (self._keepalive_task, self._reader_task):
            if task:
                task.cancel()
        self._keepalive_task = self._reader_task = None
        if self.ws:
            try:
                if self.session_id:
                    await self.post(f"{self.session_id}/dispose", None)
            except Exception:
                pass
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.session_id = None
        for fut in self._pending.values():
            fut.cancel()
        self._pending.clear()
        self._listeners.clear()

    async def _keepalive_loop(self, interval: float = 15.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if self.session_id:
                    await self.post(f"{self.session_id}/keepAlive", "")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("keepAlive failed: %s", exc)

    async def _request(self, mtype: str, path: str, body=None, want_response: bool = True):
        if not self.ws:
            raise CogSocketError(-1, "not connected")
        msg_id = next(self._ids)
        msg: dict = {"$type": mtype, "id": msg_id, "path": path}
        if body is not None:
            msg["body"] = body
        future: asyncio.Future | None = None
        if want_response:
            future = asyncio.get_running_loop().create_future()
            self._pending[msg_id] = future
        await self.ws.send(json.dumps(msg))
        if future is None:
            return None
        try:
            resp = await future
        finally:
            self._pending.pop(msg_id, None)
        if isinstance(resp, dict) and "error" in resp:
            raise CogSocketError(resp["error"], str(resp.get("body", "")))
        if isinstance(resp, dict):
            return resp.get("body")
        return resp

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                mtype = msg.get("$type")
                if mtype == "resp":
                    fut = self._pending.get(msg.get("id"))
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CogSocketError(msg["error"], str(msg.get("body", ""))))
                        else:
                            fut.set_result(msg.get("body"))
                elif mtype == "event":
                    path = msg.get("path", "")
                    for cb in self._listeners.get(path, []):
                        try:
                            cb(msg.get("body"))
                        except Exception:
                            log.exception("listener error on %s", path)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("read loop ended: %s", exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CogSocketError(-1, "connection closed"))
            self._pending.clear()


def extract_cell_value(cell: dict):
    """从 HmiResult.cells 元素中取值。"""
    for key in _RESULT_VALUE_FIELDS:
        if key in cell:
            return cell[key]
    # 部分类型只有 name/data 之外的展示字段，退回整个对象
    return cell.get("data", cell.get("value"))


def parse_cells(hmi_result: dict) -> dict[str, object]:
    """HmiResult → {location/name: value} 映射。"""
    out = {}
    for cell in hmi_result.get("cells", []) or []:
        loc = cell.get("location") or cell.get("name")
        if loc is None:
            continue
        out[loc] = extract_cell_value(cell)
        name = cell.get("name")
        if name and name != loc:
            out[name] = out[loc]
    return out
