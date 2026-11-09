"""诊断 192.168.10.123 相机：连接 → 订阅 resultChanged → 看 HmiResult 里图像相关字段。"""
from __future__ import annotations
import sys, asyncio, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cameratinymes.cogsocket import CogSocketClient
from cameratinymes.images import resolve_sources, image_source_size

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")


async def main():
    HOST, PORT = "192.168.10.123", 80
    cells = ["A0"]
    print(f"\n=== 连接 {HOST}:{PORT}，订阅 cells={cells} ===")

    client = CogSocketClient(HOST, PORT, "admin", "")
    await client.connect()
    print(f"✓ connected")

    sid = await client.open_session(cells)
    print(f"✓ session sid={sid}")

    got = {"count": 0}

    def dump(hmi):
        got["count"] += 1
        if got["count"] > 3:
            return
        print(f"\n--- HmiResult #{got['count']} ---")
        print("keys:", list(hmi.keys())[:20])

        # cells 部分
        cs = hmi.get("cells") or []
        print(f"cells 数量: {len(cs)}")
        for c in cs:
            loc = c.get("location") or c.get("name") or "?"
            data = c.get("data")
            if isinstance(data, str):
                print(f"  cell[{loc}]: str len={len(data)} preview={data[:120]}")
            elif isinstance(data, dict):
                print(f"  cell[{loc}]: dict keys={list(data.keys())}")
            else:
                print(f"  cell[{loc}]: {type(data).__name__} = {str(data)[:80]}")

        # acqImageView
        acq = hmi.get("acqImageView")
        if acq:
            print(f"acqImageView: YES  keys={list(acq.keys())[:10]}")
            layers = acq.get("layers") or []
            print(f"  layers 数量: {len(layers)}")
            for layer in layers:
                if isinstance(layer, dict):
                    t = layer.get("$type") or layer.get("type") or "?"
                    url = layer.get("url", "")
                    img_info = ""
                    im = layer.get("image")
                    if isinstance(im, dict):
                        img_info = f" image={im.get('width')}x{im.get('height')}"
                    print(f"    layer $type={t} url={str(url)[:80]}{img_info}")
                else:
                    print(f"    layer: {type(layer).__name__}")
        else:
            print("acqImageView: MISSING !!!")

        # resolve_sources 结果
        try:
            sources = resolve_sources(hmi, "A0")
            size = image_source_size(hmi)
            print(f"resolve_sources('A0') → {sources}")
            print(f"image_source_size → {size}")
        except Exception as exc:
            print(f"resolve_sources 抛异常: {exc}")

    await client.listen(f"{sid}/resultChanged", dump)
    await client.ready()
    print("\n等待 resultChanged 事件（10 秒）...")
    await asyncio.sleep(10)

    await client.close()
    print(f"\n共收到 {got['count']} 条 resultChanged")


if __name__ == "__main__":
    asyncio.run(main())
