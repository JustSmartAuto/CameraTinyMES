[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [int]$VmessPort = 11081,

    [int]$DiscoveryPort = 18081,

    [int]$SocksPort = 10082,

    [string]$TestUrl = 'www.baidu.com'
)

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'

'TIME=' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')

$apps = Get-Process -Name 'InternetProxy_*' | ForEach-Object { "$($_.Id)/s$($_.SessionId)" }
'APP=' + ($apps -join ',')

$v2 = Get-Process -Name 'v2ray' | ForEach-Object { $_.Id }
'V2RAY=' + ($v2 -join ',')

$adapter = Get-NetAdapter | Where-Object { $_.Name -like '*InternetProxy*' } | Select-Object -First 1
if ($adapter) {
    'ADAPTER=' + $adapter.Name + '/' + $adapter.Status
} else {
    'ADAPTER=none'
}

$tunIp = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '198.18.*' } | ForEach-Object { $_.IPAddress }
'TUN_IP=' + ($tunIp -join ',')

$r0 = Get-NetRoute -AddressFamily IPv4 | Where-Object { $_.DestinationPrefix -eq '0.0.0.0/1' } | ForEach-Object { $_.NextHop + '/' + $_.InterfaceAlias }
'ROUTE_0=' + ($r0 -join ',')

$r128 = Get-NetRoute -AddressFamily IPv4 | Where-Object { $_.DestinationPrefix -eq '128.0.0.0/1' } | ForEach-Object { $_.NextHop + '/' + $_.InterfaceAlias }
'ROUTE_128=' + ($r128 -join ',')

$dns = Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | ForEach-Object { $_.InterfaceAlias + '=' + ($_.ServerAddresses -join '&') }
'DNS=' + ($dns -join ';')

$socks = Get-NetTCPConnection -LocalPort $SocksPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($socks) {
    "SOCKS_${SocksPort}=pid=$($socks.OwningProcess)"
} else {
    "SOCKS_${SocksPort}=none"
}

"SERVER_${VmessPort}=" + (Test-NetConnection $ServerIp -Port $VmessPort -InformationLevel Quiet)
"SERVER_${DiscoveryPort}=" + (Test-NetConnection $ServerIp -Port $DiscoveryPort -InformationLevel Quiet)

$resolved = Resolve-DnsName $TestUrl -Type A | Where-Object { $_.IPAddress } | Select-Object -First 1
if ($resolved) { 'RESOLVE=' + $resolved.IPAddress } else { 'RESOLVE=fail' }

'HTTP=' + (curl.exe -sS -m 15 -o NUL -w '%{http_code}' http://www.msftconnecttest.com/connecttest.txt 2>$null)
'HTTPS=' + (curl.exe -sS -m 20 -o NUL -w '%{http_code}' "https://$TestUrl" 2>$null)
