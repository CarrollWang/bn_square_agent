# BN Square Agent Web

这是 BN Square Agent 的 Vue3/Vite 管理后台前端工程，按 Vben 风格的左侧菜单、顶栏、内容页结构组织。

## 技术栈

- Vue 3
- Vite
- TypeScript
- Vue Router
- Pinia
- Element Plus

## 命令

```bash
pnpm install
pnpm dev
pnpm build
```

`pnpm build` 会把构建产物输出到项目根目录的 `dist/`，由 FastAPI 继续托管。

## 页面

- 自动运行：自动循环状态、运行日志、MCP 检查
- 账号管理：Square OpenAPI Key 加密保存、独立代理/MCP 配置、删除
- 素材中心：统一的新闻源配置和新闻素材库；不再把 Binance Square 大 V 主页作为自动素材源
- 系统设置：大模型设置、邮箱预警设置、自动运行设置
