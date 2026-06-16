$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
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
    "LICENSE",
    "pyproject.toml",
    "openai_adapter_server.py",
    "START_HERE.bat",
    "START_HERE.command",
    ".clinerules",
    ".clineignore",
    ".gitattributes",
    ".gitignore"
)

foreach ($file in $files) {
    $source = Join-Path $root $file
    $destination = Join-Path $stage $file
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination (Join-Path $stage "README.md") -Force
Copy-Item -LiteralPath (Join-Path $root "scripts") -Destination $stage -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root "docs") -Destination $stage -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root "examples") -Destination $stage -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root "tests") -Destination $stage -Recurse -Force

$srcDir = Join-Path $stage "src"
New-Item -ItemType Directory -Force -Path $srcDir | Out-Null
Copy-Item -LiteralPath (Join-Path $root "src\gemini_webapi") -Destination $srcDir -Recurse -Force

$usageSyncDir = Join-Path $stage "usage-sync"
New-Item -ItemType Directory -Force -Path $usageSyncDir | Out-Null
Copy-Item -LiteralPath (Join-Path $root "usage-sync\.gitkeep") -Destination (Join-Path $usageSyncDir ".gitkeep") -Force
Copy-Item -LiteralPath (Join-Path $root "usage-sync\README.md") -Destination (Join-Path $usageSyncDir "README.md") -Force

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath)
Write-Host "Created sync pack:"
Write-Host $zipPath
