# BN Square Agent

BN Square Agent 是一个本地多账号内容运营控制台，覆盖素材采集、风格档案、生成审核、发布队列、账号轮转、表现统计和发文历史。

## 当前状态

内容管线可用，正式发布适配器迁移中。

- 已完成：多来源素材、素材质量门禁、账号风格档案、人工审核、定时队列、幂等投递状态、账号频控和运营看板。
- 当前冻结：Remote MCP、自建 HTTP MCP 传 Cookie/代理、服务器常驻 Playwright。
- 不采用：仅用 Binance Square OpenAPI 作为正式发布链，因为它无法插入所需的完整交易组件。
- 目标方案：Windows 本机读取 Cookie / Browser Profile，每次获取 nonce，以 `HMAC-SHA256(squareUid, nonce)` 签名后调用 Binance Square Web 私有接口。

架构依据见 [ADR-0001](docs/architecture/0001-local-private-web-publisher.md)。在真实格式验收前，自动发布默认关闭。

## 安全边界

- Cookie、Browser Profile、代理、signature key、API Key 和运行数据库不得提交到 GitHub。
- Cookie、代理和 signature key 不得进入 MCP 参数、普通 API 响应或日志。
- `.env`、`data/`、`chroma_db/` 和本地工具缓存已加入忽略规则。
- 遗留 MCP 发布器只有显式设置 `ALLOW_LEGACY_MCP_PUBLISH=1` 才能发送；这只用于迁移验证，不是正式部署方式。
- 测试和开发默认只生成、审核和排队，不发送真实帖子。

## 安装

建议在 Windows 主机使用 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cd web
npm ci
npm run build
cd ..
copy .env.example .env
```

## 启动本地工作台

```powershell
python -B run.py serve --host 127.0.0.1 --port 8787
```

打开 `http://127.0.0.1:8787/`。

管理台默认只监听本机。若确需局域网访问，必须先配置 `WEB_AUTH_USERNAME` 和 `WEB_AUTH_PASSWORD`；不再推荐把发布服务部署为公网 Remote MCP。

## 推荐工作流

1. 添加账号，并在本机维护账号凭据。
2. 导入代表作，构建账号风格档案。
3. 启用新闻、官方公告、链上数据和行情类素材源。
4. 素材经过时效、方向、质量和重复门禁。
5. Writer / Review Agent 生成并审核候选稿。
6. 人工在审核台批准，稿件进入发布队列。
7. 正式 LocalWebPublisher 完成验收前，由人工完成发布或仅保留队列。

交易帖格式验收包含三层：正文涉及的每个有效现货币种使用 `$TOKEN`；主交易对象额外附加 `{future}(TOKENUSDT)`；同时出现主合约 K 线图。

## 配置

复制 `.env.example` 后配置 LLM、Embedding 和本地数据库路径。关键默认值：

```text
AUTO_PUBLISH=0
ALLOW_LEGACY_MCP_PUBLISH=0
AUTO_CONSUME_MATERIALS=1
```

`MCP_URL`、`MCP_AUTH_TOKEN` 和账号级 `mcp_url` 目前只为迁移兼容保留，不应作为新部署配置。

## 验证

从项目父目录运行后端测试：

```bash
bn_square_agent/.venv/bin/python -m unittest discover -s bn_square_agent/tests -v
```

构建前端：

```bash
npm --prefix bn_square_agent/web run build
```

## 项目结构

```text
ai/           LLM Agent、改写、审核、打标
core/         配置、安全边界与投递状态
docs/         架构决策和实施计划
knowledge/    风格检索
models/       数据模型
publishing/   发布适配器；Remote MCP 当前为 legacy
sources/      素材源采集
storage/      SQLite 持久化
web/          Vue 3 / Vite 前端
workflows/    内容工作流和自动运营编排
```

## 下一阶段

下一阶段不是继续加素材源或扩远程部署，而是基于测试账号的脱敏流量契约实现 `LocalWebPublisher`，补齐 nonce 单次使用、HMAC 签名、敏感信息不落日志和 Windows 本机真实格式验收。
