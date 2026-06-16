param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Model = "gemini-3-pro"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

Write-Step "Health"
$health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get
Assert-True ($health.status -eq "ok") "Health check failed."
Write-Host "health: $($health.status), gemini_client: $($health.gemini_client)"
if ($null -ne $health.account_status) {
    Write-Host "account_status: $($health.account_status.name), authenticated: $($health.account_status.authenticated)"
}
if ($null -ne $health.cookie_writeback) {
    Write-Host "cookie_writeback: enabled=$($health.cookie_writeback.enabled), interval_seconds=$($health.cookie_writeback.interval_seconds)"
}

Write-Step "Models"
$models = Invoke-RestMethod -Uri "$BaseUrl/v1/models" -Method Get
Assert-True ($models.data.Count -gt 0) "No models returned."
Write-Host "models: $($models.data.Count)"

Write-Step "Non-streaming chat completion"
$body = @{
    model = $Model
    stream = $false
    messages = @(
        @{ role = "user"; content = "Reply in one short sentence: adapter non-stream ok." }
    )
} | ConvertTo-Json -Depth 10

$response = Invoke-RestMethod `
    -Uri "${BaseUrl}/v1/chat/completions" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body

$text = $response.choices[0].message.content
Assert-True ([string]::IsNullOrWhiteSpace($text) -eq $false) "Non-streaming response was empty."
Write-Host "response: $text"

Write-Step "Streaming chat completion"
$streamBody = @{
    model = $Model
    stream = $true
    messages = @(
        @{ role = "user"; content = "Reply in one short sentence: adapter stream ok." }
    )
} | ConvertTo-Json -Depth 10

$bodyFile = Join-Path $env:TEMP ("adapter_stream_body_{0}.json" -f ([guid]::NewGuid().ToString("N")))
try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($bodyFile, $streamBody, $utf8NoBom)

    $streamOutput = & curl.exe `
        --silent `
        --show-error `
        --no-buffer `
        -H "Content-Type: application/json" `
        --data-binary "@$bodyFile" `
        "${BaseUrl}/v1/chat/completions" 2>&1

    $curlExitCode = $LASTEXITCODE
    $streamText = ($streamOutput | Out-String)
    Assert-True ($curlExitCode -eq 0) "curl streaming request failed with exit code ${curlExitCode}: $streamText"
    Assert-True ([string]::IsNullOrWhiteSpace($streamText) -eq $false) "Streaming response was empty."
    Assert-True ($streamText -match "data:") "Streaming response did not contain SSE data lines. Raw output: $streamText"
    Assert-True ($streamText -match "data: \[DONE\]") "Streaming response did not end with [DONE]. Raw output: $streamText"
    Write-Host "stream: received SSE data and [DONE]"
}
finally {
    Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue
}

Write-Step "Usage"
$usage = Invoke-RestMethod -Uri "$BaseUrl/usage?limit=5" -Method Get
Write-Host "total_requests: $($usage.totals.requests)"
Write-Host "usage_page: $BaseUrl/usage.html"

Write-Host ""
Write-Host "Adapter OpenAI compatibility smoke test passed."
