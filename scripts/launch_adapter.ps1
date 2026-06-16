param(
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Runtime = Join-Path $Root "runtime"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

Set-Location $Root

function Write-LauncherLine {
    param([string]$Message)
    Write-Host "[Gemini Adapter] $Message"
}

function Ensure-LocalFile {
    param(
        [string]$ExampleRelativePath,
        [string]$TargetRelativePath
    )

    $source = Join-Path $Root $ExampleRelativePath
    $target = Join-Path $Root $TargetRelativePath
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $source -Destination $target -Force
        Write-LauncherLine "Created $TargetRelativePath"
    }
}

function Test-AdapterDependencies {
    $code = "import fastapi, sse_starlette, uvicorn, curl_cffi"
    python -c $code *> $null
    return ($LASTEXITCODE -eq 0)
}

function Get-AdapterHealth {
    param([int]$Port)
    $url = "http://127.0.0.1:$Port/health"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $output = & curl.exe --silent --show-error --noproxy "*" --max-time 3 $url 2>$null
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($exitCode -eq 0 -and $output) {
            return ($output | Out-String)
        }
    }

    try {
        Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.UseProxy = $false
        $client = [System.Net.Http.HttpClient]::new($handler)
        $client.Timeout = [TimeSpan]::FromSeconds(3)
        try {
            return $client.GetStringAsync($url).GetAwaiter().GetResult()
        }
        finally {
            $client.Dispose()
            $handler.Dispose()
        }
    }
    catch {
        return $null
    }
}

function Test-PortListening {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return ($null -ne $connection)
}

Ensure-LocalFile "examples\adapter_env.example.ps1" "adapter_env.local.ps1"
Ensure-LocalFile "examples\gemini_cookies.example.json" "gemini_cookies.local.json"

if (-not (Test-AdapterDependencies)) {
    Write-LauncherLine "Missing dependencies; installing..."
    & (Join-Path $ScriptDir "install_adapter_dependencies.bat")
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE"
    }
}

. .\adapter_env.local.ps1

$port = 8000
if ($env:OPENAI_ADAPTER_PORT -and ($env:OPENAI_ADAPTER_PORT -as [int])) {
    $port = [int]$env:OPENAI_ADAPTER_PORT
}

if (-not $env:OPENAI_ADAPTER_SERVER_LOG_PATH) {
    $env:OPENAI_ADAPTER_SERVER_LOG_PATH = Join-Path $Runtime "server.log"
}

$logPath = $env:OPENAI_ADAPTER_SERVER_LOG_PATH
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

$health = Get-AdapterHealth -Port $port
if ($health) {
    Write-LauncherLine "Server already running."
}
else {
    if (Test-PortListening -Port $port) {
        Write-LauncherLine "Port $port is already in use, but /health is not available."
    }
    else {
        Write-LauncherLine "Starting server in background..."
        $runner = Join-Path $ScriptDir "run_server.ps1"
        Start-Process -FilePath powershell.exe `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $runner) `
            -WindowStyle Hidden `
            -WorkingDirectory $Root

        $deadline = (Get-Date).AddSeconds(75)
        do {
            Start-Sleep -Seconds 2
            $health = Get-AdapterHealth -Port $port
        } while (-not $health -and (Get-Date) -lt $deadline)

        if ($health) {
            Write-LauncherLine "Server started."
        }
        else {
            Write-LauncherLine "Server is still starting or failed. Check Terminal Output in the panel."
        }
    }
}

$url = "http://127.0.0.1:$port/"
if (-not $NoOpen) {
    Start-Process $url
    Write-LauncherLine "Opened panel: $url"
}
else {
    Write-LauncherLine "Panel: $url"
}
