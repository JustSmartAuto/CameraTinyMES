"""应用入口。

用法：
    camera-tiny-mes [<options>]

--simulate：启动内置模拟相机并把其加入配置，便于离线体验完整流程。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from cameratinymes.config import (
    CameraConfig, CellConfig, default_data_dir,
    load_config, load_settings, save_config,
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="camera-tiny-mes", description=__doc__)
    parser.add_argument("--simulate", action="store_true", help="启动模拟相机并加入配置")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认 D:/JustSmart/CameraTinyMES）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    # 单文件 exe 打包后，渲染子进程用同一 exe 带此标志重启
    if "--renderer-host" in argv:
        from cameratinymes.renderer import host_main

        host_main()
        return
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir = args.data_dir or default_data_dir()
    print(f"数据目录: {data_dir}")

    cameras = load_config(data_dir)
    settings = load_settings(data_dir)

    # settings.autostart_enabled 为 True 但系统侧无快捷方式 → 尝试补建
    if settings.autostart_enabled:
        try:
            from cameratinymes import autostart
            if not autostart.is_enabled():
                autostart.set(True, log_fn=lambda m: print(f"[自启] {m}"))
        except Exception as exc:
            print(f"[自启] 补建失败: {exc}")

    extra_startup = []
    if args.simulate:
        from cameratinymes.simulator import SimulatedCamera

        sim = SimulatedCamera(port=8087)
        extra_startup.append(sim.start())
        print("模拟相机: ws://127.0.0.1:8087/ws")
        if not any(c.name == "模拟相机" for c in cameras):
            cameras.append(CameraConfig(
                name="模拟相机", host="127.0.0.1", port=8087,
                cells=[
                    CellConfig(name="A0", data_type="image", record_mode="simple",
                               note="采集图/渲染图"),
                    CellConfig(name="A1", sample_mode="event", lower=9.5, upper=10.5, unit="mm"),
                    CellConfig(name="Measure.Score", sample_mode="poll", poll_interval=1.0,
                               lower=90),
                ],
            ))
            save_config(data_dir, cameras)

    from cameratinymes.ui import MonitorApp

    app = MonitorApp(data_dir, cameras, settings=settings, extra_startup=extra_startup)
    app.run()


if __name__ == "__main__":
    main()
