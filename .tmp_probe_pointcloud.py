"""探测 192.168.10.55 相机 A0 单元格（pointcloud）的 CogSocket 数据结构。"""
import asyncio
import json
import sys

sys.path.insert(0, "src")
from cameratinymes.cogsocket import CogSocketClient

HOST, PORT = "192.168.10.55", 80
OUT = {}


def dump(obj, limit=4000):
    s = json.dumps(obj, ensure_ascii=False, indent=1)
    return s[:limit] + ("...[truncated]" if len(s) > limit else "")


def summarize_view(rec, name):
    if not isinstance(rec, dict):
        return
    info = {"key": name, "$type": rec.get("$type"), "id": rec.get("id"),
            "url": rec.get("url"), "source": rec.get("source"),
            "viewport": rec.get("viewport")}
    layers = []
    for ly in rec.get("layers") or []:
        layers.append({"$type": ly.get("$type"), "url": ly.get("url"),
                       "contentType": ly.get("contentType"),
                       "width": ly.get("width"), "height": ly.get("height"),
                       "image": {k: ly.get(k) for k in ("width", "height", "bitsPerPixel",
                                                        "isColor", "imageFormat", "url")}
                       if isinstance(ly.get("image"), dict) else None})
    info["layers"] = layers
    OUT.setdefault("views", []).append(info)


async def main():
    c = CogSocketClient(HOST, PORT, "admin", "")
    await c.connect(timeout=8)
    try:
        # 根路径探测：cam0/hmi/info 返回 None/报错时改试 system/info
        root = c.root
        try:
            info = await c.get(f"{root}/info")
        except Exception as exc:
            info = None
            OUT["info_error_v3"] = repr(exc)
        if not info:
            try:
                info = await c.get("system/info")
                if info:
                    root = "system"
            except Exception as exc:
                OUT["info_error_legacy"] = repr(exc)
        OUT["root"] = root
        OUT["info_raw"] = info
        if isinstance(info, dict):
            info = {k: info.get(k) for k in
                    ("model", "name", "ipAddress", "serial", "firmwareVersion",
                     "hmiProtocolVersion", "acq", "capabilities", "jobExtension")}
        else:
            info = {}
        OUT["info"] = info
        sid = await c.post(f"{root}/openSession",
                           {"$type": "HmiSessionInfo", "cellNames": ["A0"]})
        c.session_id = sid
        try:
            await c.post(f"{sid}/login", ["admin", "", False])
        except Exception:
            pass
        OUT["sid"] = sid

        # GET 最新结果，看 A0 原始结构与 views
        try:
            result = await c.get(f"{sid}/result")
        except Exception as exc:
            result = None
            OUT["get_result_error"] = repr(exc)
        if isinstance(result, dict):
            OUT["result_keys"] = list(result.keys())
            cells = result.get("cells") or []
            OUT["cells_raw"] = cells
            for key in ("acqImageView", "acqPointCloudView"):
                rec = result.get(key)
                OUT[key] = "null" if rec is None else dump(rec, 2500)
                summarize_view(rec, key)
            for k, v in (result.get("views") or {}).items():
                summarize_view(v, f"views[{k}]")
        else:
            OUT["result_raw"] = dump(result, 2000)

        # 订阅 resultChanged 收一帧事件
        got = asyncio.Event()

        def on_result(hmi_result):
            if got.is_set():
                return
            if isinstance(hmi_result, dict):
                OUT["event_keys"] = list(hmi_result.keys())
                for key in ("acqImageView", "acqPointCloudView"):
                    rec = hmi_result.get(key)
                    OUT[f"event_{key}"] = "null" if rec is None else dump(rec, 4000)
                    summarize_view(rec, key)
                for k, v in (hmi_result.get("views") or {}).items():
                    summarize_view(v, f"event_views[{k}]")
            got.set()

        await c.listen(f"{sid}/resultChanged", on_result)
        await c.ready()
        try:
            await asyncio.wait_for(got.wait(), timeout=15)
        except asyncio.TimeoutError:
            OUT["event"] = "no event in 15s"

        # queryCellResults 查询 A0
        try:
            qr = await c.post(f"{sid}/queryCellResults", [["A0"]])
            OUT["queryCellResults"] = qr
        except Exception as exc:
            OUT["queryCellResults_error"] = repr(exc)

        # 所有单元格名（看 A0 叫什么 / 有无其他 pointcloud 相关单元格）
        try:
            OUT["all_cell_names"] = await c.post(f"{sid}/getAllCellNames", None)
        except Exception as exc:
            OUT["all_cell_names_error"] = repr(exc)

        # HTTP 下载各视图 layer URL，看返回什么内容
        import urllib.request
        for view in OUT.get("views") or []:
            for ly in view.get("layers") or []:
                url = ly.get("url")
                if not url:
                    continue
                full = f"http://{HOST}:{PORT}{url}"
                try:
                    req = urllib.request.Request(full, headers={"Accept": "*/*"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        head = resp.read(256)
                        OUT.setdefault("http_probe", []).append({
                            "url": url, "status": resp.status,
                            "content_type": resp.headers.get("Content-Type"),
                            "content_length": resp.headers.get("Content-Length"),
                            "head_bytes_hex": head[:64].hex(),
                            "head_text": head[:120].decode("utf-8", "replace"),
                        })
                except Exception as exc:
                    OUT.setdefault("http_probe", []).append(
                        {"url": url, "error": repr(exc)})
    finally:
        await c.close()

    print(json.dumps(OUT, ensure_ascii=False, indent=1, default=str))


asyncio.run(main())
