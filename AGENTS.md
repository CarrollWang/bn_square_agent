# BN Square Agent 协作规则

## 唯一产品目标

这是多账号 Binance Square 内容运营系统。保留素材采集、风格档案、生成审核、发布队列、账号轮转、表现统计和 TickTick 风格 Web 工作台。

## 发布架构硬约束

- 正式运行位置是用户的 Windows 主机，不是公网服务器。
- 发布凭据只保存在本机：账号 Cookie / Browser Profile、代理和 signature key 不得进入 MCP 参数、HTTP API 响应、日志或 Git。
- 正式发布适配器应在本机获取一次性 nonce，使用 `HMAC-SHA256(squareUid, nonce)` 生成签名，并调用经过真实流量验证的 Binance Square Web 私有接口。
- 第三方 Remote MCP、服务器常驻 Playwright、自建 HTTP MCP 传 Cookie/代理的链路均为 legacy，不得作为新功能基础，也不得默认开启。
- Binance Square OpenAPI 可保留为受限研究适配器，但在无法插入完整交易组件前不能替代正式 Web 发布链路。
- 在真实格式验收完成前，自动发布必须默认关闭；测试不得访问真实账号、发送真实帖子或读取真实凭据。

## 实施顺序

1. 保持内容管线与人工审核可用。
2. 把发布能力隔离为适配器，默认 fail closed。
3. 用脱敏抓包契约或本地 fixture 实现 nonce、签名与请求组装。
4. 在 Windows 本机用测试账号人工确认 Cashtag、主合约组件和 K 线图三层格式。
5. 只有验收证据写入 `docs/architecture/` 后，才允许开启自动发布。

## 修改要求

- 架构决策以 `docs/architecture/0001-local-private-web-publisher.md` 为准。
- 不要删除多账号、代理隔离、轮转、运营看板和发文历史等产品能力。
- 不得把失败的 Cookie/MCP/浏览器链路包装成兼容回退长期保留。
- 后端测试从项目父目录运行：`bn_square_agent/.venv/bin/python -m unittest discover -s bn_square_agent/tests -v`。
- 前端修改后运行：`npm --prefix bn_square_agent/web run build`。
