$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outDir = Join-Path $root "dist"
$stage = Join-Path $outDir "gemini-openai-adapter-sync"
$zipPath = Join-Path $outDir "gemini-openai-adapter-sync-$stamp.zip"

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$files = @(
    "openai_adapter_server.py",
    "start_ai_server.bat",
    "open_usage_dashboard.bat",
    "install_adapter_dependencies.bat",
    "adapter_env.example.ps1",
    "gemini_cookies.example.json",
    "COMPANY_SYNC_README.md",
    "export_company_sync_pack.ps1"
)

foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination (Join-Path $stage $file) -Force
}

$usageSyncDir = Join-Path $stage "usage-sync"
New-Item -ItemType Directory -Force -Path $usageSyncDir | Out-Null
Copy-Item -LiteralPath (Join-Path $root "usage-sync\.gitkeep") -Destination (Join-Path $usageSyncDir ".gitkeep") -Force

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath)
Write-Host "Created sync pack:"
Write-Host $zipPath
