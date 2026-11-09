[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [string]$Command,

    [string]$ScriptFile,

    [int]$Timeout = 30,

    [string]$WorkDir,

    [switch]$Health
)

$ErrorActionPreference = 'Stop'

$base = $Url.Trim().TrimEnd('/')
if ($base -notmatch '^https?://') { $base = "http://$base" }

$exitCode = 0
try {
    if ($Health) {
        $healthResp = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 10
        $healthResp | ConvertTo-Json -Compress
        if ($healthResp.status -ne 'running') { $exitCode = 1 }
    }
    else {
        if ($ScriptFile) {
            $Command = Get-Content -LiteralPath $ScriptFile -Raw -Encoding UTF8
        }
        if (-not $Command) {
            throw "必须提供 -Command 或 -ScriptFile，或使用 -Health"
        }

        $payload = @{ command = $Command; timeout = $Timeout }
        if ($WorkDir) { $payload['work_dir'] = $WorkDir }

        $json = $payload | ConvertTo-Json -Depth 5 -Compress
        $body = [System.Text.Encoding]::UTF8.GetBytes($json)

        $resp = Invoke-RestMethod -Uri "$base/exec" -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body $body -TimeoutSec ($Timeout + 15)

        if ($resp.stdout) { [Console]::Out.Write($resp.stdout) }
        if ($resp.stderr) { [Console]::Error.WriteLine($resp.stderr) }
        if ($resp.timeout) { [Console]::Error.WriteLine("[remote] 命令执行超时（>${Timeout}秒）") }

        if ($null -ne $resp.exit_code) { $exitCode = [int]$resp.exit_code }
    }
}
catch {
    [Console]::Error.WriteLine("[http-shell] $($_.Exception.Message)")
    $exitCode = 1
}

exit $exitCode
