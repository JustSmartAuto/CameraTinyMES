"""仅启动改善报告网页工作台，不启动采集主程序（便于其他工控机调试）。

默认连接中心数据库 192.168.10.64:8306 的 MariaDB，监听 0.0.0.0:18110，
可在浏览器访问 http://<本机IP>:18110/report 。

用法：
    uv run python web_only.py
    uv run python web_only.py --port 18120 --token abc
    uv run python web_only.py --db-host 192.168.10.64 --db-user justsmart
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    import cameratinymes  # noqa: F401
except ImportError:  # 未安装为包时直接使用源码目录
    sys.path.insert(0, str(ROOT / "src"))

from cameratinymes import web
from cameratinymes.config import (
    DatabaseConfig, MariaDBConfig, default_data_dir, load_config, load_settings,
)
from cameratinymes.storage import open_storage

DEFAULT_DB_HOST = "192.168.10.64"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="仅启动改善报告网页工作台（不启动采集主程序）")
    parser.add_argument("--db-host", default=DEFAULT_DB_HOST, help=f"MariaDB 主机（默认 {DEFAULT_DB_HOST}）")
    parser.add_argument("--db-port", type=int, default=8306, help="MariaDB 端口（默认 8306）")
    parser.add_argument("--db-user", default="justsmart", help="数据库用户")
    parser.add_argument("--db-password", default="justsmart", help="数据库密码")
    parser.add_argument("--db-name", default="camera_tiny_mes", help="数据库名")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="数据目录（默认 D:/JustSmart/CameraTinyMES）")
    parser.add_argument("--host", default=None, help="监听地址（默认读取 settings.json）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认读取 settings.json）")
    parser.add_argument("--token", default=None, help="访问口令（默认读取 settings.json）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir = args.data_dir or default_data_dir()
    print(f"数据目录: {data_dir}")

    settings = load_settings(data_dir)
    if args.host is not None:
        settings.web_host = args.host
    if args.port is not None:
        settings.web_port = args.port
    if args.token is not None:
        settings.web_token = args.token

    cameras = load_config(data_dir)
    if not cameras:
        print("警告：未找到 config/cameras.json，网页可浏览已有报告，但无法按工站生成新报告。")

    cfg = DatabaseConfig(
        backend="mariadb",
        mariadb=MariaDBConfig(host=args.db_host, port=args.db_port, user=args.db_user,
                              password=args.db_password, database=args.db_name),
    )
    print(f"连接数据库: {args.db_user}@{args.db_host}:{args.db_port}/{args.db_name}")
    try:
        storage = open_storage(data_dir, cfg)
    except Exception as exc:
        print(f"数据库连接失败：{exc}", file=sys.stderr)
        return 1

    server = web.ReportServer(storage, cameras, settings, data_dir)
    try:
        port = server.start()
    except Exception as exc:
        print(f"网页服务启动失败：{exc}", file=sys.stderr)
        storage.close()
        return 1

    urls = server.urls()
    suffix = f"&k={server.token}" if server.token else ""
    print("改善报告工作台已启动（采集主程序未启动）")
    print(f"  本机访问:   {urls['local']}{suffix}")
    if urls["lan"]:
        print(f"  局域网访问: {urls['lan']}{suffix}")
    print("  按 Ctrl+C 停止")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.stop()
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
