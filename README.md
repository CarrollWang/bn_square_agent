# BN Square Agent

BN Square Agent 是一个 Binance Square 多账号内容运营控制台，包含素材采集、LLM 改写与审核、账号轮转、自建 MCP、账号表现、发文历史和 TickTick 风格 Web UI。

发布链路只使用 Binance 官方 Square OpenAPI，不使用 Cookie、扫码登录或浏览器自动发布。

## 架构

```text
Web 管理台
→ 多账号配置 / 审核 / 轮转 / 定时 / 看板 / 历史回写
→ 自建 MCP: publish_binance_square
→ Binance Square OpenAPI
→ 帖子 ID 与公开 URL
```

自建 MCP 根据 `account_key` 从同机加密数据库读取对应账号的 Square OpenAPI Key。Key 不会出现在 MCP 参数、日志、API 响应或 Git 中。

## 功能

- 多账号独立 Square OpenAPI Key。
- 多账号独立代理、MCP 地址和 MCP Token。
- 素材源采集、去重、打标和队列消费。
- LLM 多候选生成、审核与重写。
- 账号轮转、失败重试、发布历史和表现看板。
- 文本发布与单图发布。
- 自建 MCP 服务，工具名固定为 `publish_binance_square`。

## Binance Square OpenAPI Key

在 [Binance Square Creator Center](https://www.binance.com/square/creator-center/home) 创建 Key，然后只通过 Web 账号管理页录入。

不要把 Key 写进：

- `.env` 的公开样例。
- MCP 调用参数。
- 命令行参数。
- 日志、聊天或 Git。

账号 Key、代理凭据和 MCP Token 使用应用密钥加密保存到 SQLite。请备份 `SECRET_KEY_PATH` 指向的密钥文件；丢失后已加密凭据无法恢复。

## 安装

要求 Python 3.11+ 和 Node.js 18+。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd web
npm install
npm run build
cd ..
```

Playwright 仍用于素材采集和行情截图，不参与登录或发布。如需这些能力：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

复制 `.env.example` 为 `.env`，至少配置 LLM、Embedding、自建 MCP 和安全项。账号 Square OpenAPI Key 在 Web 页面单独录入。

## 本地运行

启动 MCP：

```powershell
.\start-local-mcp.ps1
```

启动 Web：

```powershell
.\.venv\Scripts\python.exe -B run.py serve --host 127.0.0.1 --port 8787
```

访问：<http://127.0.0.1:8787/>

## MCP 工具

`publish_binance_square` 参数：

- `content`：必填，发布正文。
- `account_key`：必填，账号标识。
- `coins`：可选，仅用于结果记录。
- `image_base64`：可选，一张图片的 data URL 或纯 base64。

工具不接受 OpenAPI Key、Cookie 或代理参数。MCP 从加密数据库读取 Key 和账号代理。

## 发布结果

- OpenAPI 返回帖子 ID 或公开 URL：`outcome=published`。
- `/content/add` 返回 HTTP 504，或成功码但没有 ID/URL：`outcome=unknown`，不能记为最终成功。
- API 明确错误：`outcome=failed`。

第一条真实发布必须先人工确认正文，并以公开帖子 URL 或页面证据验收。

## 服务器部署

参考 `deploy/systemd/`：

- Web：`127.0.0.1:8787`
- MCP：`127.0.0.1:8788`
- 环境文件：`/etc/bn-square-agent/env`，建议权限 `600`
- 数据库、应用密钥和 Chroma 数据目录需要持久化

MCP 和 Web 应使用相同的 `DATABASE_PATH` 与 `SECRET_KEY_PATH`，否则 MCP 无法读取 Web 保存的账号 Key。

默认保持以下开关关闭，完成一条真实首帖后再逐级开启：

```text
AUTO_MONITOR_ENABLED=0
AUTO_CONSUME_MATERIALS=0
AUTO_PUBLISH=0
```

## 安全边界

- Web/MCP 默认只绑定 `127.0.0.1`。
- 非本机访问必须启用认证或使用 SSH/Tailscale 隧道。
- 不在日志中输出 Key、Token 或代理密码。
- 不把 HTTP 504 当作已验证发布成功。
