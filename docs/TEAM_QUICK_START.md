# 团队快速上手

这份说明给第一次使用本项目的同事看。目标是：不用理解内部实现，也能在 5 分钟内把本地 OpenAI 兼容服务跑起来，并接到 Cline / Continue.dev。

## 一句话理解

本项目会在本机启动一个服务：

```text
http://127.0.0.1:8000
```

IDE 插件把它当成 OpenAI API 调用，实际请求会由本项目转发到 Gemini 网页端账号。

## 第一次使用

1. 克隆或解压项目到本机。
2. 双击 `START_HERE.bat`。
3. 在菜单里先选 `2. 初始化本地配置文件`。
4. 再选 `3. 安装/更新依赖`。
5. 确认 Chrome 里的 Gemini 网页可以正常发送消息。
6. 回到菜单选 `6. 刷新浏览器 Cookie`。
7. 选 `4. 启动服务`，看到 `Uvicorn running on http://127.0.0.1:8000` 表示服务已启动。
8. 新开一个菜单窗口，选 `7. 运行兼容自测`。

自测通过后，就可以配置 IDE 插件。

## Cline 推荐配置

```text
Provider: OpenAI Compatible
Base URL: http://127.0.0.1:8000/v1
API Key: local
Model ID: gemini-3-flash
```

需要更强推理时再切：

```text
Model ID: gemini-3-pro
```

如果某个插件要求 Base URL 不带 `/v1`，也可以填：

```text
Base URL: http://127.0.0.1:8000
```

因为本项目同时支持 `/v1/chat/completions` 和 `/chat/completions`。

## 日常使用顺序

1. 双击 `START_HERE.bat`。
2. 选 `4. 启动服务`。
3. 打开 Cline 或 Continue.dev 使用。
4. 想看用量、Cookie 状态、测试工具时，选 `5. 打开控制台`。

## 常见问题

### 8000 端口被占用

说明已经有一个服务在运行，或上次窗口没关干净。

处理方式：

- 先看是否已经能打开 `http://127.0.0.1:8000/`。
- 如果能打开，通常不用再启动。
- 如果不能打开，关闭旧的命令行窗口后重新启动。

### health 显示 UNAUTHENTICATED

通常是 Cookie 失效或浏览器账号状态异常。

处理顺序：

1. 先打开 Gemini 网页端，手动发一句话，确认网页本身可用。
2. 回到菜单选 `6. 刷新浏览器 Cookie`。
3. 重启服务。
4. 打开控制台看账号状态是否变成 `AVAILABLE`。

### Cline 报 prompt too large

说明这次任务带了太多历史上下文或文件内容。

建议：

- 新开一个 Cline 任务。
- 让 Cline 只读取必要文件。
- 把任务拆成“先分析、再修改、最后测试”几个阶段。

### Cline 一直思考但没有输出

先打开控制台看 `/health` 是否正常，再运行菜单里的兼容自测。

如果自测通过，通常是当前 Cline 任务上下文太大或模型在重试；新开任务会更稳。

## 不要提交这些文件

这些是每台电脑自己的本地文件，不能上传到 GitHub：

```text
adapter_env.local.ps1
gemini_cookies.local.json
adapter_usage.jsonl
adapter_forwarded_prompt.debug.txt
.gemini_cookie_cache/
dist/
```

项目的 `.gitignore` 已经默认忽略它们。

## 给维护者的提醒

- 改功能后先运行 `python -m py_compile openai_adapter_server.py refresh_gemini_cookies_from_browser.py capture_browser_gemini_cookies_cdp.py capture_edge_gemini_cookies_cdp.py team_menu.py`。
- 再启动服务，运行 `test_adapter_openai_compat.ps1`。
- 推送前确认 `git status --short` 里没有 Cookie、本地配置、用量日志。
