"""通过本机代理执行 git push.

GitHub 直连会超时，脚本按以下顺序取代理并传给 git：
    1. 命令行 --proxy
    2. 环境变量 HTTPS_PROXY / HTTP_PROXY
    3. Windows 系统代理（注册表 Internet Settings）

用法：
    python git_push.py                 # 推送当前分支
    python git_push.py origin main     # 透传任意 git push 参数
    python git_push.py --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _normalize(proxy: str) -> str:
    """补全协议头，并处理 'http=host:port;https=host:port' 这种系统写法."""
    proxy = proxy.strip()
    if "=" in proxy:
        parts = dict(
            item.split("=", 1) for item in proxy.split(";") if "=" in item
        )
        proxy = parts.get("https") or parts.get("http") or ""
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return proxy


def _system_proxy() -> str:
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    return _normalize(server) if enabled else ""


def resolve_proxy(cli_proxy: str | None) -> str:
    if cli_proxy:
        return _normalize(cli_proxy)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name)
        if value:
            return _normalize(value)
    return _system_proxy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过本机代理执行 git push")
    parser.add_argument("--proxy", help="代理地址，如 http://127.0.0.1:7993")
    args, rest = parser.parse_known_args(argv)

    proxy = resolve_proxy(args.proxy)
    cmd = ["git"]
    if proxy:
        cmd += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
        print(f"代理: {proxy}", flush=True)
    else:
        print("未找到代理，按直连执行", flush=True)
    cmd += ["push", *rest]

    print("执行:", " ".join(cmd), flush=True)
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print(f"无法执行 git: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
