param(
    [int]$DebugPort = 9222,
    [string]$Model = "gemini-3-flash",
    [int]$ChatRetries = 2,
    [int]$RetryDelaySeconds = 30,
    [switch]$SkipChatTest
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Find-Edge {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    $cmd = Get-Command msedge.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "msedge.exe not found."
}

Write-Step "Close Edge"
Get-Process msedge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3

Write-Step "Start Edge with remote debugging"
$edge = Find-Edge
$edgeArgs = @(
    "--remote-debugging-port=$DebugPort",
    "--profile-directory=Default",
    "https://gemini.google.com"
)
Start-Process -FilePath $edge -ArgumentList $edgeArgs
Write-Host "Started Edge on CDP port $DebugPort"

Write-Step "Wait for Edge CDP"
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Seconds 2
    try {
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:$DebugPort/json/version" -TimeoutSec 3
        Write-Host "CDP ready: $($version.Browser)"
        break
    }
    catch {
        $version = $null
    }
} while ((Get-Date) -lt $deadline)

if ($null -eq $version) {
    throw "Edge CDP did not become ready on port $DebugPort."
}

Write-Step "Capture cookies from Edge CDP"
python .\capture_edge_gemini_cookies_cdp.py "http://127.0.0.1:$DebugPort" ".\gemini_cookies.local.json"
if ($LASTEXITCODE -ne 0) {
    throw "Could not capture Gemini auth cookies from Edge CDP."
}

Write-Step "Repair adapter and test"
$repairArgs = @(
    "-SkipBrowserRefresh",
    "-Model", $Model,
    "-ChatRetries", $ChatRetries,
    "-RetryDelaySeconds", $RetryDelaySeconds
)
if ($SkipChatTest) {
    $repairArgs += "-SkipChatTest"
}
& .\repair_adapter_auth_and_test.ps1 @repairArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
