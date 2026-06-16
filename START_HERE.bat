@echo off
chcp 65001 >nul
title Gemini OpenAI Adapter
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_adapter.ps1"
if errorlevel 1 (
  echo.
  echo Start failed. Check Python 3.10+ and runtime\server.log.
  pause
)
