@echo off
title Install Gemini Adapter Dependencies
cd /d "%~dp0"
echo Installing Gemini-API package and OpenAI adapter dependencies...
python -m pip install -e .[browser]
python -m pip install fastapi uvicorn sse-starlette
echo.
echo Done. Copy adapter_env.example.ps1 to adapter_env.local.ps1 and gemini_cookies.example.json to gemini_cookies.local.json, then fill in your Gemini cookies.
pause
