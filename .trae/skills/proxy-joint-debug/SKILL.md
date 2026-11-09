---
name: proxy-joint-debug
description: Debug InternetProxy between this machine and a remote industrial PC via the HTTP Shell skill, including deployment, v2ray+wintun startup, clock and DNS. Use when asked to 联调 proxy or fix remote internet. Not for SSH.
---

# 本机 ↔ 工控机 Proxy 联调

本机做服务端（网关），远程工控机做客户端（v2ray + wintun 透明代理）。远程操作统一走 `http-shell-ipc-powershell` 技能，不要用 SSH。

## 0. 约定与前提

- 本机（服务端）示例：`192.168.10.209`，VMess `11081`，发现页 `18081`，nginx `18080`。
- 工控机（客户端）示例：`192.168.10.64`，HTTP Shell `10022`，程序目录 `D:\JustSmart\InternetProxyPy`。
- 两端时钟必须在 90 秒内，否则 VMess 报 `invalid user`。
- 远程 Shell 是 Medium 权限：改时间、改 DNS、建 wintun 网卡、启动 exe 都要 UAC 提权；任务计划 `/RL HIGHEST` 会 `Access is denied`，只能用 `Start-Process -Verb RunAs`。
- InternetProxyPy 客户端自带同款内嵌 Shell（默认 `10022`，配置项 `remote_shell` 可关），服务端 exe 没起时 `$Rps` 照样能直连；它随软件以管理员身份运行，改时间/网卡不需要再弹 UAC。

项目内路径：

```powershell
$Root = "d:\JustStupid\httpshell远程操作工控机_2605201359\InternetProxyPy"
$Rps  = "$Root\.trae\skills\http-shell-ipc-powershell\scripts\Invoke-RemotePowerShell.ps1"
$Diag = "$Root\.trae\skills\proxy-joint-debug\scripts\Remote-ProxyDiagnose.ps1"
$Elev = "$Root\.trae\skills\proxy-joint-debug\scripts\Remote-Elevated.ps1"
```

## 1. 本机服务端自检

```powershell
curl.exe -sS -m 10 -o NUL -w "INTERNET=%{http_code}`n" https://www.baidu.com
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object LocalPort -in 11080,11081,18080,18081 |
  Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
Invoke-RestMethod http://127.0.0.1:18081/v2ray.json
Get-Content D:/JustSmart/InternetProxyPy/installer.json -Raw
Get-Content D:/JustSmart/InternetProxyPy/v2ray/config.json -Raw
```

判定：

- `11081` 必须监听，`v2ray.json` 的 uuid 必须等于服务端 `config.json` 里 `vmess-in.clients[].id`。
- 服务端本身必须能直接上网，否则客户端一定不通。
- v2ray 模式下本机 **10082 不监听是正常的**（客户端才用 10082）。

## 2. 远程到本机的链路

```powershell
& $Rps -Url "http://192.168.10.64:10022" -Timeout 60 -Command '"[11081]=" + (Test-NetConnection 192.168.10.209 -Port 11081 -InformationLevel Quiet); "[18081]=" + (Test-NetConnection 192.168.10.209 -Port 18081 -InformationLevel Quiet); (Invoke-WebRequest http://192.168.10.209:18081/v2ray.json -UseBasicParsing -TimeoutSec 5).Content'
```

两项都为 `True` 且能取到 uuid，才进入部署。

## 3. 部署与配置客户端

上传最新构建（exe 名带时间戳）：

```powershell
$pkg = (Get-ChildItem "$Root\dist\InternetProxy_*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
curl.exe -sS -X POST "http://192.168.10.64:10022/upload" -F "file=@$pkg" -F "path=D:/JustSmart/InternetProxyPy/"
```

写 `D:\JustSmart\InternetProxyPy\config.json`（UTF-8 **无 BOM**，BOM 会导致 JSON 解析失败）：

```json
{
  "role": "client",
  "gateway": "192.168.10.209",
  "port": 10082,
  "proxy_mode": "rule",
  "transparent": true,
  "v2ray_enabled": true,
  "v2ray_server": "192.168.10.209",
  "v2ray_port": 11081,
  "v2ray_uuid": "<服务端 uuid>",
  "v2ray_network": "tcp",
  "v2ray_path": "/vmess",
  "v2ray_tls": false,
  "autostart": false
}
```

用 `[IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))` 落盘。客户端启动后会自己生成 `v2ray/client.json`（SOCKS 入站监听 10082），一般不要手改。

## 4. 提权启动

exe 是 `requireAdministrator`，且 wintun 网卡必须管理员权限。远程 Shell 造不了计划任务，只能在工控机控制台弹 UAC：

1. 在工控机准备好提权脚本，内容依次为：从 `http://<服务端>:18081/` 的 `Date` 响应头校时 → `Start-Process "<exe>" -ArgumentList "--role","client","--headless"` → 写日志。
2. 用本技能脚本触发并等待：

```powershell
& $Rps -Url "http://192.168.10.64:10022" -Timeout 180 -Command '& "D:\JustSmart\InternetProxyPy\Remote-Elevated.ps1" -ScriptFile "D:\JustSmart\InternetProxyPy\elevated_launch.ps1" -LogFile "D:\JustSmart\InternetProxyPy\elevated.log"'
```

- UAC 弹窗出现在工控机控制台（session 1），需要有人点“是”。
- exe 是 windowed 构建，**没有控制台输出**，只能靠端口、网卡、路由和 `v2ray/client-error.log` 判断。
- 提权脚本应在启动 exe 后自行退出，否则 `Remote-Elevated.ps1` 会一直等。

## 5. 远程诊断

上传并执行诊断脚本，一次输出全部关键状态：

```powershell
curl.exe -sS -X POST "http://192.168.10.64:10022/upload" -F "file=@$Diag" -F "path=D:/JustSmart/InternetProxyPy/"
& $Rps -Url "http://192.168.10.64:10022" -Timeout 180 -Command '& "D:\JustSmart\InternetProxyPy\Remote-ProxyDiagnose.ps1" -ServerIp 192.168.10.209'
```

输出为 `KEY=VALUE`：进程、网卡、`198.18.0.1`、`0.0.0.0/1` 与 `128.0.0.0/1` 路由、DNS、10082、服务端端口、解析、HTTP/HTTPS。

## 6. 故障树

| 现象 | 根因 | 处理 |
|---|---|---|
| 服务端 access.log 出现 `invalid user > Not Found` | 两端时钟差超过 90 秒 | 客户端启动时会自动按服务端 `Date` 校时；若仍出现，手动把系统时间调成服务端时间 |
| `Could not resolve host` 或 DNS 超时 | 系统 DNS 不可用（常见于原 LAN DNS 已停） | 透明模式会自动给 `InternetProxyPy` 网卡设置 `223.5.5.5`/`8.8.8.8`；被封锁时改 `tun.py` 的 `TUN_DNS` |
| 没有 `InternetProxyPy` 网卡 / 自动回退 SOCKS5 | 未提权或 wintun 驱动未装 | UAC 提权运行 exe |
| 10082 不监听 | v2ray 未启动、uuid 错或端口被占 | 程序会先检测占用并直接报错；看 `v2ray/client-error.log` 与 `logs/app.log` |
| HTTPS 报 `schannel 0x80092013` | 证书吊销列表离线 | 临时加 `--ssl-no-revoke`；修好 DNS/路由后再试 |
| 11081 连不通 | 本机防火墙或 v2ray 未起 | 放行端口，确认服务端进程 |
| ping 不通 | 不支持 ICMP | 正常，用 TCP/HTTP 验证 |

## 7. 验收标准

- 客户端进程以管理员运行，`InternetProxyPy` 网卡 Up、`198.18.0.1/24`。
- `0.0.0.0/1`、`128.0.0.0/1` 指向 `198.18.0.1`，10082 由客户端 v2ray 子进程监听。
- `Resolve-DnsName www.baidu.com` 有结果，HTTP 与 HTTPS 均返回 200。
- 服务端 access.log 不再出现 `invalid user`。

## 8. 收尾

- 停客户端前先确认没有其它 v2ray 占用 10082；工控机上遗留的服务端式 v2ray（11080/11081）要单独确认并停掉。
- `autostart=false` 时重启不会自动运行，需要常驻再开自启。
- DNS 与时钟是持久化改动，先记录原值以便回滚。
- `10022`、`18081` 无鉴权，仅限内网使用。
