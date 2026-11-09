"""AV1 帧序列压缩：把连续 A0 图像打成 IVF 分片后入库。

相机连续拍摄的画面变化很小，按 GOP 分片做帧间预测，压缩率远高于逐张 JPEG。
每个分片独立可解码，片内保留 30 帧或空闲 5 秒即封片。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# 默认编码参数（可按需调整）
SEGMENT_FRAMES = 30          # 每片最多帧数（同时是 GOP 长度）
SEGMENT_IDLE_SECONDS = 5.0   # 空闲多久后封片
AV1_CRF = 32
AV1_PRESET = 10
FLUSH_INTERVAL = 0.5         # 后台巡检查间隔


def _installer_candidates():
    """离线安装器的默认落点：%LOCALAPPDATA%\\ffmpeg 与 %ProgramFiles%\\ffmpeg。"""
    roots = []
    for env in ("LOCALAPPDATA", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "ffmpeg")
    for root in roots:
        yield root / "bin" / "ffmpeg.exe"
        if root.is_dir():
            for sub in sorted(root.iterdir(), reverse=True):
                if sub.is_dir():
                    yield sub / "bin" / "ffmpeg.exe"


def _registry_path_entries():
    """进程环境可能早于安装器启动，补读注册表里的用户 / 系统 PATH。"""
    if sys.platform != "win32":
        return []
    import winreg

    entries: list[str] = []
    for hive, key in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as reg:
                value, _ = winreg.QueryValueEx(reg, "PATH")
            entries.extend(os.path.expandvars(value).split(os.pathsep))
        except OSError:
            pass
    return entries


def find_ffmpeg() -> str | None:
    """定位 ffmpeg：环境变量 → PATH → 安装器默认目录 → 注册表 PATH。"""
    custom = os.environ.get("CAMERATINYMES_FFMPEG")
    if custom and Path(custom).is_file():
        return custom
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in _installer_candidates():
        if path.is_file():
            return str(path)
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for entry in _registry_path_entries():
        entry = entry.strip().strip('"')
        if entry and (Path(entry) / exe).is_file():
            return str(Path(entry) / exe)
    return None


FFMPEG = find_ffmpeg()
if FFMPEG:
    log.info("AV1 编码器 ffmpeg: %s", FFMPEG)


def available() -> bool:
    return bool(FFMPEG)


def encode_frames(jpegs: list[bytes]) -> bytes | None:
    """JPEG 帧序列 → AV1(IVF) 码流；失败返回 None。"""
    if not FFMPEG or not jpegs:
        return None
    with tempfile.TemporaryDirectory(prefix="ctm_av1_") as tmp:
        root = Path(tmp)
        for i, data in enumerate(jpegs, start=1):
            (root / f"f_{i:06d}.jpg").write_bytes(data)
        out = root / "out.ivf"
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", "25", "-start_number", "1",
            "-i", str(root / "f_%06d.jpg"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libsvtav1", "-preset", str(AV1_PRESET), "-crf", str(AV1_CRF),
            "-g", str(max(1, len(jpegs))), "-keyint_min", "1",
            "-pix_fmt", "yuv420p", "-an", "-f", "ivf", str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.warning("AV1 编码失败: %s", proc.stderr.strip()[:400])
            return None
        data = out.read_bytes()
        return data or None


def decode_stream(data: bytes) -> list[bytes]:
    """AV1(IVF) 码流 → JPEG 帧列表；失败返回空列表。"""
    if not FFMPEG or not data:
        return []
    with tempfile.TemporaryDirectory(prefix="ctm_av1d_") as tmp:
        root = Path(tmp)
        src = root / "in.ivf"
        src.write_bytes(data)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-fps_mode", "passthrough", "-an",
            "-f", "image2", "-c:v", "mjpeg", "-q:v", "2",
            str(root / "o_%06d.jpg"),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.warning("AV1 解码失败: %s", proc.stderr.strip()[:400])
            return []
        return [p.read_bytes() for p in sorted(root.glob("o_*.jpg"))]


def load_images(storage, camera: str, cell: str, kind: str | None = None,
                start: datetime | None = None, end: datetime | None = None):
    """合并旧版单帧 images 与 AV1 分片，返回 [(ts, kind, mime, w, h, jpeg)]。"""
    rows = list(storage.query_images(camera, cell, kind=kind, start=start, end=end))
    frames = storage.query_stream_frames(camera, cell, kind=kind, start=start, end=end)
    if frames:
        cache: dict[int, list[bytes]] = {}
        last_index: dict[int, int] = {}
        for _ts, _kind, sid, idx, _w, _h in frames:
            last_index[sid] = max(last_index.get(sid, 0), idx)
        for ts, frame_kind, sid, idx, width, height in frames:
            if sid not in cache:
                blob = storage.get_image_stream(sid)
                cache[sid] = decode_stream(blob) if blob else []
            decoded = cache[sid]
            if 0 <= idx < len(decoded):
                rows.append((ts, frame_kind, "image/jpeg", width, height, decoded[idx]))
            else:
                log.warning("分片 %s 缺少第 %s 帧，已跳过", sid, idx)
            if idx >= last_index.get(sid, 0):
                cache.pop(sid, None)
    rows.sort(key=lambda r: r[0])
    return rows


class ImageStreamWriter:
    """按 相机+单元格+类型 缓冲 JPEG 帧，满片或空闲时异步编码入库。"""

    def __init__(self, storage, enabled: bool = True):
        self.storage = storage
        self.enabled = bool(enabled and FFMPEG)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._active = 0            # 正在编码的分片数（关闭时等待归零）
        self._buffers: dict[tuple[str, str, str, str], list[tuple]] = {}
        self._ready: list[tuple[tuple[str, str, str, str], list[tuple]]] = []
        self._last_add: dict[tuple[str, str, str, str], float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if not self.enabled:
            log.warning("未找到 ffmpeg，AV1 帧序列压缩不可用，回退为 JPEG 单帧入库")
        else:
            self._thread = threading.Thread(target=self._loop, name="av1-writer", daemon=True)
            self._thread.start()

    # ---------- 对外接口 ----------

    def add_frame(self, ts: datetime, camera: str, cell: str, kind: str,
                  data: bytes, width: int | None, height: int | None,
                  job: str | None = None) -> bool:
        """投递一帧；返回 False 表示编码不可用，调用方应回退 JPEG 入库。

        作业名（jobx 配方）进缓冲键：换配方后帧自然落到新分片，一个分片不混两种配方。
        """
        if not self.enabled:
            return False
        key = (camera, cell, kind, job or "")
        with self._lock:
            buf = self._buffers.setdefault(key, [])
            if buf and (buf[0][2], buf[0][3]) != (width, height):
                # 尺寸变化（配置改动）：旧片立即封口
                self._ready.append((key, self._buffers.pop(key)))
                buf = []
                self._buffers[key] = buf
            buf.append((ts, data, width, height))
            self._last_add[key] = time.monotonic()
            if len(buf) >= SEGMENT_FRAMES:
                self._ready.append((key, self._buffers.pop(key)))
        return True

    def flush_camera(self, camera: str) -> None:
        """相机断开时把该相机所有未封片缓冲立即编码。"""
        self._take_and_encode(lambda key: key[0] == camera)

    def flush_all(self) -> None:
        """把所有未封片缓冲立即编码（停机/关窗时调用）。"""
        self._take_and_encode(lambda key: True)

    def close(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.flush_all()
        # 等其它线程（相机断开触发的封片）编码完成，避免紧接着关闭数据库
        with self._cond:
            self._cond.wait_for(lambda: self._active == 0, timeout=30)

    # ---------- 内部 ----------

    def _loop(self) -> None:
        while not self._stop.wait(FLUSH_INTERVAL):
            with self._lock:
                ready, self._ready = self._ready, []
                now = time.monotonic()
                for key, last in list(self._last_add.items()):
                    if now - last >= SEGMENT_IDLE_SECONDS and key in self._buffers:
                        ready.append((key, self._buffers.pop(key)))
                        self._last_add.pop(key, None)
            for key, frames in ready:
                self._encode(key, frames)

    def _take_and_encode(self, match) -> None:
        with self._lock:
            ready, self._ready = self._ready, []
            for key in list(self._buffers):
                if match(key):
                    ready.append((key, self._buffers.pop(key)))
                    self._last_add.pop(key, None)
        for key, frames in ready:
            self._encode(key, frames)

    def _encode(self, key: tuple[str, str, str, str], frames: list[tuple]) -> None:
        if not frames:
            return
        with self._cond:
            self._active += 1
        try:
            self._encode_inner(key, frames)
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def _encode_inner(self, key: tuple[str, str, str, str], frames: list[tuple]) -> None:
        data = encode_frames([f[1] for f in frames])
        if data is None:
            self._fallback(key, frames)
            return
        try:
            self.storage.insert_image_stream(
                key[0], key[1], key[2],
                [(f[0], f[2], f[3]) for f in frames], data,
                job=key[3] or None)
        except Exception:
            log.exception("AV1 分片入库失败，回退 JPEG 单帧")
            self._fallback(key, frames)

    def _fallback(self, key: tuple[str, str, str, str], frames: list[tuple]) -> None:
        for ts, data, _w, _h in frames:
            try:
                self.storage.insert_image(ts, key[0], key[1], key[2], data, "image/jpeg",
                                          job=key[3] or None)
            except Exception:
                log.exception("JPEG 回退入库失败")
