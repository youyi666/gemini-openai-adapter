# Cline + Gemini 适配器排查手册

> 适用场景：本地 Gemini Web API 适配器（`openai_adapter_server.py`，端口 8000）+ VS Code Cline 插件

---

## 问题一：Cline 两次提问就触发 48000 token 上限

**症状**：Cline 界面报 "prompt too long"，只提问 1-2 次就中断。

**原因**：适配器默认 token 上限过低，且会把本地文件内容注入 prompt，严重膨胀。

**修复**（`adapter_env.local.ps1`）：
```powershell
$env:OPENAI_ADAPTER_MAX_PROMPT_TOKENS = "200000"   # 从 48000 改为 200000
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT = "0"        # 禁用本地文件自动注入
```

---

## 问题二：Cline 检测逻辑失效（紧凑模式不触发）

**症状**：Gemini 自我介绍说"我是 Gemini"，拒绝直接调用工具，让用户手动粘贴文件内容。

**原因**：Cline 3.x（新版）系统提示格式与旧版不同，旧版检测只匹配 `TOOL USE`、`ACT MODE` 等旧标记，新版没有这些词。

**新版特征**：系统提示包含 `"You are Cline"` + `"AI coding agent"` + `"<env>"`。

**现状**：适配器已同时支持新旧两种格式检测。新版附加反拒绝后缀，旧版替换为紧凑提示。

---

## 问题三：Cline 3.x 工具不执行，AI 只输出文字

**症状**：Gemini 回复里有 `<read_files>` XML，但 Cline 没有实际执行读文件操作，对话就结束了。

**原因**：
- 旧版 Cline：AI 在文本里写 XML，Cline 解析 XML 执行工具
- **新版 Cline 3.x**：只接受 OpenAI `tool_calls` 格式（结构化 JSON），完全忽略文本里的 XML

**修复**：适配器现在会缓冲 Gemini 的完整输出，检测 XML 工具调用，转换成 `tool_calls` 格式再返回给 Cline。

---

## 问题四：`read_files` 参数格式错误

**症状**：Cline 报错 `Invalid input for tool read_files: expected object, received string`，工具调用被拒绝。

**原因**：`read_files` 的 `files` 参数需要**对象数组** `[{"path": "..."}]`，但 Gemini 生成了**字符串数组** `["..."]`。

**正确格式**：
```xml
<read_files>
<files>[{"path": "D:\\absolute\\path\\to\\file.ext"}]</files>
</read_files>

<!-- 指定行范围 -->
<read_files>
<files>[{"path": "D:\\path\\file.ext", "start_line": 1, "end_line": 50}]</files>
</read_files>
```

**修复**：系统提示里的示例已更正为对象数组格式，工具 schema 解析也已升级以正确展示嵌套结构。

---

## 问题五：任务完成后 Cline 无限循环重复输出

**症状**：任务完成后，Cline 界面连续出现多次相同的 "Task Completed"，停不下来。

**原因**：`attempt_completion` 被转换成了 `tool_calls`，Cline 执行后会把工具结果发回给 Gemini，Gemini 又调用 `attempt_completion`，形成死循环。

**原则**：`attempt_completion` 是终止信号，**必须作为文本内容返回**，不能作为 `tool_calls`。

**修复**：适配器排除了 `attempt_completion` 的 `tool_calls` 转换，它会以纯文本形式到达 Cline，Cline 显示结果后等待用户输入，循环终止。

---

## 问题六：Generate Commit Message 按钮生成英文提交信息

**症状**：VS Code Source Control 面板点「生成提交信息」按钮，生成英文 commit；但在输入框里输入"commit message"再点，生成中文 commit。

**原因**：
1. 按钮使用独立的系统提示（"You are a helpful assistant that generates git commit messages..."），不包含 Cline 标记，走了非 Cline 代码路径。
2. 中文覆盖逻辑原来只在 Cline 路径里执行，非 Cline 路径被跳过了。

**修复**：适配器现在在非 Cline 路径也检测 commit 请求，匹配到后把中文覆盖指令注入系统提示。

**commit 请求触发条件**（满足任一即触发）：
- 消息中含关键词：`commit message`、`generate commit`、`git commit`、`staged changes`、`提交信息`、`生成提交` 等
- 用户消息含 git diff 标记：`diff --git `、`+++ b/`、`--- a/`

---

## 问题七：`search_codebase` 验证失败（queries 格式错误）

**症状**：Cline 报错 `Invalid input for tool search_codebase: expected string, received object` 或 `expected array, received string`，搜索无法执行。

**原因**：Gemini 生成的 `queries` 格式不对。该工具期望 `queries` 是**字符串数组** `["term"]`，但 Gemini 会输出：
- 嵌套 XML：`<queries><query>term</query></queries>`
- 或对象数组：`[{"query": "term"}]`

**修复**：适配器的 XML 解析器现已自动转换：
- 纯文本子元素 → 字符串：`<query>X</query>` → `"X"` → 最终 `["X"]`
- 有嵌套子标签的元素 → 对象：`<file><path>x</path></file>` → `{"path": "x"}`

---

## 问题八：Cline 4.x 升级后检测失效（commit 英文、工具不执行、压缩不触发）

**症状**：Cline 升级到 4.0.x 后，commit 消息变回英文；或 Gemini 自我介绍说"我是 Gemini"；或历史消息不再被压缩，token 超限更频繁。

**原因**：Cline 4.0.x 移除了系统提示中的 `"AI coding agent"` 关键词，改为多种变体描述：
- `"You are Cline, a highly skilled software engineer..."`
- `"You are Cline, a software engineering AI..."`
- `"You are Cline, a senior software engineer + precise task runner..."`

适配器旧检测要求同时包含 `"You are Cline"` + `"AI coding agent"`，Cline 4.x 全部判定为非 Cline 请求，导致：
1. 消息压缩、anti-refusal 后缀、commit 中文注入等所有 Cline 专属逻辑全部失效
2. 原始大消息直通 Gemini，token 用量暴涨

**修复**（已在 `openai_adapter_server.py` 中更新，2026-06-29）：`_is_cline_system_prompt()` 新增对 Cline 4.x 变体词的检测，同时兼容旧版。

**排查确认**：服务日志里搜索 `Applied compact Cline prompt` 或 `New Cline format detected`，有输出则说明检测正常。

---

## 问题九：token 超限（50K > 48K）重启后复现

**症状**：`adapter_env.local.ps1` 里修改了 `MAX_PROMPT_TOKENS`，测试通过；但重启系统或重启服务后报错恢复。

**原因**：`adapter_env.local.ps1` 在 `.gitignore` 里，不进入版本库。如果只是在会话里手动设置了环境变量，或者在本地文件中修改后**没有保存**，重启进程就会重新读取旧值。

**正确修复**（永久生效）：确保 `adapter_env.local.ps1` 文件中包含：
```powershell
$env:OPENAI_ADAPTER_MAX_PROMPT_TOKENS = "200000"   # 从 48000 改为 200000
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT = "0"        # 禁用本地文件自动注入（防膨胀）
```

**家里电脑同步**：此文件不在 git 里，需手动复制或参照上述两行在家里的 `adapter_env.local.ps1` 里同步修改。

---

## 问题十：`gemini_stream_error` — The original request may have been silently aborted by Google

**症状**：Cline 报错 `gemini_stream_error`，主模型和 fallback 模型都失败，错误信息包含 "silently aborted by Google"。

**根因**：Gemini Web API 的流式响应提前结束，且没有返回可恢复的对话 ID（CID）。触发场景：

| 触发原因 | 表现 | 排查方法 |
|----------|------|----------|
| Prompt 过大（Cline 4.x 工具重复注入） | 新对话也报错，日志 `prompt_chars > 35000` | 看 `server.log` 里最近一条 `prompt_chars=...` |
| Gemini 服务端瞬时抖动 | 偶发，重试即成功 | 直接点 Retry |
| Cline 历史太长 | 长对话后开始报错 | 开新任务 |
| Cookie 失效（少见） | `health` 显示 `authenticated: false` | 打开浏览器重新登录 Gemini |

**重要**：`runtime/gemini_cookie_cache/*.json` 写入失败（`[Errno 2] No such file`）是**无害副路径报错**，不影响实际认证。真正的 cookie 写回路径是 `gemini_cookies.local.json`，日志里有 `Wrote refreshed Gemini cookies` 则说明 cookie 健康。

**排查步骤**：
```powershell
# 1. 看最近请求的 prompt 大小
Select-String "prompt_chars" "...\runtime\server.log" | Select-Object -Last 5

# 2. 确认 cookie 健康
(Invoke-RestMethod http://127.0.0.1:8000/health).account_status

# 3. 看 Cline 检测是否生效（prompt 压缩是否触发）
Select-String "compact Cline|New Cline format" "...\runtime\server.log" | Select-Object -Last 3
```

**修复优先级**：
1. 先点 **Retry** —— 瞬时抖动重试即可
2. 若 prompt_chars > 35000 —— `Ctrl+Shift+P → Developer: Reload Window`，再开新 Cline 任务
3. 若持续失败 —— 浏览器打开 `gemini.google.com` 确认登录状态，然后重启适配器

---

## 新功能：历史摘要压缩（2026-07-01）

**背景**：问题八/问题九里提到的"历史消息压缩"，原本只是对超长的旧消息做**硬截断**（保留头尾、挖掉中间），会丢失被挖掉部分的具体内容（比如中途改了哪个文件、踩了什么坑怎么解决的）。长任务被硬截断到一定程度后，Gemini 经常"忘记"前面做过的判断，重复试错。

**改动**：`_compact_cline_history_messages()` 现在对"最近 8 轮"之外、连续超限的旧消息，先尝试调一次 Gemini 把这段历史压成一段结构化摘要（保留改动过的文件路径、关键决定、报错与解决方式、未完成的状态），再把摘要塞回上下文，而不是直接挖空中间内容。摘要调用失败、超时或被关闭时，**自动退回原来的硬截断逻辑**，不会导致请求失败。

**相关环境变量**（`adapter_env.local.ps1` / `examples/adapter_env.example.ps1`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_ENABLED` | `1` | 总开关，设为 `0` 恢复纯硬截断 |
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_TRIGGER_CHARS` | `12000` | 一段旧历史累计超过多少字符才值得摘要 |
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_MAX_CALLS` | `3` | 单次请求最多并发几次摘要调用 |
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_TIMEOUT_SECONDS` | `25` | 单次摘要调用超时秒数 |
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_INPUT_MAX_CHARS` | `60000` | 喂给摘要模型的输入上限 |
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_OUTPUT_MAX_CHARS` | `3000` | 摘要输出上限 |
| `OPENAI_ADAPTER_CLINE_HISTORY_SUMMARY_MODEL` | 空（跟随主请求模型） | 想用更便宜的模型做摘要可单独指定 |

**排查确认**：服务日志里搜索 `Compacted long`，能看到 `truncated=` 和 `summarized=` 两个计数；`summarized` 非零说明摘要路径生效了。也可以打开 `OPENAI_ADAPTER_DEBUG_PROMPT_PATH` 指向的文件，找 `[Adapter compacted N earlier ... turns into a summary]` 这段标记。

---

## 通用操作：适配器重启后 Cline 无响应

**症状**：重启适配器后，Cline 聊天或按钮没有反应，适配器日志里看不到任何 POST 请求。

**原因**：Cline 插件缓存了旧连接，适配器重启后没有自动重连。

**解决**：
```
Ctrl+Shift+P → Developer: Reload Window
```
重载 VS Code 窗口，Cline 会重新连接适配器。

---

## 改造全记录（2026-06-27）

本次改造针对 Cline 3.x 与适配器的完整兼容性问题，历经多轮调试，最终 8 轮多工具对话测试全部通过。

### 核心架构变化

**改造前**：适配器仅转发文本，依赖旧版 Cline 从文本里解析 XML 工具调用。

**改造后**：
1. 检测 Gemini 输出里的 XML 工具调用
2. 缓冲完整响应，解析 XML
3. 转换成 OpenAI `tool_calls` 格式返回给 Cline 3.x
4. `attempt_completion` 特殊处理为纯文本，防止死循环

### XML 参数解析规则

适配器自动处理 Gemini 不规范的 XML 输出：

| Gemini 输出 | 转换结果 |
|-------------|----------|
| `["value"]`（标准 JSON） | 直接使用 |
| `[{"key": "value"}]`（标准 JSON） | 直接使用 |
| `<query>X</query>`（纯文本子元素） | `["X"]`（字符串数组） |
| `<file><path>x</path></file>`（嵌套标签） | `[{"path": "x"}]`（对象数组） |

### 各工具正确参数格式

```xml
<!-- read_files：files 是对象数组 -->
<read_files>
<files>[{"path": "D:\\absolute\\path\\file.ext"}]</files>
</read_files>

<!-- read_files 带行范围 -->
<read_files>
<files>[{"path": "D:\\path\\file.ext", "start_line": 1, "end_line": 50}]</files>
</read_files>

<!-- search_codebase：queries 是字符串数组 -->
<search_codebase>
<queries>["CONTROL_PANEL_PORT"]</queries>
</search_codebase>

<!-- run_commands：commands 是字符串数组 -->
<run_commands>
<commands>["Get-ChildItem -Path . -Filter *.cjs"]</commands>
</run_commands>
```

### 测试结果

| 功能 | 结果 |
|------|------|
| `read_files` 读文件 | ✅ |
| `search_codebase` 搜索 | ✅ |
| `run_commands` 执行命令 | ✅ |
| `attempt_completion` 终止不循环 | ✅ |
| 8 轮多工具对话不溢出 | ✅ |
| Commit 消息中文输出 | ✅ |

---

## 日常排查命令

```powershell
# 查看最近的适配器日志（含请求/错误）
Get-Content "...\gemini_openai_adapter\runtime\server.log" -Tail 30

# 查看发送给 Gemini 的最后一次完整 prompt
Get-Content "...\gemini_openai_adapter\runtime\adapter_forwarded_prompt.debug.txt" -Head 80

# 检查适配器是否在线
Invoke-RestMethod http://127.0.0.1:8000/health

# 检查工具调用转换是否触发
Select-String "Converted XML|tool_calls|Commit message|Cline format" "...\runtime\server.log" | Select-Object -Last 10

# 检查历史摘要压缩是否触发（summarized= 非零说明生效）
Select-String "Compacted long" "...\runtime\server.log" | Select-Object -Last 10
```
