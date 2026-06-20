# Gemini OpenAI Adapter

把本地 Gemini WebAPI 封装成 OpenAI 兼容服务，供 Cline / Continue 等工具使用。

## 常用入口

Windows：

```powershell
.\START_HERE.bat
```

macOS：

```bash
bash ./START_HERE.command
```

控制台地址：

```text
http://127.0.0.1:8000/
```

OpenAI 兼容地址：

```text
http://127.0.0.1:8000/v1
```

## 启动代理

Windows 启动脚本会在启动 Gemini 上游客户端前自动选择代理：

- 优先使用本项目实验代理 `http://127.0.0.1:17997`
- 如果 `17997` 没有监听，则回退到传统本机代理 `http://127.0.0.1:7897`
- 如果你在 `adapter_env.local.ps1` 里手动设置了 `$env:GEMINI_PROXY`，会优先尊重手动设置

这样关闭全局 VPN 时，`START_HERE.bat` 也能优先使用项目代理；项目代理没开时，仍兼容原来的 7897。

## 目录结构

```text
openai_adapter_server.py   FastAPI 主服务
START_HERE.bat             Windows 入口：启动服务并打开控制台
START_HERE.command         macOS 入口：启动服务并打开控制台

scripts/                   安装、Cookie、修复、导出脚本
examples/                  示例配置
docs/                      详细说明
tests/                     兼容自测与测试样例
src/gemini_webapi/         上游 Gemini WebAPI 基座代码
usage-sync/                多电脑用量同步日志
runtime/                   本机运行缓存和日志（自动生成，已忽略）
```

## 第一次使用

Windows：

```powershell
.\START_HERE.bat
```

macOS：

```bash
chmod +x ./START_HERE.command ./scripts/*.sh
./START_HERE.command
```

控制台打开后按页面按钮操作：

```text
入门引导
刷新登录凭据
快速测试
终端输出
```

## 详细文档

- [同事快速上手](docs/TEAM_QUICK_START.md)
- [macOS 快速上手](docs/MACOS_QUICK_START.md)
- [公司电脑部署说明](docs/公司电脑部署使用说明.md)
- [多电脑同步说明](docs/COMPANY_SYNC_README.md)
- [Codex 类能力测试清单](docs/CODEX_LIKE_TESTS.md)

## 不要提交

以下文件包含本机状态或敏感信息，默认已被 `.gitignore` 忽略：

```text
adapter_env.local.ps1
adapter_env.local.sh
gemini_cookies.local.json
runtime/
```
