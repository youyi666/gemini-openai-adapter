$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Runtime = Join-Path $Root "runtime"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

Set-Location $Root

if (-not (Test-Path -LiteralPath ".\adapter_env.local.ps1")) {
    throw "Missing adapter_env.local.ps1"
}

. .\adapter_env.local.ps1
. (Join-Path $ScriptDir "adapter_proxy.ps1")

if (-not $env:OPENAI_ADAPTER_SERVER_LOG_PATH) {
    $env:OPENAI_ADAPTER_SERVER_LOG_PATH = Join-Path $Runtime "server.log"
}

$logPath = $env:OPENAI_ADAPTER_SERVER_LOG_PATH
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
Set-AdapterProxyDefaults
$proxyForLog = if ($env:GEMINI_PROXY) { Format-AdapterProxyForLog $env:GEMINI_PROXY } else { "not set" }
[System.IO.File]::AppendAllText(
    $logPath,
    "`r`n=== Adapter server started at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===`r`n[Gemini Adapter] GEMINI_PROXY=$proxyForLog`r`n",
    $utf8NoBom
)

$python = (Get-Command python -ErrorAction Stop).Source
$command = "`"$python`" .\openai_adapter_server.py >> `"$logPath`" 2>>&1"
cmd.exe /d /c $command
