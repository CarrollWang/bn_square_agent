# ADR-0001：Windows 本机 Web 私有接口发布器

- 状态：Accepted
- 日期：2026-08-12
- 决策来源：2026-07-12 用户纠正记录与 2026-08-12 全局收敛审计

## 背景

仓库当前 `main` 的内容管线已经具备多账号、风格档案、审核、队列、幂等投递和多来源采集，但发布仍依赖 Remote MCP：主程序把 Cookie、代理和正文作为 MCP 参数发送，自建 MCP 再用 Playwright 操作网页。README 同时推荐服务器常驻和公网部署。

另一个远端实验分支改为 Binance Square OpenAPI。该方案减少 Cookie 暴露，但已确认 OpenAPI 不能插入用户所需的完整合约交易组件，因此不能作为正式发布路径。

## 决策

正式发布路径统一为 Windows 本机发布适配器：

```text
内容管线 -> 人工审核/发布队列 -> LocalWebPublisher
                                  |- 本机 Cookie / Browser Profile
                                  |- 每次请求获取 nonce
                                  |- HMAC-SHA256(squareUid, nonce)
                                  `- Binance Square Web 私有接口
```

硬边界：

1. Cookie、Browser Profile、代理和 signature key 只存在于本机凭据层。
2. MCP 或普通 Web API 参数只允许传 `account_key`、已批准稿 ID 和非敏感发布选项。
3. Remote MCP 与服务器 Playwright 发布器进入 legacy 冻结状态，不再扩展。
4. OpenAPI 仅可作为不要求交易组件的受限适配器研究，不得静默降级使用。
5. 正式适配器完成前，`AUTO_PUBLISH` 默认关闭；legacy 发送还需显式设置 `ALLOW_LEGACY_MCP_PUBLISH=1`。

## 当前落地状态

本 ADR 本轮先完成安全收口，不伪造未知私有接口：

- 内容采集、生成、审核、队列继续运行。
- 自动发布与手动运行请求默认不发送。
- 遗留 MCP 发布器默认 fail closed，只有显式风险开关才能启用。
- 原 `serve-mcp` 代码暂留作迁移参考，不能视为推荐部署方式。

## 后续验收门槛

正式启用 LocalWebPublisher 前必须同时具备：

- 来自测试账号的脱敏 nonce 请求、签名输入和发布请求契约；
- nonce 单次使用、过期和重放拒绝测试；
- 日志/异常/数据库不泄露 Cookie、代理凭据和 signature key 的测试；
- Windows 本机实发验证：正文所有有效现货币种使用 `$TOKEN`，主交易对象附加 `{future}(TOKENUSDT)`，并出现主合约 K 线图；
- 人工确认自动发布开关后再启用，不允许由迁移脚本自动打开。

## 被否决的方案

- 公网 Remote MCP：凭据跨进程/跨网络传输，扩大暴露面。
- 服务器常驻 Playwright：与本地 Profile、私有接口签名和用户设备决策冲突。
- OpenAPI-only：不能满足完整交易组件格式。
- 浏览器自动化长期回退：失败实现会继续侵蚀产品架构，故只允许迁移期冻结参考。
