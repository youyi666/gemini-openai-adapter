# macOS 快速上手

这份说明给 macOS 同事使用。核心服务仍是同一个 `openai_adapter_server.py`，mac 只多了一层 shell 启动脚本。

## 1. 准备环境

安装 Python 3.10+ 和 Chrome：

```bash
python3 --version
```

如果缺 Python，建议用 Homebrew：

```bash
brew install python
```

## 2. 启动

在项目根目录执行：

```bash
chmod +x ./START_HERE.command ./scripts/*.sh
./START_HERE.command
```

脚本会自动：

- 复制 `examples/adapter_env.example.sh` 为 `adapter_env.local.sh`
- 复制 `examples/gemini_cookies.example.json` 为 `gemini_cookies.local.json`
- 安装缺失依赖
- 后台启动服务
- 打开控制台 `http://127.0.0.1:8000/`

## 3. Cline 配置

```text
Base URL: http://127.0.0.1:8000/v1
API Key: dummy
Model: gemini-3-flash 或 gemini-3-pro
```

## 4. 刷新 Gemini 登录凭据

先确认 Chrome 里的 Gemini 能正常发送消息。然后在控制台点击：

```text
刷新登录凭据
```

如果系统打开了一个专用 Chrome 窗口，请在这个窗口登录 Gemini，并至少发送一条普通消息。该窗口使用 `runtime/chrome-gemini-profile`，不会污染日常 Chrome 配置。

## 5. 常见问题

### 权限不足，无法双击启动

执行：

```bash
chmod +x ./START_HERE.command ./scripts/*.sh
```

### 端口 8000 被占用

查看占用：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

停止旧服务：

```bash
kill <PID>
```

### 账号显示 UNAUTHENTICATED

通常是 Gemini 登录凭据失效。打开控制台重新点击“刷新登录凭据”。如果专用 Chrome 窗口看起来已登录但仍失败，先退出 Gemini 再重新登录，然后发送一条 Gemini 消息。
