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
if (Test-Path ".\adapter_env.local.ps1") {
    . .\adapter_env.local.ps1
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Get-ConfiguredBrowser {
    $browser = if ($env:OPENAI_ADAPTER_COOKIE_BROWSER) { $env:OPENAI_ADAPTER_COOKIE_BROWSER } else { "chrome" }
    $browser = $browser.Trim().ToLowerInvariant()
    if ($browser -eq "auto") {
        return "chrome"
    }
    if ($browser -in @("chrome", "google-chrome")) {
        return "chrome"
    }
    if ($browser -in @("edge", "msedge", "microsoft-edge")) {
        return "edge"
    }
    throw "Unsupported OPENAI_ADAPTER_COOKIE_BROWSER=$browser. Use chrome or edge."
}

function Find-ChromiumBrowser {
    param([string]$Browser)

    if ($Browser -eq "chrome") {
        $exeName = "chrome.exe"
        $candidates = @(
            (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
            (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
        )
    }
    else {
        $exeName = "msedge.exe"
        $candidates = @(
            (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
            (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
            (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
        )
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    $cmd = Get-Command $exeName -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "$exeName not found."
}

$browser = Get-ConfiguredBrowser
$processName = if ($browser -eq "chrome") { "chrome" } else { "msedge" }
$browserName = if ($browser -eq "chrome") { "Chrome" } else { "Edge" }

if ($browser -eq "chrome") {
    $chromeUserDataDir = if ($env:OPENAI_ADAPTER_CHROME_USER_DATA_DIR) {
        $env:OPENAI_ADAPTER_CHROME_USER_DATA_DIR
    }
    else {
        Join-Path $Root ".chrome-gemini-profile"
    }
    New-Item -ItemType Directory -Force -Path $chromeUserDataDir | Out-Null

    Write-Step "Close dedicated $browserName profile"
    $escapedUserDataDir = [regex]::Escape($chromeUserDataDir)
    Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
        Where-Object { $_.CommandLine -match $escapedUserDataDir } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
else {
    Write-Step "Close $browserName"
    Get-Process $processName -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3
}

Write-Step "Start $browserName with remote debugging"
$browserExe = Find-ChromiumBrowser $browser
$profile = if ($env:OPENAI_ADAPTER_COOKIE_PROFILE) { $env:OPENAI_ADAPTER_COOKIE_PROFILE } else { "Default" }
$browserArgs = @(
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=$DebugPort",
    "--profile-directory=$profile"
)
if ($browser -eq "chrome") {
    $browserArgs += "--user-data-dir=$chromeUserDataDir"
}
$browserArgs += "https://gemini.google.com"
Start-Process -FilePath $browserExe -ArgumentList $browserArgs
Write-Host "Started $browserName on CDP port $DebugPort with profile $profile"
if ($browser -eq "chrome") {
    Write-Host "Chrome user data dir: $chromeUserDataDir"
}

Write-Step "Wait for $browserName CDP"
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
    throw "$browserName CDP did not become ready on port $DebugPort."
}

Write-Step "Capture cookies from $browserName CDP"
$env:NO_PROXY = "localhost,127.0.0.1,::1"
$env:no_proxy = "localhost,127.0.0.1,::1"
python .\capture_browser_gemini_cookies_cdp.py "http://127.0.0.1:$DebugPort" ".\gemini_cookies.local.json" 300
if ($LASTEXITCODE -ne 0) {
    throw "Could not capture Gemini auth cookies from $browserName CDP."
}

Write-Step "Repair adapter and test"
$repairArgs = @{
    SkipBrowserRefresh = $true
    Model = $Model
    ChatRetries = $ChatRetries
    RetryDelaySeconds = $RetryDelaySeconds
}
if ($SkipChatTest) {
    $repairArgs["SkipChatTest"] = $true
}
& .\repair_adapter_auth_and_test.ps1 @repairArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
