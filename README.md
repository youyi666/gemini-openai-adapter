# Gemini OpenAI Adapter

这是一个本地 FastAPI 服务，把 Google Gemini 网页端逆向客户端封装成 OpenAI 兼容接口，方便 Cline、Continue.dev 等 IDE AI 插件把它当作本地 OpenAI 服务使用。

本项目的目标不是重新实现 Gemini 网页协议，而是在保留上游 `gemini_webapi` 客户端的基础上，新增一层 OpenAI API 适配器、Cline 兼容提示词、Cookie 管理、用量统计和本地控制台。

## 文件来源说明

### 保留的上游客户端代码

这些文件来自 `HanaokaYuzu/Gemini-API`，当前仍然是 adapter 的运行依赖：

```text
src/gemini_webapi/**
LICENSE
```

`openai_adapter_server.py` 会直接导入：

```python
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus, Model
from gemini_webapi.exceptions import APIError, AuthError, GeminiError
```

所以 `src/gemini_webapi` 不能直接删除。除非以后改成从 PyPI 或 GitHub 安装等价版本，并完整回归测试鉴权、流式输出、模型选择、Cookie 轮换，否则删除后服务会启动失败。

`LICENSE` 也需要保留，因为仓库仍然包含上游客户端源码。

### 已删除的原仓库周边文件

这些文件不参与本 adapter 运行，已经从当前项目移除：

```text
.github/
.vscode/
assets/
tests/
cli.py
```

原作者的 README 也已经被本说明替换。

### 本项目新增的核心文件

```text
openai_adapter_server.py               FastAPI OpenAI 兼容服务
refresh_gemini_cookies_from_browser.py 从 Chrome / Edge 刷新 Gemini Cookie
capture_edge_gemini_cookies_cdp.py     Edge CDP Cookie 抓取辅助脚本
adapter_env.example.ps1                PowerShell 环境变量示例
gemini_cookies.example.json            Cookie JSON 示例
start_ai_server.bat                    双击启动服务
install_adapter_dependencies.bat       安装依赖
test_adapter_openai_compat.ps1         OpenAI 兼容接口自测
open_usage_dashboard.bat               打开控制台
usage-sync/                            多电脑用量汇总目录
.clinerules                            Cline 项目规则
.clineignore                           Cline 忽略规则
公司电脑部署使用说明.md                  公司电脑快速部署说明
CODEX_LIKE_TESTS.md                    接近 Codex 能力的测试清单
COMPANY_SYNC_README.md                 多电脑同步说明
```

## 架构原理

整体链路：

```text
Cline / Continue.dev
        |
        | OpenAI-compatible HTTP
        v
POST /v1/chat/completions
        |
        | messages -> Gemini prompt
        v
GeminiClient.generate_content_stream / generate_content
        |
        | Gemini chunks
        v
OpenAI SSE chunks / JSON response
```

核心模块：

- `FastAPI` 提供 HTTP 服务。
- `/v1/chat/completions` 接收 OpenAI Chat Completions 请求。
- `messages` 会被转换成 Gemini 可理解的 prompt。
- Cline 的长系统提示词会被压缩成更短的兼容提示词，减少自我拒绝。
- 流式输出通过 `sse-starlette` 转成 OpenAI SSE 格式。
- `/v1/models` 返回可用模型列表。
- 同时提供 `/chat/completions` 和 `/models` 兼容别名，减少 Base URL 少写 `/v1` 时的 404。
- `/health` 返回服务、账号、Cookie 和 prompt 上限状态。
- `/` 是统一控制台，包含状态、Cookie、用量、模型、快速测试、限流探测、Prompt 体积探测。

## 当前能力

- OpenAI 兼容接口：`POST /v1/chat/completions`
- 支持 `stream=true` 的 SSE 输出
- 支持非流式 JSON 输出
- Cline / Continue.dev 可连接
- Gemini Cookie JSON 加载
- Cookie 自动写回
- 从 Chrome / Edge 刷新 Cookie
- 可指定 Cookie 来源浏览器和 Profile
- 本地用量日志与费用估算
- 多电脑用量汇总目录
- GitHub 风格每日用量热力图
- 限流探测
- Prompt 体积探测
- 48k prompt 硬拦截，32k 推荐工作预算

## 安装

进入项目目录：

```powershell
Set-Location "D:\GeminiAPI\Gemini-API"
```

安装依赖：

```powershell
.\install_adapter_dependencies.bat
```

或者手动安装：

```powershell
python -m pip install -e ".[browser]"
```

## 配置

复制示例配置：

```powershell
copy adapter_env.example.ps1 adapter_env.local.ps1
copy gemini_cookies.example.json gemini_cookies.local.json
```

编辑本地环境：

```powershell
notepad adapter_env.local.ps1
```

常用配置：

```powershell
$env:OPENAI_ADAPTER_HOST = "127.0.0.1"
$env:OPENAI_ADAPTER_PORT = "8000"
$env:GEMINI_DEFAULT_MODEL = "gemini-3-pro"
$env:OPENAI_ADAPTER_MAX_PROMPT_TOKENS = "48000"
$env:OPENAI_ADAPTER_COOKIE_BROWSER = "auto"
$env:OPENAI_ADAPTER_COOKIE_PROFILE = "Default"
```

Cookie 来源可选值：

```powershell
$env:OPENAI_ADAPTER_COOKIE_BROWSER = "auto"   # 自动选择
$env:OPENAI_ADAPTER_COOKIE_BROWSER = "chrome" # 只读 Chrome
$env:OPENAI_ADAPTER_COOKIE_BROWSER = "edge"   # 只读 Edge
```

如果给 adapter 准备了专用浏览器 Profile，把 `OPENAI_ADAPTER_COOKIE_PROFILE` 改成真实目录名，比如 `Profile 1`。

## 启动

双击：

```text
start_ai_server.bat
```

或者 PowerShell 启动：

```powershell
Set-Location "D:\GeminiAPI\Gemini-API"
. .\adapter_env.local.ps1
python .\openai_adapter_server.py
```

启动后打开控制台：

```text
http://127.0.0.1:8000/
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 8
```

正常状态应包含：

```json
{
  "status": "ok",
  "gemini_client": "initialized",
  "account_status": {
    "authenticated": true
  },
  "prompt_budget": {
    "max_prompt_tokens": 48000
  }
}
```

## Cline 配置

Cline 里选择 OpenAI Compatible 类型：

```text
Base URL: http://127.0.0.1:8000/v1
API Key: local
Model ID: gemini-3-pro
```

如果某个插件要求 Base URL 不带 `/v1`，也可以填：

```text
Base URL: http://127.0.0.1:8000
```

因为 adapter 同时提供了 `/chat/completions` 和 `/models` 两个兼容别名。

稳妥模型：

```text
gemini-3-flash
```

更强模型：

```text
gemini-3-pro
```

建议：

- 普通文件修改优先用 `gemini-3-flash`。
- 复杂设计、跨文件分析再切 `gemini-3-pro`。
- 任务太长时让 Cline 拆阶段处理。
- 不要让 Cline 一次性读取整个项目。

## 简单测试

非流式：

```powershell
$body = @{
  model = "gemini-3-flash"
  stream = $false
  messages = @(
    @{ role = "user"; content = "只回复 OK" }
  )
} | ConvertTo-Json -Depth 5 -Compress

Invoke-RestMethod `
  -Uri http://127.0.0.1:8000/v1/chat/completions `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

流式：

```powershell
$body = '{"model":"gemini-3-flash","stream":true,"messages":[{"role":"user","content":"用一句话说 adapter 已连接。"}]}'

curl.exe -N `
  -H "Content-Type: application/json" `
  -d $body `
  http://127.0.0.1:8000/v1/chat/completions
```

完整兼容自测：

```powershell
.\test_adapter_openai_compat.ps1
```

## Prompt 上限策略

当前策略分两层：

- `.clinerules` 提醒 Cline 尽量把任务控制在 32k token 工作预算内。
- adapter 实际硬上限是 48k token。

原因：

- 本地测试中约 64k token 成功。
- 128k token 失败。
- 32k 适合作为日常安全预算。
- 48k 适合作为任务尾声的容错上限。

如果请求超过上限，会返回：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Prompt is too large ..."
  }
}
```

处理方式：

- 新开 Cline 任务继续最后一步。
- 要求 Cline 只读取相关文件。
- 删除无关命令输出和长日志。
- 把大任务拆成多个小任务。

## Cookie 与浏览器建议

建议给 adapter 单独准备一个浏览器 Profile，只登录 Gemini。

原因：

- Gemini Cookie 会频繁轮换。
- 同一个账号同时被浏览器和本地逆向客户端使用，可能导致浏览器被登出。
- 专用 Profile 可以避免影响日常 Chrome / Edge 主账号。

控制台首页的 Cookie 区可以刷新 Cookie。页面只显示来源和长度，不显示真实 Cookie 值。

## 用量统计

本地请求会写入：

```text
adapter_usage.jsonl
```

多电脑汇总目录：

```text
usage-sync/
```

默认不会提交真实用量日志。日志包含时间、模型、token 估算和费用估算，不包含 prompt 或回复正文。

## 安全边界

不要提交这些文件：

```text
gemini_cookies.local.json
adapter_env.local.ps1
adapter_usage*.jsonl
adapter_forwarded_prompt.debug.txt
.gemini_cookie_cache/
.env
```

`.gitignore` 和 `.clineignore` 已经覆盖这些文件。

## 常见问题

### 端口被占用

报错：

```text
WinError 10048
```

说明已有服务占用 `127.0.0.1:8000`。关闭旧服务，或者修改：

```powershell
$env:OPENAI_ADAPTER_PORT = "8001"
```

### 鉴权失败

如果 `/health` 显示：

```text
UNAUTHENTICATED
```

处理：

1. 打开 `https://gemini.google.com`
2. 确认浏览器能正常发送消息
3. 回到控制台点击刷新 Cookie
4. 再看 `/health`

### Cline 提示无法访问文件

新开任务，并明确要求它使用工具读取文件，不要让你复制粘贴代码。项目 `.clinerules` 已经写入相关规则。

### Cline 一直思考或自动重试失败

通常是上下文太长、上游慢、Cookie 异常或模型能力不足。优先尝试：

- 切到 `gemini-3-flash`
- 新开任务
- 明确限制读取文件数量
- 只让它做一个小步骤

## 保留上游代码的原因

本项目目前采用 vendored client 架构：

```text
adapter 自己实现 OpenAI API 外壳
上游 src/gemini_webapi 负责 Gemini 网页协议
```

这样做的好处：

- 不修改上游核心代码。
- 适配器逻辑和 Gemini 协议逻辑分离。
- 本地运行稳定，不依赖远端包版本临时变化。

这样做的代价：

- 仓库里仍然保留部分上游源码。
- 未来升级 Gemini 协议时，需要从上游同步 `src/gemini_webapi`。

如果将来要彻底删除 `src/gemini_webapi`，需要先把它改成外部依赖，并完成以下测试：

- `/health`
- `/v1/models`
- 非流式对话
- 流式对话
- Cookie 刷新
- Cline 读文件
- Cline 写文件
- Prompt 体积边界
