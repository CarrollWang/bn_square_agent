# bn_square_agent 内容流水线升级实施方案

> 日期：2026-08-11
> 状态：待执行
> 执行方式：交给执行模型按批次实施，每批次独立可验收、独立提交

## 0. 背景与目标

本项目是"采集素材 → AI 改写 → 审核 → 多账号发布到币安广场"的自动化 agent。经与两个姊妹项目（套利雷达 arbitrage-radar、投资雷达 investment_radar_v2）及同类实践（X 文章《Codex全自动运营币安广场》及其 Content OS 工作台截图）对比，确定以下升级方向：

1. 修复风格档案死代码（profile graph 构建后无人调用，RAG 永远为空）
2. 引入投递状态机（解决发布超时重试导致重复发帖的风险）
3. 引入三级 Gate + 人工审核队列 + 定时发布队列 + 批准哈希 + 每日配额（产品核心，对标 Content OS）
4. 通知状态机（飞书/Webhook + 冷却去重，替代单一 SMTP）
5. 素材双键去重 + 优先级评分
6. "去 AI 味"二次改写 pass + 多候选生成

设计原则（从两个雷达项目继承，执行时必须遵守）：

- **fail-closed**：任何无法判定的状态默认阻断，绝不假设成功
- **幂等**：所有状态机终态重放直接返回旧结果
- **unknown 不等于 failed**：传输层超时等无法确认的结果进入"人工恢复"态，禁止自动重试
- **先富化后落库**：依赖历史统计的计算，必须在写入当前记录之前完成
- 参照截图（豪哥 Content OS）的产品形态：素材库 → 批量生成 → 集中审核 → 发布队列，每日配额可视

## 1. 代码现状速查（执行前必读）

### 1.1 关键文件

| 文件 | 职责 |
|---|---|
| `storage/database.py` | SQLite 全部表结构与查询，惰性幂等迁移（`_migrate_schema` L122） |
| `workflows/graphs.py` | LangGraph 两个图：profile graph（L57-97，**无人调用**）、content graph（L100-249） |
| `workflows/operator.py` | 多账号素材消费与发布编排，`run_pending_material_queue`（L375） |
| `models/schemas.py` | Pydantic schema：Candidate/CandidateSet/ContentReview/StyleProfile |
| `ai/agents.py` | AnalysisAgent / StyleProfileAgent / WriterAgent / ContentReviewAgent |
| `knowledge/style_rag.py` | Chroma RAG，`StyleRAG.rebuild` / `StyleRAG.search` |
| `publishing/publisher.py` | MCPPublisher.publish（L78）、PublishingService.publish_generated（L144） |
| `services.py` | `build_services()` 依赖组装（L42-87） |
| `webapp.py` | FastAPI 全部 API + 后台监控循环（约 1900 行） |
| `web/src/` | Vue3 + Element Plus 前端，`router.ts` 6 个页面 |

### 1.2 数据库迁移机制（必须遵守的既有模式）

- 无版本表。`init_schema`（L54）先建基础表，再调 `_migrate_schema`。
- 加新列：`if "col" not in _columns(conn, table): ALTER TABLE ... ADD COLUMN ...`
- 改 CHECK 约束：重建表（参照 `_ensure_material_items_source_fk` L402：`RENAME TO _old → CREATE 新表 → INSERT SELECT → DROP old`，前后切换 `PRAGMA foreign_keys`）
- 新表：`_migrate_schema` 内 `CREATE TABLE IF NOT EXISTS`

### 1.3 现有状态字段（批次 1/2 要改的核心）

- `generated_posts.status`：`CHECK(status IN ('pending','approved','rejected','failed'))`，另有 `publish_status TEXT DEFAULT 'not_published'`、`publish_json`、`published_at`
- `material_account_runs.status`：`CHECK(status IN ('published','failed','skipped'))`，UNIQUE(material_item_id, account_key)
- `PublishResult`（publisher.py L20）：`success: bool` + `result["outcome"]` 三态 `published/failed/unknown`；`httpx.TransportError → unknown`
- 队列查询 `list_material_queue_for_account`（database.py L712）：已排除 `error LIKE 'publish_outcome_unknown:%'` 的 run——这是 unknown 语义的雏形，批次 1 将其正式化

### 1.4 前端模式（新页面必须照抄）

- `api.ts`：原生 fetch，统一 `requestJson<T>`，导出单例 `api`
- `types.ts`：全部类型集中定义
- 页面模式（参照 `Sources.vue`）：`<script setup lang="ts">` + `el-card shadow="never"` + `el-table border stripe` + `el-tag` 状态徽标 + `ElMessage`/`ElMessageBox` + 操作后 `await loadXxx()` 刷新
- 路由：`router.ts` 懒加载注册；构建命令 `cd web && npm run build`，产物 `dist/` 由 FastAPI 静态托管

---

## 2. 批次 0：接通风格档案链路（修死代码）(已完成, 2026-08-11)

**问题**：`build_profile_graph` 在 `services.py:58` 构建后全项目无人调用；`author_profiles`/`style_profiles` 表和 `StyleRAG.rebuild` 都是通的，但 `source_posts(role='reference')` 无入库入口，content graph 的 `prepare` 节点永远落到 `default_style_profile`，`rag.search` 永远返回 `[]`。

**目标**：给每个账号提供"导入历史文章 → 逐篇分析 → 生成风格档案 → 重建 RAG"的完整通路。

### 2.1 后端

1. 新 API（webapp.py）：
   - `POST /api/accounts/{account_key}/reference-posts`：body `{posts: [{title?, content, url?, source_created_at?}]}`，逐条调 `db.add_source_post(role='reference', account_key=...)`（hash 去重由表 UNIQUE 保证，重复跳过并计数）。返回 `{added, duplicated}`。
   - `POST /api/accounts/{account_key}/profile/build`：在线程池执行 `services.profile_graph.invoke({"account_key": ...})`，返回 `{analyzed_count, failed_count, source_count}`。加 `job_locks` 锁（job_name=`profile_build:{account_key}`，复用现有锁机制），防并发重建。
   - `GET /api/accounts/{account_key}/profile`：返回 `style_profiles` 表内容 + `source_posts(role='reference')` 计数 + 各 `analysis_status` 分布。
2. 注意：profile graph 的 `analyze_posts` 节点消费 `db.pending_reference_posts(account_key)`，失败会写 `post_analysis.error` 并计数——已有逻辑，不需要改 graph 本身。

### 2.2 前端

- Accounts 页每个账号行增加"风格档案"按钮，弹 `el-drawer`：
  - 当前档案 JSON 摘要展示（persona/tone/favorite_topics 等字段人性化渲染，不要直接甩 JSON）
  - 参考文章列表（计数 + 分析状态分布）
  - 文本域批量粘贴导入（每行/每篇分隔约定：`---` 分隔多篇）+ "导入"按钮
  - "重新构建档案"按钮（调 build 接口，loading 态，完成后刷新）
- `api.ts` + `types.ts` 同步加类型。

### 2.3 验收

- 对测试账号导入 5 篇文章 → build → `GET profile` 返回非默认档案；随后手动 `POST /api/run` 生成的稿件，content graph 日志中 `prepare` 节点拿到非默认 profile 且 `similar_analyses` 非空（可在 save 节点的 review_json 或日志中验证）。

---

## 3. 批次 1：投递状态机（防重复发帖）(已完成, 2026-08-11)

**参照**：`investment_radar_v2/delivery.py` 五态模型。

**问题**：当前 `material_account_runs.status='failed'` 与 `error 前缀 'publish_outcome_unknown:'` 混用表达"结果未知"，语义靠字符串前缀约定，脆弱；`generated_posts.publish_status` 是自由文本。

### 3.1 状态定义

`generated_posts.publish_status` 收敛为五态（写常量，禁止散落字符串）：

| 状态 | 含义 | 迁移来源 |
|---|---|---|
| `not_published` | 未进入发布流程 | 现有默认 |
| `queued` | 已入定时发布队列（批次 2 使用，本批次先定义） | 新增 |
| `published` | 确认发布成功 | outcome=published |
| `failed_retryable` | 明确失败，可重试 | outcome=failed |
| `unknown_manual_recovery` | 传输异常结果未知，**禁止自动重试**，需人工确认 | outcome=unknown |

`material_account_runs.status` 同步扩展为：`published / failed / skipped / unknown`（CHECK 约束需重建表迁移，INSERT SELECT 时把 `error LIKE 'publish_outcome_unknown:%'` 的 failed 行改写为 `unknown`，并剥掉 error 前缀）。

新建 `core/delivery.py`：定义状态常量与纯函数 `classify_publish_outcome(result: dict) -> str`，输入 PublishingService 的 result dict，输出五态之一。 PublishingService/operator 统一走这个函数，删除散落的 outcome 判断。

### 3.2 行为变更

1. `PublishingService.publish_generated`：
   - 幂等扩展：`publish_status IN ('published','queued','unknown_manual_recovery')` 时直接返回不重发（unknown 返回 success=False + 明确 error 文案"状态未知需人工确认"）。
   - 落库状态由 `classify_publish_outcome` 决定。
2. `operator._generate_for_account`：发布结果按新状态写 run；unknown → run.status='unknown'，error 不再加前缀。
3. `database.list_material_queue_for_account`：排除条件从 error 前缀改为 `run.status='unknown'`（迁移后旧数据已转 status，过渡期保留前缀排除作为兜底，注释说明）。
4. `_update_publish_failure_guard`：unknown 计入失败统计（熔断宁严勿宽），但告警文案区分"确认失败"与"结果未知"。

### 3.3 UI 与 API

- History 页发布状态筛选与 `el-tag` 增加 `unknown` 态（warning 色，文案"结果未知"）。
- 新 API `POST /api/history/runs/{run_id}/resolve`：body `{resolution: 'published'|'failed'}`，人工确认后修正 run 与 generated_posts 状态（unknown → published/failed_retryable），记录操作时间与 resolution 到 publish_json。History 页 unknown 行显示"标记已发布 / 标记失败"按钮。

### 3.4 验收

- 单测：`classify_publish_outcome` 三态；unknown 的 run 不再进入队列重试；resolve 接口状态修正正确。
- 迁移幂等：二次启动不重复重建表。

---

## 4. 批次 2：三级 Gate + 审核队列 + 定时发布队列 + 批准哈希 + 每日配额

**本批次是产品核心**，对标 Content OS 的"集中审核 → 发布队列"形态。

### 4.1 三级 Gate（参照 arbitrage-radar `engine.py: classify_gate_status`）

content graph 的 `save` 节点产出改为三级：

| Gate | 触发条件 | 结果 status |
|---|---|---|
| `ok` | review.passed 且无证据缺口 | `approved`（若账号关闭人工审核）或 `pending_review`（默认） |
| `manual_review` | review.passed 但存在证据缺口：缺 url 来源、打标方向缺失（tag 无 long/short）、素材来源已禁用、走势图截图失败 | `pending_review` |
| `blocked` | review 不通过且重写次数耗尽 | `failed` |

实现：
- `models/schemas.py` 新增 `GateStatus` Literal 与 `GateDecision {status, reasons: list[str]}`；reason 用机器可读 code（`source_url_missing` / `direction_untagged` / `chart_image_failed` / `review_threshold_failed` …），UI 映射中文文案。
- graphs.py `save` 节点：选最优候选后计算 GateDecision，写入 `generated_posts.review_json`（新增 `gate` 字段）与 status。
- 账号级开关：accounts 表加列 `require_manual_review INTEGER NOT NULL DEFAULT 1`。为 1 时即使 gate=ok 也进 `pending_review`。

### 4.2 批准哈希（参照 investment_radar `approval.py`）

- `generated_posts` 加列 `approval_hash TEXT`。
- 人工"通过"或自动 approve 时：对 `(account_key, content, material 版本)` 做规范化 SHA-256（`core/delivery.py` 加 `content_fingerprint(account_key, content)`：strip + 统一换行后 sha256 hexdigest），写入 approval_hash。
- `PublishingService.publish_generated` 发布**前**重算指纹并与 approval_hash 比对，不一致直接拒绝（fail-closed，error `content_modified_after_approval`）。

### 4.3 定时发布队列

- `generated_posts` 加列 `scheduled_at TEXT`（ISO 时间，可空）。审核通过时按策略计算：账号级 `min_interval_minutes`（app_settings，默认 20 分钟）+ 随机抖动 0-10 分钟，排在该账号上一篇 scheduled/published 之后。
- publish_status='queued' 表示在队列中。
- webapp.py 后台循环新增阶段"发布队列"（在监控循环内，`run_material_monitor_once` 末尾）：查 `publish_status='queued' AND scheduled_at <= now`，逐条调 `PublishingService.publish_generated`。**单条失败不阻断后续**（参照套利雷达 gather(return_exceptions=True) 思想）。
- 每日配额：app_settings `PUBLISH_DAILY_QUOTA_PER_ACCOUNT`（默认 30）。发布前查该账号当日 `published_at` 计数，达配额则该账号今日不再出队（不标失败，留到明天）。Dashboard 显示"今日已发布 X / 配额"。
- 注意时区：统一用本地时间存 ISO（与现有 created_at 一致），配额按自然日。

### 4.4 operator 流程调整

- `_generate_for_account`：auto_publish 开启时不再直接发布，而是 gate → approved/queued 入库；`AccountContentRun.status` 新增 `pending_review`/`queued`。
- `_save_material_run`：pending_review/queued 的素材写 run status='skipped'（error='等待人工审核'/'等待定时发布'）还是保持不写——**决策：保持不写 run**，由 `_finalize_material_item` 标素材为 new 并 error='等待审核/发布'，避免素材被误判 used。**但**要防同一素材每轮重复生成：在 `list_material_queue_for_account` 增加排除——该素材+账号已存在 `generated_posts.status IN ('pending_review','approved') AND publish_status IN ('not_published','queued')` 的记录。
- 人工驳回后素材应可重新生成：驳回接口把 generated_posts.status='rejected'，run 不动，素材回 new。

### 4.5 新 API

- `GET /api/review/items?account_key=&status=pending_review`：待审核列表（含素材标题、稿件内容、gate reasons、审核四维分）。
- `POST /api/review/items/{generated_id}/approve`：计算 approval_hash → status='approved' → 计算 scheduled_at → publish_status='queued'。
- `POST /api/review/items/{generated_id}/reject`：status='rejected'，可带 comment 写入 review_json。
- `POST /api/review/items/{generated_id}/edit`：人工修改正文（更新 content，重算指纹在 approve 时做；编辑后必须重新 approve）。
- `POST /api/review/batch-approve`：批量通过（逐条算指纹，单条失败不影响其他）。
- `GET /api/publish-queue?account_key=`：队列列表（queued 项 + scheduled_at + 当日配额用量）。
- `POST /api/publish-queue/{generated_id}/cancel`：移出队列（publish_status 回 not_published，status 回 approved）。
- `POST /api/publish-queue/{generated_id}/publish-now`：立即发布（仍走 approval_hash 校验与配额检查，配额可传 `ignore_quota=true` 由人工强制）。
- `GET /api/dashboard/quota`：各账号今日已发布/配额。

### 4.6 新前端页面

- **Review.vue（待审核）**：表格列：账号、素材标题、稿件预览（el-popover 或展开行显示全文）、四维分（el-tag）、gate reasons（warning tag）、操作（通过/驳回/编辑 el-dialog 文本域）。顶部批量通过。
- **Queue.vue（待发布）**：表格列：账号、稿件预览、scheduled_at、当日配额进度（el-progress）、操作（立即发布/移出队列）。
- router.ts 注册 `/review`、`/queue`；App.vue 导航加入口；Dashboard 加配额卡片。
- types.ts / api.ts 同步。

### 4.7 验收

- 全流程：导入素材 → 生成 → 默认进待审核 → 人工通过 → 进队列到点自动发布 → History 可见 published。
- 篡改测试：approve 后直接改 DB 的 content → publish 被拒，error=content_modified_after_approval。
- 配额测试：配额设 1，第二篇不出队，次日（或 mock 时间）恢复。
- 现有测试不回归：`python -m unittest discover tests` 全绿。

---

## 5. 批次 3：通知状态机（飞书/Webhook + 冷却去重）

**参照**：arbitrage-radar `services/alerts.py`。

1. 新表 `notification_events`（id, event_key, channel, payload_json, status, error, created_at）与 `notification_states`（event_key PK, last_sent_at, last_status, last_error, silent_baseline_done）。
2. 新模块 `services/notify.py`：
   - Notifier 协议：`send(title, markdown_body) -> None`；实现 `FeishuWebhookNotifier`（自定义机器人 webhook，比套利雷达的应用机器人简单，settings 加 `FEISHU_WEBHOOK_URL` 密钥项）与现有 SMTP notifier 并存，多渠道 fan-out。
   - `notify_event(event_key, title, body, *, cooldown_seconds=21600)`：首次启用静默建基线（只落状态不发送）；冷却期内去重；失败记 last_error 下轮重试但不抛异常阻塞主流程；全部事件落库。
3. 接入事件：连续发布失败熔断（替换现有 `_send_publish_failure_alert_email` 内部实现，保留 SMTP 渠道）、账号 cookie 检测失效、待审核积压超阈值（如 >20 条，每 6h 提醒一次）、每日发布摘要（配额使用、成功/失败/unknown 计数，每天一次）。
4. API `GET /api/notify/status`：渠道健康（disabled/degraded/ok）+ 最近事件，不泄露 webhook/密码。
5. Settings 页加飞书 webhook 配置与测试按钮。

**验收**：配置假 webhook（本地 httpbin 或 mock server）触发熔断事件，6h 内重复触发只发一次，事件落库。

---

## 6. 批次 4：素材双键去重 + 优先级评分

**参照**：investment_radar `contract.py: lead_dedup_keys` 与 `topic.py: positioning_score`。

1. `material_items` 加列 `title_hash TEXT`、`priority INTEGER NOT NULL DEFAULT 0`。
2. 双键：入库时除现有 content hash 外，计算 `title_hash = sha256(去标点小写标题 + 来源日期)`；新素材 title_hash 与近 7 天素材命中则标 `status='ignored'`, error='duplicate_title'。迁移时为存量数据补算 title_hash。
3. 优先级：在 `MaterialTagger` 打标后计算 score：方向明确（long/short）+30、有 symbol +20、来源权重（app_settings 每源可调）+0~30、时效（<1h +20，<6h +10）；写 priority。
4. `list_material_queue_for_account` 排序改为 `priority DESC, COALESCE(source_created_at, created_at) ASC`。

**验收**：同标题跨源转载只入一条；高 priority 素材优先被消费（单测构造队列断言顺序）。

---

## 7. 批次 5：去 AI 味二次改写 + 多候选

**参照**：investment_radar `draft.py` 两段式（起草高温 → 去味低温）。

1. `ai/agents.py` 新增 `HumanizeAgent.polish(content, profile) -> Candidate`：system prompt 要点——保持全部事实/币种/方向不变；拆二元对仗与排比壳；删"值得注意的是/综上所述/总的来说"等讲义词；删 emoji 堆砌；句子长短交错；temperature 用低温（0.3-0.4，StructuredLLM 需支持 per-call temperature 参数，没有则加）。
2. content graph 在 review 通过后、save 前插入 `polish` 节点（仅对将通过的候选执行，失败静默保留原稿——polish 是增强不是门）。polish 后再跑一次轻量 review？**决策：不跑全量 review，只做事实锚点校验**（正则确认 symbol、方向词、数字在 polish 后仍存在，缺失则弃用 polish 结果）。
3. 多候选：WriterAgent.generate 的 prompt 放开 candidate_index 1-3（删掉"固定为 1"约束），一次生成 2 条候选，review/save 现有选优逻辑天然支持。app_settings `WRITER_CANDIDATE_COUNT`（1-3，默认 2，成本控制）。

**验收**：同素材生成 2 候选，save 选优正常；polish 后 symbol 缺失时回退原稿。

---

## 8. 后续批次（不在本次执行范围，仅记录方向）

- 效果回填：抓取已发布文章阅读/点赞数据入 `post_metrics` 表，Performance 页按素材/账号归因；长期对齐"佣金收益"KPI（参照 X 文章：24 万浏览 0 佣金的教训，浏览量不是目标）。
- 新素材源：币安官方公告 RSS、项目方动态（X 文章验证有效的"一手动态+解读"打法）；复用 investment_radar_sources 的 RSS/JSON 双形态适配器模式与 source_health 三态。
- 配置启动强校验（参照 arbitrage-radar config.py `__post_init__` fail-fast）。
- 逐源/逐账号四级健康端点 `/api/health` 扩展。
- 安全文件 I/O 收拢（参照 lite_io.py 原子写/0600/symlink 防御）。

---

## 9. 全局执行要求

1. **每批次独立分支/提交**，提交信息结尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`（按用户要求再实际提交，未要求不提交）。
2. 每批次完成后运行 `python -m unittest discover tests` 与 `cd web && npm run build`，全部通过才算完成。
3. 新功能必须有单测：状态机转换、迁移幂等、Gate 分类、指纹校验、配额计数是硬性测试点；测试风格参照现有 `tests/`（unittest，不用 pytest）。
4. 不改根目录的旧版扁平 re-export 壳文件（agents.py/db.py/...），只改包内真实代码。
5. 数据库变更全部走 `_migrate_schema` 惰性迁移模式，禁止手工 SQL 脚本。
6. 所有密钥类新配置项加入 `SECRET_APP_SETTING_KEYS`，落库加密、API 返回打码。
7. UI 文案用中文，状态用 el-tag 颜色语义：成功 success / 警告 warning / 危险 danger / 信息 info。
8. 每个批次完成后更新本文件，在对应批次标题后标注 `(已完成, <日期>)`。
