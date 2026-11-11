"""点云单元格支持：Cognex CPCD 点云（`image/x-cpcd`）的识别、下载与格式解析。

探测结论（In-Sight 3D 相机，固件 26.x，相机 192.168.10.55）：

- 点云单元格（如 A0）在 `HmiResult.cells` 中的取值是占位标记字符串 `❒PointCloud`
  （类比图像单元格的 `❒Image`），本身不含点云数据；
- 实际点云在 `HmiResult.acqPointCloudView.layers[*].graphics` 中的
  `PointCloud3D.url`（形如 `/cam0/pc/000000000000528`），HTTP 下载得到 CPCD 二进制；
- 头部 20 字节（小端）：魔数 `0xB0B15BAD`、保留、头部长度(20)、点数 N、保留，
  其后为 N × 3 个 float32（x, y, z，毫米）。文件长度 = 20 + N × 12。
"""

from __future__ import annotations

import logging
import struct
import urllib.request

log = logging.getLogger(__name__)

# 点云单元格的占位标记（与图像单元格的 ❒Image 同理）
POINTCLOUD_MARKER = "❒PointCloud"
POINTCLOUD_MIME = "image/x-cpcd"
# images.kind：点云在 images 表中的类型标记
POINTCLOUD_KIND = "pointcloud"

CPCD_MAGIC = 0xB0B15BAD
_HEADER = struct.Struct("<5I")  # 魔数、保留、头部长度、点数、保留
_POINT = struct.Struct("<3f")   # x, y, z


def _cell_data(hmi_result: dict, cell_name: str):
    """取指定单元格在 HmiResult.cells 中的 data 字段。"""
    for item in (hmi_result or {}).get("cells") or []:
        if not isinstance(item, dict):
            continue
        if item.get("location") == cell_name or item.get("name") == cell_name:
            return item.get("data")
    return None


def cell_is_pointcloud(hmi_result: dict, cell_name: str) -> bool:
    """该单元格自身是否为点云单元格（值为 ❒PointCloud 占位标记）。"""
    data = _cell_data(hmi_result, cell_name)
    if isinstance(data, str):
        return POINTCLOUD_MARKER in data
    if isinstance(data, dict):
        return any(isinstance(v, str) and POINTCLOUD_MARKER in v
                   for v in (data.get("value"), data.get("data")))
    return False


def pointcloud_source(hmi_result: dict) -> dict | None:
    """从 HmiResult 解析点云下载地址与量程。

    返回 {"url", "min_z", "max_z", "has_greyscale_image"}；无点云视图时返回 None。
    """
    view = (hmi_result or {}).get("acqPointCloudView") or {}
    for layer in view.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        for graphic in layer.get("graphics") or []:
            if not isinstance(graphic, dict):
                continue
            if graphic.get("$type") != "PointCloud3D":
                continue
            url = graphic.get("url")
            if isinstance(url, str) and url:
                return {
                    "url": url,
                    "min_z": graphic.get("minZ"),
                    "max_z": graphic.get("maxZ"),
                    "has_greyscale_image": bool(graphic.get("hasGreyscaleImage")),
                }
    return None


def fetch_pointcloud(host: str, port: int, path: str, timeout: float = 60.0) -> bytes:
    """下载 CPCD 点云原始字节（单帧可达十余 MB，超时放宽）。"""
    url = path if path.startswith("http") else f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        raise ValueError(f"空响应: {path}")
    return data


def parse_cpcd(data: bytes) -> int:
    """校验 CPCD 头部并返回点数；格式不符返回 0（不抛异常，避免拖垮采集）。"""
    if len(data) < _HEADER.size:
        return 0
    magic, _reserved, header_size, count, _reserved2 = _HEADER.unpack_from(data, 0)
    if magic != CPCD_MAGIC:
        log.debug("CPCD 魔数不符: 0x%08X", magic)
        return 0
    if header_size < _HEADER.size or header_size + count * _POINT.size > len(data):
        log.debug("CPCD 长度不符: 头部 %s, 点数 %s, 字节 %s",
                  header_size, count, len(data))
        return 0
    return count
