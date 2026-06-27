# 开发交接说明

这份文档用于让后续接手的 AI 或开发者快速理解本项目的演进、边界、功能和常见故障。接手前优先阅读本文件，再按需阅读 `README.md` 与其他专题文档。

## 项目定位

本项目把上游 `gemini_webapi` 封装成 OpenAI 兼容的本地 HTTP 服务，让 Cline / Continue / IDE AI 插件可以通过：

```text
http://127.0.0.1:8000/v1
```

调用 Gemini 网页端逆向接口。

主入口：

- Windows: `START_HERE.bat`
- macOS: `START_HERE.command`
- Web 控制台: `http://127.0.0.1:8000/`
- 主服务文件: `openai_adapter_server.py`

## 架构边界

必须遵守：

- `src/gemini_webapi/` 是上游 Gemini WebAPI 基座代码，默认视为只读。
- 新功能优先放在 `openai_adapter_server.py`、`scripts/`、`examples/`、`docs/`、`tests/`。
- 不要提交本机 Cookie、运行日志、用量日志、调试 prompt。
- 修改已有行为前先读当前代码和 `.clinerules`，不要凭印象改。

敏感或本机文件：

```text
gemini_cookies.local.json
adapter_env.local.ps1
adapter_env.local.sh
runtime/
adapter_usage*.jsonl
adapter_forwarded_prompt.debug.txt
提示词*.txt
```

## 当前核心功能

### OpenAI 兼容接口

实现位置：`openai_adapter_server.py`

主要路由：

- `POST /v1/chat/completions`
- `POST /chat/completions`
- `GET /v1/models`
- `GET /models`
- `GET /health`

支持：

- OpenAI 格式 `messages`
- `stream: true` SSE 流式输出
- 非流式输出
- OpenAI 风格错误 JSON
- 模型名映射到 Gemini 模型
- 可选 gpt4free sidecar 路由

### Cline 兼容增强

已针对 Cline 做过大量提示词压缩和工具使用修复：

- 压缩 Cline 大段系统提示词
- 避免模型反复说“无法访问本地文件”
- 保留 Cline 工具调用规则
- 过滤模型自我拒绝回答
- 控制上下文预算，默认硬限制 `48000` 估算 tokens
- 对本地文件请求注入少量 adapter 读取到的文件上下文

关键环境变量：

```text
OPENAI_ADAPTER_PROMPT_MODE=auto
OPENAI_ADAPTER_MAX_PROMPT_TOKENS=48000
OPENAI_ADAPTER_LOCAL_FILE_CONTEXT=1
OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_FILES=3
OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_CHARS=120000
OPENAI_ADAPTER_CLINE_MESSAGE_MAX_CHARS=18000
OPENAI_ADAPTER_CLINE_OLDER_MESSAGE_MAX_CHARS=7000
OPENAI_ADAPTER_CLINE_KEEP_RECENT_MESSAGES=8
```

实测经验：

- 日常建议把单任务控制在约 `32k` tokens 内。
- 本地探测中过 `64k` 左右有成功记录。
- `128k` 已出现失败边界。

### 流式输出修复

实现点：

- `_openai_sse_events`
- `_prime_gemini_stream`
- `_stream_chunk_payload`
- `_stream_eager_enabled`
- `_stream_fallback_enabled`

修复过的问题：

- Gemini stream 报错后 Cline 长时间“思考中”无心跳
- 上游 stream 失败时回退到非流式请求
- SSE 结束时补齐 finish chunk 和 `[DONE]`
- 支持 `OPENAI_ADAPTER_STREAM_PING_SECONDS` 心跳配置

关键配置：

```text
OPENAI_ADAPTER_STREAM_EAGER=1
OPENAI_ADAPTER_STREAM_PING_SECONDS=15
OPENAI_ADAPTER_STREAM_FALLBACK_NON_STREAM=1
OPENAI_ADAPTER_STREAM_FALLBACK_MODEL=gemini-3-flash
OPENAI_ADAPTER_UPSTREAM_RETRIES=0
```

### Cookie 与鉴权

本项目不改变上游 Gemini 鉴权顺序，只在外层维护 Cookie 文件和热重载。

功能：

- 从 `gemini_cookies.local.json` 加载 Cookie
- 周期性把 Gemini 客户端刷新后的 Cookie 写回本地文件
- 控制台点击“刷新登录凭据”后重新读取浏览器 Cookie 并热重载客户端
- 普通浏览器 Cookie 读取失败后，回退到 Chrome CDP 专用 Profile

关键路由：

- `POST /admin/refresh-cookies`
- `GET /health`

关键脚本：

- `scripts/refresh_gemini_cookies_from_browser.py`
- `scripts/capture_browser_gemini_cookies_cdp.py`
- `scripts/repair_auth_with_browser_cdp.ps1`

常见状态：

```text
AVAILABLE          正常
UNAUTHENTICATED    Cookie 失效或浏览器登录态不可用
```

如果 `/health` 显示 `UNAUTHENTICATED`，先确认浏览器里的 Gemini 能正常发送消息，再刷新登录凭据。

### Web 控制台

入口：

```text
http://127.0.0.1:8000/
```

集成功能：

- 入门引导
- 服务状态
- 终端输出
- 登录凭据刷新
- 用量统计
- GitHub 风格每日用量热力图
- 模型列表
- 快速测试
- 限流探测
- 提示词体积探测
- Cline / Continue 配置速查

曾修复：

- 控制台中文化
- `/usage.html`、`/cookie.html` 统一重定向到一个入口
- 长时间刷新 Cookie 时不再锁死整个面板
- 请求时间改为普通日期时间
- 热力图按 GitHub 贡献图布局修正今天位置

### 用量统计与多电脑汇总

本地日志：

```text
runtime/adapter_usage.jsonl
```

共享日志：

```text
usage-sync/adapter_usage.<instance_id>.jsonl
```

相关变量：

```text
OPENAI_ADAPTER_USAGE_LOG_PATH
OPENAI_ADAPTER_USAGE_SHARED_DIR
OPENAI_ADAPTER_INSTANCE_NAME
OPENAI_ADAPTER_INSTANCE_ID
OPENAI_ADAPTER_USD_TO_CNY
OPENAI_ADAPTER_USAGE_TZ_OFFSET_HOURS
```

设计：

- 本机写 `runtime/adapter_usage.jsonl`
- 同时写入 `usage-sync/adapter_usage.<电脑名>.jsonl`
- Git 同步 `usage-sync/` 后，多电脑控制台可汇总展示

注意：

- `usage-sync/adapter_usage*.jsonl` 有意允许提交。
- 根目录或 runtime 下的用量日志仍应忽略。

### gpt4free sidecar 支持

可选实验功能，用于把部分 GPT 模型名路由到外部 gpt4free OpenAI 兼容服务。

配置示例：

```text
OPENAI_ADAPTER_G4F_BASE_URL=http://127.0.0.1:1337/v1
OPENAI_ADAPTER_G4F_PROVIDER=PollinationsAI
OPENAI_ADAPTER_G4F_TIMEOUT_SECONDS=180
OPENAI_ADAPTER_G4F_MODELS=g4f:gpt-4o-mini,g4f:deepseek-v3
OPENAI_ADAPTER_G4F_ROUTE_OPENAI_MODELS=1
```

说明：

- 不把 gpt4free 源码复制进本项目。
- 只作为实验/备用通道。
- provider 稳定性取决于 gpt4free 自己的上游。

### 跨平台启动

Windows：

- `START_HERE.bat`
- `scripts/launch_adapter.ps1`
- `scripts/run_server.ps1`
- `examples/adapter_env.example.ps1`

macOS：

- `START_HERE.command`
- `scripts/launch_adapter.sh`
- `scripts/run_server.sh`
- `scripts/install_adapter_dependencies.sh`
- `examples/adapter_env.example.sh`

macOS 说明见：

```text
docs/MACOS_QUICK_START.md
```

## 重要迭代记录

### v0.1 初始适配

- 新增 `openai_adapter_server.py`
- 实现 FastAPI 服务
- 实现 `/v1/chat/completions`
- 支持 OpenAI 请求体解析
- 支持 OpenAI SSE 流式格式
- 不修改上游 Gemini WebAPI 核心代码

### v0.2 Cline 可用性修复

- 增强 Cline 工具调用提示
- 修复模型要求用户复制文件的问题
- 增加本地文件上下文注入
- 增加 prompt 压缩策略
- 增加 prompt budget 保护
- 增加 PowerShell 规则，避免 Cline 使用 `&&`

### v0.3 Cookie 与鉴权稳定性

- 增加 Cookie 文件读取
- 增加 Cookie 写回
- 增加 `/admin/refresh-cookies`
- 增加浏览器 Cookie 刷新脚本
- 增加 Chrome CDP 专用 Profile 回退
- 增加 `/health` 认证状态展示

### v0.4 控制台与用量统计

- 增加统一 Web 控制台
- 增加用量估算
- 增加模型/电脑维度统计
- 增加 GitHub 风格每日用量热力图
- 增加多电脑 `usage-sync/` 汇总
- 增加终端日志查看

### v0.5 流式与边界测试

- 增加 stream 心跳
- 增加 stream 失败非流式回退
- 增加限流探测
- 增加提示词体积探测
- 根据实测把日常建议上下文控制在 32k 左右

### v0.6 项目整理与跨平台

- 根目录保留关键入口
- 脚本、文档、测试样例分类放入目录
- 增加 macOS 启动脚本和说明
- 增加同步包脚本
- README 简化并链接专题文档

### v0.7 可选 gpt4free sidecar

- 增加 gpt4free OpenAI 兼容上游路由
- 支持 `g4f:` 模型前缀
- 可选把常见 GPT 模型名自动路由到 gpt4free
- `/v1/models` 可暴露 g4f 模型

## 常见故障速查

### Cline 报 OpenAI API key required

现象：

```text
[OPENAI] OpenAI API key or Azure Identity Authentication is required
```

通常不是 adapter 错，而是 Cline 的 OpenAI provider 没填 API Key。即使本地服务不验证 key，Cline UI 仍要求填一个非空值。

处理：

```text
Base URL: http://127.0.0.1:8000/v1
API Key: dummy
Model: gemini-3-pro 或 gemini-3-flash
```

如果 Cline 有“OpenAI Compatible”或“Custom OpenAI Compatible”选项，优先用它；不要选 Azure OpenAI。

### `/health` 是 ok，但 account_status 是 UNAUTHENTICATED

说明服务启动了，但 Gemini 登录失效。

处理：

1. 打开浏览器确认 Gemini 网页端能发送消息。
2. 控制台点击“刷新登录凭据”。
3. 如果专用 Chrome 窗口已登录但仍失败，退出 Gemini 后重新登录，并发送一条消息。

### Cline 长时间思考无输出

检查：

- 控制台“终端输出”
- `/health`
- 是否触发 Gemini stream 错误
- 是否 prompt 超过 budget

处理：

- 切 `gemini-3-flash` 测试
- 拆小任务
- 降低上下文
- 保持 `OPENAI_ADAPTER_STREAM_FALLBACK_NON_STREAM=1`

### PowerShell 命令失败

Windows PowerShell 5.x 不支持 `&&`。

应使用：

```powershell
Set-Location "D:\GeminiAPI\Gemini-API"; python -m py_compile openai_adapter_server.py
```

或拆成两次命令。

## 接手开发建议

### 修改前

1. 看 `git status --short`
2. 确认当前工作目录是真正仓库根目录
3. 不要碰本机敏感文件
4. 对已有文件做局部修改前先读取相关代码

### 修改后

至少运行：

```powershell
python -m py_compile openai_adapter_server.py
```

如改了兼容接口，再运行：

```powershell
.\tests\test_adapter_openai_compat.ps1 -Model gemini-3-flash
```

如改了脚本：

- Windows 脚本用 PowerShell 测试
- macOS `.sh` 脚本至少用 `bash -n` 检查

### 不建议做的事

- 不要把上游 `src/gemini_webapi/` 大改成本项目私有分叉。
- 不要默认提交 `usage-sync/adapter_usage*.jsonl` 之外的日志。
- 不要把 Cookie、prompt dump、测试 payload 贴进文档或提交。
- 不要为了解决 Cline 上下文问题把整个项目内容塞进 prompt。

## 下一步可改进方向

- 给 Cline/Continue 分别写更精确的配置截图和 JSON 示例
- 增加 `/admin/diagnose`，一次性输出“服务、鉴权、模型、配置、端口、依赖”的诊断报告
- 增加真正异步后台任务版 Cookie 刷新，前端只轮询状态
- 给 macOS Cookie CDP 流程做实机验证
- 给 gpt4free sidecar 增加健康检查和 provider 列表展示
- 增加自动化测试覆盖 prompt 压缩、SSE 格式和错误响应
