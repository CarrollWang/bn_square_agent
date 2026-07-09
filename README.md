# BN Square Agent

BN Square Agent 是一个本地自动运营控制台，用来采集 Binance Square 作者文章，自动打标、改写、配走势图，并通过你自己的 MCP 发布到 Binance Square。

## 功能

- 本地 FastAPI 服务，Vue 前端源码放在 `web/`，构建产物输出到 `dist/`
- 多账号 Cookie 管理，数据保存到本地 SQLite
- 监控 Binance Square 作者主页并采集文章
- 素材入库、打标、过期清理
- 后台自动循环运行，也支持前端手动立即运行
- 每个账号生成不同终稿
- LLM 自动审核与重写
- DashScope Embedding + Chroma 风格检索
- Playwright 自动截取 Binance 合约走势图
- 通过你自己的 MCP 工具 `publish_binance_square` 发布文章
- 支持账号级独立代理 / 独立 MCP 地址，便于多账号隔离运行

## 安全说明

不要提交运行数据和密钥。仓库已忽略：

- `.env`
- `data/`
- `chroma_db/`
- 本地 agent / 工具缓存目录

Cookie、API Key、生成稿、采集样本都只应该保存在本地数据库或本地配置中，不要提交到 GitHub。

现在数据库里的这些字段会自动加密保存：

- 账号 Cookie
- 账号代理地址 `proxy_url`（包括可能包含的认证信息）
- 账号级 `mcp_auth_token`
- 全局 `LLM_API_KEY`、`DASHSCOPE_API_KEY`、`MCP_AUTH_TOKEN`、`SMTP_PASSWORD`

默认会在 `SECRET_KEY_PATH` 指向的位置生成主密钥文件；如果你更希望自己管理，也可以直接设置 `APP_SECRET_KEY`。
迁移或备份服务器时，数据库和这个主密钥必须一起保留，否则历史密文无法解开。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

可以复制 `.env.example` 到 `.env` 使用文件配置，也可以直接在网页控制台里保存配置。

```powershell
copy .env.example .env
```

如果不想手工生成主密钥，可以直接留空 `APP_SECRET_KEY`，程序会在 `SECRET_KEY_PATH` 自动生成一个本地主密钥文件。

## 启动

首次构建前端：

```powershell
cd web
pnpm install
pnpm build
cd ..
```

```powershell
python -B run.py serve --host 127.0.0.1 --port 8787
```

打开：

```text
http://127.0.0.1:8787/
```

Windows 下如果 Playwright 采集或截图报 `WinError 5`，需要用更高权限启动服务。

如果你把整套主程序部署到服务器，启动时通常改成：

```powershell
python -B run.py serve --host 0.0.0.0 --port 8787
```

这时你可以在自己本地浏览器直接打开：

```text
http://你的服务器IP:8787/
```

更推荐用 Nginx / Caddy 反代成你自己的域名并启用 HTTPS。
如果你只把 `serve-mcp` 部署到服务器，那么本地网页控制台不会自动跟过去；这种情况下是“本地控制台 + 远程 MCP”模式。

### 推荐公网部署方式

如果你是“自用 + 公网服务器”，更推荐这套拓扑：

```text
浏览器 -> Nginx(443, Basic Auth, HTTPS) -> Web 管理台(127.0.0.1:8787)
                                      └-> MCP 服务(127.0.0.1:8788, 仅本机可见)
```

也就是：

- Web 管理台只监听 `127.0.0.1:8787`
- MCP 服务只监听 `127.0.0.1:8788`
- Nginx 作为唯一公网入口
- 主程序里的 `MCP_URL` 直接填 `http://127.0.0.1:8788/mcp`

仓库里已经放了 Nginx 样板：

- [deploy/nginx/bn-square-agent-web.conf.example](/Users/Admin/Documents/bn_square_agent/deploy/nginx/bn-square-agent-web.conf.example)
- [deploy/nginx/bn-square-agent-mcp.conf.example](/Users/Admin/Documents/bn_square_agent/deploy/nginx/bn-square-agent-mcp.conf.example)
- [deploy/nginx/README.md](/Users/Admin/Documents/bn_square_agent/deploy/nginx/README.md)

## 自建 MCP 发布服务

项目内置了一个可单独部署的 HTTP MCP 服务，接口兼容当前客户端使用方式：

```powershell
python -B run.py serve-mcp --host 0.0.0.0 --port 8788
```

启动后发布地址通常是：

```text
http://127.0.0.1:8788/mcp
```

建议至少配置这些环境变量：

```text
MCP_SERVER_AUTH_TOKEN=your-secret-token
MCP_SERVER_DEFAULT_PROXY=
MCP_SERVER_DEBUG_DIR=./data/mcp_debug
MCP_SERVER_PUBLISH_WAIT_MS=12000
```

然后在主程序里这样填：

- `MCP_URL=http://你的服务器:8788/mcp`
- `MCP_AUTH_TOKEN=your-secret-token`
- 如果某个账号需要独立出口 IP，可以在账号管理里单独填写 `proxy_url`

### 自建 MCP 特性

- 工具名保持为 `publish_binance_square`
- 支持 `cookie`、`content`
- 可选支持 `coins`、`image_base64`、`proxy_url`
- 发布过程使用 Playwright 本地浏览器自动化，不再依赖第三方黑盒 MCP
- 发布前会等待按钮进入可点击状态，发布后会结合页面提示、网络响应和编辑器状态判定结果
- 发布失败或结果不确定时，会在 `MCP_SERVER_DEBUG_DIR` 下保存调试包

调试包里会包含：

- `page.png`：完整截图
- `page.html`：当时的页面 HTML
- `diagnostics.json`：最近网络响应、请求失败、控制台日志、页面异常

### 部署建议

- 用 `systemd` 或 `supervisor` 常驻运行 `python -B run.py serve-mcp`
- 外层用 Nginx/Caddy 反代到 `/mcp`
- 强烈建议开启 HTTPS，并配置 `MCP_SERVER_AUTH_TOKEN`
- 如果你要做多账号 IP 隔离，可以给每个账号单独配置 `proxy_url`，或者给不同账号指向不同的自建 MCP 地址

## 网页配置

网页控制台会把这些配置保存到 SQLite：

- LLM API Key、Base URL、模型名
- DashScope API Key、Embedding 模型
- 默认 MCP 地址、发布工具、访问 Token
- 账号级独立 MCP 地址 / 独立代理配置
- 自动循环、自动发布、自动消费素材
- 采集间隔、成功后间隔、失败重试间隔、素材有效期

LLM 和 Embedding 有独立测试按钮，方便分别确认连接是否正常。

## 自动运行流程

1. 在账号管理里添加 Binance Cookie。
2. 在素材中心添加 Binance Square 作者主页链接。
3. 后台循环按配置间隔采集新文章。
4. 素材源文章进入本地素材库。
5. 打标器识别币种、方向、合约符号。
6. 过期素材会按 TTL 自动失效。
7. 自动消费器从可用素材中取一条。
8. Writer Agent 改写成账号对应的终稿。
9. Review Agent 自动审核，不合格则重写。
10. 发布前自动匹配合约图和 `coins` 参数。
11. 你的 MCP 使用 Cookie 发布文章。

## 前端页面

前端采用 Vue3/Vite/TypeScript 管理后台布局：

- 自动运行：查看状态、启动/暂停循环、立即运行、检查发布通道
- 账号管理：保存账号 Cookie，配置独立代理 / 独立 MCP
- 素材中心：管理采集源、查看素材库
- 系统设置：配置 LLM、Embedding、自动运行参数

## 项目结构

```text
ai/           LLM Agent、改写、审核、打标
core/         配置与环境变量
web/          Vue3/Vite 前端源码
dist/         前端构建产物，由 FastAPI 托管
knowledge/    Chroma / Embedding 风格检索
models/       Pydantic 数据结构
publishing/   MCP 发布、走势图截图、账号检测、账号隔离出网
sources/      素材源采集
storage/      SQLite 持久化
workflows/    LangGraph 工作流和自动运营编排
```
