#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small teammate-facing launcher for Gemini OpenAI Adapter."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
POWERSHELL = shutil.which("powershell") or "powershell"
HEALTH_URL = "http://127.0.0.1:8000/health"
DASHBOARD_URL = "http://127.0.0.1:8000/"

REQUIRED_MODULES = (
    "fastapi",
    "sse_starlette",
    "uvicorn",
    "pydantic",
    "curl_cffi",
)


def pause() -> None:
    input("\n按 Enter 返回菜单...")


def print_header() -> None:
    print("=" * 64)
    print("Gemini OpenAI Adapter 团队启动菜单")
    print("=" * 64)
    print(f"项目目录: {ROOT}")
    print()


def run_command(command: list[str] | str, *, title: str) -> int:
    print()
    print(f"== {title} ==")
    if isinstance(command, list):
        print(" ".join(command))
    else:
        print(command)
    print()
    try:
        if isinstance(command, list):
            return subprocess.run(command, cwd=ROOT).returncode
        return subprocess.run(command, cwd=ROOT, shell=True).returncode
    except FileNotFoundError as exc:
        print(f"命令不存在: {exc}")
        return 1


def powershell(command: str, *, title: str) -> int:
    return run_command(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        title=title,
    )


def open_path(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError:
        print(f"无法打开: {path}")


def module_status() -> tuple[bool, list[str]]:
    missing = [
        name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None
    ]
    return (not missing), missing


def local_file_status() -> dict[str, bool]:
    return {
        "adapter_env.local.ps1": (ROOT / "adapter_env.local.ps1").exists(),
        "gemini_cookies.local.json": (ROOT / "gemini_cookies.local.json").exists(),
    }


def get_health(timeout: float = 3.0) -> tuple[bool, dict[str, object] | str]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return True, payload
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return False, str(exc)


def show_status() -> None:
    print_header()
    print(f"Python: {sys.version.split()[0]}")

    deps_ok, missing = module_status()
    if deps_ok:
        print("依赖: 已安装")
    else:
        print("依赖: 缺失 " + ", ".join(missing))

    files = local_file_status()
    for name, exists in files.items():
        print(f"{name}: {'已存在' if exists else '未创建'}")

    ok, health = get_health()
    if ok and isinstance(health, dict):
        account = health.get("account_status") or {}
        if isinstance(account, dict):
            account_name = account.get("name", "UNKNOWN")
            authenticated = account.get("authenticated", False)
        else:
            account_name = "UNKNOWN"
            authenticated = False
        print(f"服务: 已运行，账号状态 {account_name}，authenticated={authenticated}")
        budget = health.get("prompt_budget") or {}
        if isinstance(budget, dict) and budget.get("max_prompt_tokens"):
            print(f"Prompt 上限: {budget.get('max_prompt_tokens')} tokens")
    else:
        print("服务: 未运行或无法访问")
        print(f"原因: {health}")


def init_local_files() -> None:
    examples = (
        ("adapter_env.example.ps1", "adapter_env.local.ps1"),
        ("gemini_cookies.example.json", "gemini_cookies.local.json"),
    )
    for source_name, target_name in examples:
        source = ROOT / source_name
        target = ROOT / target_name
        if target.exists():
            print(f"跳过: {target_name} 已存在")
            continue
        shutil.copyfile(source, target)
        print(f"已创建: {target_name}")

    print()
    print("下一步：")
    print("1. 先确认浏览器里的 Gemini 网页可以正常发送消息。")
    print("2. 回到本菜单选择“刷新浏览器 Cookie”。")
    print("3. 再启动服务并运行兼容自测。")


def install_dependencies() -> int:
    return run_command(
        ["cmd", "/c", str(ROOT / "install_adapter_dependencies.bat")],
        title="安装依赖",
    )


def start_server() -> int:
    if not (ROOT / "adapter_env.local.ps1").exists():
        init_local_files()
    return run_command(
        ["cmd", "/c", str(ROOT / "start_ai_server.bat")],
        title="启动本地 OpenAI 兼容服务",
    )


def open_dashboard() -> None:
    webbrowser.open(DASHBOARD_URL)
    print(f"已打开控制台: {DASHBOARD_URL}")


def refresh_cookies() -> int:
    if not (ROOT / "adapter_env.local.ps1").exists():
        init_local_files()
    return powershell(
        ". .\\adapter_env.local.ps1; python .\\refresh_gemini_cookies_from_browser.py",
        title="刷新浏览器 Cookie",
    )


def smoke_test() -> int:
    ok, _ = get_health()
    if not ok:
        print("服务还没有运行。请先选择“启动服务”，看到 Uvicorn running 后再自测。")
        return 1
    return powershell(
        ". .\\adapter_env.local.ps1; .\\test_adapter_openai_compat.ps1 -Model gemini-3-flash",
        title="OpenAI 兼容自测",
    )


def export_sync_pack() -> int:
    return powershell(
        ".\\export_company_sync_pack.ps1",
        title="导出公司电脑同步包",
    )


def open_docs() -> None:
    docs = ROOT / "TEAM_QUICK_START.md"
    if not docs.exists():
        docs = ROOT / "README.md"
    open_path(docs)
    print(f"已打开说明: {docs.name}")


def print_menu() -> None:
    print_header()
    print("1. 查看环境状态")
    print("2. 初始化本地配置文件")
    print("3. 安装/更新依赖")
    print("4. 启动服务")
    print("5. 打开控制台")
    print("6. 刷新浏览器 Cookie")
    print("7. 运行兼容自测")
    print("8. 打开同事快速上手说明")
    print("9. 导出公司电脑同步包")
    print("0. 退出")
    print()


def interactive_menu() -> int:
    actions = {
        "1": lambda: show_status(),
        "2": lambda: init_local_files(),
        "3": lambda: install_dependencies(),
        "4": lambda: start_server(),
        "5": lambda: open_dashboard(),
        "6": lambda: refresh_cookies(),
        "7": lambda: smoke_test(),
        "8": lambda: open_docs(),
        "9": lambda: export_sync_pack(),
    }
    while True:
        print_menu()
        choice = input("请选择: ").strip()
        if choice == "0":
            return 0
        action = actions.get(choice)
        if action is None:
            print("无效选项。")
            pause()
            continue
        action()
        pause()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini OpenAI Adapter team menu")
    parser.add_argument("--check", action="store_true", help="print status and exit")
    parser.add_argument("--init", action="store_true", help="create local config files")
    parser.add_argument("--dashboard", action="store_true", help="open dashboard")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        show_status()
        return 0
    if args.init:
        init_local_files()
        return 0
    if args.dashboard:
        open_dashboard()
        return 0
    return interactive_menu()


if __name__ == "__main__":
    raise SystemExit(main())
