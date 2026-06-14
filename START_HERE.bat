@echo off
chcp 65001 >nul
title Gemini OpenAI Adapter
cd /d "%~dp0"
python "%~dp0team_menu.py"
if errorlevel 1 (
  echo.
  echo Python 启动失败。请先安装 Python 3.10+，然后重新运行本文件。
  pause
)
