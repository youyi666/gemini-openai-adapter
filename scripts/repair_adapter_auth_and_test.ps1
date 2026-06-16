param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Model = "gemini-3-flash",
    [int]$HealthTimeoutSeconds = 60,
    [int]$ChatRetries = 3,
    [int]$RetryDelaySeconds = 30,
    [int]$ChatTimeoutSeconds = 90,
    [switch]$SkipBrowserRefresh,
    [switch]$SkipChatTest
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Stop-Adapter {
    $processes = Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
        Where-Object { $_.CommandLine -match 'openai_adapter_server\.py' }

    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force
        Write-Host "Stopped adapter process $($process.ProcessId)"
    }
}

function Start-Adapter {
    $command = 'Set-Location "{0}"; . .\adapter_env.local.ps1; python .\openai_adapter_server.py' -f $Root
    Start-Process `
        -WindowStyle Hidden `
        -WorkingDirectory $Root `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command)
    Write-Host "Started adapter process"
}

function Get-Health {
    Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 10
}

Write-Step "Load local environment"
. .\adapter_env.local.ps1
if (-not $env:GEMINI_COOKIE_PATH) {
    $env:GEMINI_COOKIE_PATH = Join-Path $Root "runtime\gemini_cookie_cache"
}
Write-Host "Cookie JSON: $env:GEMINI_COOKIES_JSON"
Write-Host "Cookie cache: $env:GEMINI_COOKIE_PATH"

if (-not $SkipBrowserRefresh) {
    Write-Step "Refresh cookies from browser if possible"
    python .\scripts\refresh_gemini_cookies_from_browser.py $env:GEMINI_COOKIES_JSON
    $refreshExit = $LASTEXITCODE
    if ($refreshExit -eq 0) {
        Write-Host "Browser cookie refresh succeeded."
    }
    elseif ($refreshExit -eq 2) {
        Write-Host "Browser cookie refresh unavailable; using existing gemini_cookies.local.json."
    }
    else {
        throw "Browser cookie refresh failed with exit code $refreshExit."
    }
}

Write-Step "Reset project cookie cache"
Remove-Item -LiteralPath $env:GEMINI_COOKIE_PATH -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $env:GEMINI_COOKIE_PATH | Out-Null

Write-Step "Restart adapter"
Stop-Adapter
Start-Sleep -Seconds 2
Start-Adapter

Write-Step "Wait for authenticated health"
$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$lastHealth = $null
do {
    Start-Sleep -Seconds 2
    try {
        $lastHealth = Get-Health
        $status = $lastHealth.account_status
        Write-Host ("health: {0}, account={1}, authenticated={2}" -f `
            $lastHealth.status, $status.name, $status.authenticated)
        if ($status.authenticated -eq $true) {
            break
        }
    }
    catch {
        Write-Host "health not ready: $($_.Exception.Message)"
    }
} while ((Get-Date) -lt $deadline)

if ($null -eq $lastHealth -or $lastHealth.account_status.authenticated -ne $true) {
    throw "Adapter did not become authenticated. Last health: $($lastHealth | ConvertTo-Json -Depth 5)"
}

Write-Step "Check models"
$models = Invoke-RestMethod -Uri "$BaseUrl/v1/models" -Method Get -TimeoutSec 20
Write-Host "models: $($models.data.Count)"

if (-not $SkipChatTest) {
    Write-Step "Run minimal chat test"
    $body = @{
        model = $Model
        stream = $false
        messages = @(
            @{ role = "user"; content = "Reply with exactly: adapter test ok" }
        )
    } | ConvertTo-Json -Depth 10

    $success = $false
    for ($attempt = 1; $attempt -le $ChatRetries; $attempt++) {
        try {
            Write-Host "chat attempt $attempt/$ChatRetries using $Model"
            $response = Invoke-RestMethod `
                -Uri "$BaseUrl/v1/chat/completions" `
                -Method Post `
                -ContentType "application/json" `
                -Body $body `
                -TimeoutSec $ChatTimeoutSeconds
            $text = $response.choices[0].message.content
            if ([string]::IsNullOrWhiteSpace($text)) {
                throw "Chat response was empty."
            }
            Write-Host "chat response: $text"
            $success = $true
            break
        }
        catch {
            Write-Host "chat attempt failed: $($_.Exception.Message)"
            if ($attempt -lt $ChatRetries) {
                Write-Host "waiting $RetryDelaySeconds seconds before retry..."
                Start-Sleep -Seconds $RetryDelaySeconds
            }
        }
    }

    if (-not $success) {
        throw "Health is authenticated, but chat test did not pass. This is usually upstream 429/rate limiting."
    }
}

Write-Step "Done"
Write-Host "Adapter auth repair and test completed successfully."
