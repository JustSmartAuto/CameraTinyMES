"""模拟 In-Sight 相机 WebSocket 服务器（测试用）。

实现 CogSocket 最小子集：@/hello、openSession、login、ready、keepAlive、
queryCellResults、resultChanged 周期推送、dispose；
同时在同一端口提供 /cam0/img/{序号} 图像（?render=1 返回带叠加的渲染图）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from http import HTTPStatus
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

log = logging.getLogger(__name__)


def _cell(location: str, name: str, value, result_type: str = "HmiFloatResult") -> dict:
    return {"$type": result_type, "location": location, "name": name, "data": value}


def _resp(msg_id, body=None, error=None) -> str:
    msg = {"$type": "resp", "id": msg_id}
    if error is not None:
        msg["error"] = error
    if body is not None:
        msg["body"] = body
    return json.dumps(msg)


class SimulatedCamera:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, interval: float = 0.5):
        self.host = host
        self.port = port
        self.interval = interval
        self._server = None
        self._seq = 0
        self._png_cache: dict[tuple[str, bool], bytes] = {}

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, self.host, self.port,
                                              process_request=self._serve_image)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------- 图像（同一端口 HTTP） ----------

    def _png(self, seq: str, render: bool) -> bytes:
        key = (seq, render)
        cached = self._png_cache.get(key)
        if cached:
            return cached
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (640, 480), (28, 30, 34))
        draw = ImageDraw.Draw(img)
        for y in range(0, 480, 24):
            draw.line([(0, y), (640, y)], fill=(42, 46, 52), width=1)
        cx, cy = 320, 240
        w = 180 + int(40 * math.sin(len(seq) + int(seq or 0) % 7))
        draw.rectangle([cx - w // 2, cy - 90, cx + w // 2, cy + 90],
                       outline=(180, 180, 180), width=2)
        draw.line([(cx - 40, cy), (cx + 40, cy)], fill=(200, 200, 200), width=2)
        draw.line([(cx, cy - 40), (cx, cy + 40)], fill=(200, 200, 200), width=2)
        draw.text((16, 16), f"SIM CAM  SEQ={seq}", fill=(230, 230, 230))
        if render:
            draw.rectangle([cx - w // 2, cy - 90, cx + w // 2, cy + 90],
                           outline=(0, 200, 120), width=3)
            draw.text((16, 40), "PASS", fill=(0, 210, 120))
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        if len(self._png_cache) > 200:
            self._png_cache.clear()
        self._png_cache[key] = data
        return data

    async def _serve_image(self, connection, request):
        parsed = urlparse(request.path)
        if parsed.path.startswith("/cam0/img/"):
            seq = parsed.path.rsplit("/", 1)[-1]
            render = parse_qs(parsed.query).get("render", ["0"])[0] in ("1", "true")
            try:
                data = self._png(seq, render)
            except Exception as exc:
                log.warning("sim image failed: %s", exc)
                return connection.respond(HTTPStatus.INTERNAL_SERVER_ERROR, "image error")
            headers = Headers([
                ("Content-Type", "image/png"),
                ("Content-Length", str(len(data))),
                ("Cache-Control", "no-store"),
            ])
            return Response(HTTPStatus.OK, "OK", headers, data)
        if parsed.path.endswith("/graphics"):
            data = json.dumps(self._graphics()).encode()
            headers = Headers([
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(data))),
            ])
            return Response(HTTPStatus.OK, "OK", headers, data)
        return None

    def _graphics(self) -> list:
        """模拟 GraphicsLayer 图形（坐标与 640×480 图像同尺度）。"""
        cx, cy, w = 320, 240, 200
        return [
            {"$type": "Text", "color": -13447886, "text": f"模拟检测 SEQ={self._seq}",
             "fontSize": 12, "x": 16, "y": 44},
            {"$type": "Rectangle", "color": -65536, "x": cx - w // 2, "y": cy - 90,
             "w": w, "h": 180, "lineThickness": 2},
            {"$type": "Circle", "color": -16711936, "x": cx, "y": cy, "radius": 110,
             "lineThickness": 2},
            {"$type": "Line", "color": -16728065, "x0": cx - 60, "y0": cy,
             "x1": cx + 60, "y1": cy, "lineThickness": 2},
        ]

    def _result(self) -> dict:
        self._seq += 1
        t = self._seq * self.interval
        width = 10.0 + 0.5 * math.sin(t / 3.0) + random.gauss(0, 0.08)
        score = max(0.0, min(100.0, 95 + random.gauss(0, 3)))
        img_url = f"/cam0/img/{self._seq}"
        return {
            "$type": "HmiResult",
            "id": self._seq,
            "jobStatus": 1,
            # 主采集视图：ImageLayer=原始图，GraphicsLayer=检测图形（用于合成渲染图）
            "acqImageView": {
                "$type": "ViewRecord",
                "id": self._seq,
                "url": f"/cam0/app/views/{self._seq}",
                "layers": [
                    {"$type": "ImageLayer", "url": img_url, "width": 640, "height": 480,
                     "image": {"$type": "Image", "url": img_url, "width": 640, "height": 480}},
                    {"$type": "GraphicsLayer",
                     "url": f"/cam0/app/views/{self._seq}/layers/2/graphics"},
                ],
            },
            "cells": [
                _cell("A0", "AcquireImage", img_url, "HmiImageResult"),
                _cell("A1", "Measure.Width", round(width, 4)),
                _cell("A2", "Measure.Score", round(score, 2)),
            ],
        }

    async def _handle(self, ws) -> None:
        session_id = None
        pushing = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("$type")
                path = msg.get("path", "")
                msg_id = msg.get("id", 0)
                body = msg.get("body")

                if path == "@/hello":
                    await ws.send(_resp(msg_id))
                elif path.endswith("hmi/info") or path.endswith("system/info"):
                    await ws.send(_resp(msg_id, body={
                        "model": "SIM-7800", "ipAddress": self.host,
                        "firmwareVersion": "26.1.0", "hmiProtocolVersion": 3.0,
                    }))
                elif path.endswith("hmi/openSession"):
                    session_id = f"hs/~{random.randint(10**9, 10**10)}"
                    await ws.send(_resp(msg_id, body=session_id))
                elif session_id and path == f"{session_id}/login":
                    await ws.send(_resp(msg_id, body="full"))
                elif session_id and path == f"{session_id}/ready":
                    await ws.send(_resp(msg_id))
                    if pushing is None:
                        pushing = asyncio.create_task(self._push_loop(ws, session_id))
                elif session_id and path == f"{session_id}/keepAlive":
                    await ws.send(_resp(msg_id))
                elif session_id and path == f"{session_id}/dispose":
                    if pushing:
                        pushing.cancel()
                        pushing = None
                    await ws.send(_resp(msg_id))
                elif session_id and path == f"{session_id}/queryCellResults":
                    result = self._result()
                    wanted = []
                    for group in body or []:
                        wanted.extend(group if isinstance(group, list) else [group])
                    matches = [
                        c for c in result["cells"]
                        if c["location"] in wanted or c["name"] in wanted
                    ]
                    await ws.send(_resp(msg_id, body=matches))
                elif session_id and path == f"{session_id}/resultChanged" and mtype == "listen":
                    await ws.send(_resp(msg_id))
                elif session_id and path == f"{session_id}/resultChanged" and mtype == "unlisten":
                    await ws.send(_resp(msg_id))
                else:
                    await ws.send(_resp(msg_id, error=-1, body=f"unknown path {path}"))
        except websockets.ConnectionClosed:
            pass
        finally:
            if pushing:
                pushing.cancel()

    async def _push_loop(self, ws, session_id: str) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await ws.send(json.dumps({
                "$type": "event",
                "path": f"{session_id}/resultChanged",
                "body": self._result(),
            }))
