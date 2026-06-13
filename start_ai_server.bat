@echo off
title Local AI Codex Server
echo Starting Gemini Proxy Server...
cd /d "D:\GeminiAPI\Gemini-API"
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\adapter_env.local.ps1; python openai_adapter_server.py"
pause
