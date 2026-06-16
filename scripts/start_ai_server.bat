@echo off
title Local AI Codex Server
echo Starting Gemini Proxy Server...
cd /d "%~dp0\.."
if not exist "adapter_env.local.ps1" (
  echo Missing adapter_env.local.ps1
  echo Copy examples\adapter_env.example.ps1 to adapter_env.local.ps1 first.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\adapter_env.local.ps1; python .\openai_adapter_server.py"
pause
