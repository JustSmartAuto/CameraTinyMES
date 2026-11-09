[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ScriptFile,

    [string]$LogFile = '',

    [int]$TimeoutSec = 120
)

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ScriptFile)) {
    'ELEVATION_FAILED=script not found: ' + $ScriptFile
    exit 2
}

if ($LogFile) {
    Remove-Item -LiteralPath $LogFile -Force -ErrorAction SilentlyContinue
}

try {
    $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ScriptFile)
} catch {
    'ELEVATION_FAILED=' + $_.Exception.Message
    exit 1
}

if (-not $proc) {
    'ELEVATION_FAILED=no elevated process'
    exit 1
}

'ELEVATED_PID=' + $proc.Id
$proc | Wait-Process -Timeout $TimeoutSec -ErrorAction SilentlyContinue

if ($LogFile -and (Test-Path -LiteralPath $LogFile)) {
    'LOG='
    Get-Content -LiteralPath $LogFile -Encoding UTF8
}
