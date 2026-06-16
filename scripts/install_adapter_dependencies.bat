@echo off
title Install Gemini Adapter Dependencies
cd /d "%~dp0\.."
echo Installing Gemini OpenAI Adapter and browser cookie helpers...
python -m pip install -e ".[browser]"
if errorlevel 1 (
  echo.
  echo Install failed. Check Python and pip, then run this script again.
  pause
  exit /b 1
)
echo.
echo Done. Copy examples\adapter_env.example.ps1 to adapter_env.local.ps1 and examples\gemini_cookies.example.json to gemini_cookies.local.json, then start the server.
pause
