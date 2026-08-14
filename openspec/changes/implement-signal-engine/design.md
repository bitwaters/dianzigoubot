# 设计：信号引擎与插件的实施

## Context

四份开发文档已完成并锁定契约（见 proposal.md）。仓库当前只有文档与 openspec 规格，无任何代码；实施从零开始。交付节奏为三阶段串行（核心 → SDK+交易插件 → 波段插件），阶段间有验收闸门，不并行开工。

## Goals / Non-Goals

**Goals:**
- 实现与开发文档逐条对应的可运行系统，验收以文档第 13 章清单为唯一标准
- 核心规则（评分、安全、防追高、回测）为纯函数，可独立测试
- 插件与核心只有事件/通知/命令三个接口面

**Non-Goals:**
- 不使用 ORM；不引入文档之外的框架（Flask/gRPC/Celery 等）
- 不实现 LOCAL_SMART v2 机制（规范保留，特征权重固定 `"0"`）
- 不做插件热加载、插件间通信、多进程隔离
- 不做 Web 界面；交互只有 Telegram

## Decisions

### D1. 代码组织与模块边界

目录严格按总控文档第 12.1 节。关键边界：

- `clients/` 是唯一接触供应商原始响应的层，输出归一化模型；业务层不 import 供应商字段名。
- `services/strategy.py` 等核心规则为纯函数（输入快照 → 输出决策），不持状态、不调 IO。
- `storage/repository.py` 集中全部 SQL；表结构由 `storage/migrations/` 序号迁移定义。
- `bus/` 只提供三接口；插件不 import `services/`、`clients/`、`storage/`。

备选：按领域拆多个包（过度工程，违背"简单易维护"）；被否。

### D2. 数据库与迁移

- 首个迁移 `001_init.sql` 建总控文档第 9.3 节全部核心表；后续变更用递增序号 SQL。
- 启动时在 `PRAGMA` 设置后执行未应用迁移（单连接串行，无并发迁移）。
- 写入走 `storage_task` 的 PriorityQueue（策略事务 > 信号与安全快照 > 候选行情），队列水位触发发现/订阅暂停（文档第 9.2 节）。
- 插件迁移由插件自己的 `migrations/` 目录管理，核心不触碰（SDK 规范第 6 章）。

### D3. 供应商 client 与契约锁定

- CoinGecko REST：httpx.AsyncClient，`x-cg-pro-api-key` 头；统一超时、错误分类（超时/限流/契约错误）。
- 归一化模型用 pydantic；金额/比例 `Decimal`，时间 UTC 毫秒。
- 契约测试用固定 fixture（录制响应 + 元数据规则）；首次接入前生成带 Python 版本/脚本版本/UTC 时间的探测报告；缺元数据的旧报告一律按 `LEGACY_EVIDENCE` 处理（文档第 3.1 节）。
- GoPlus：无 Authorization，30 次/分钟客户端限流。
- 供应商字段变更遵循"先改文档再改代码"（文档第 1.1 节）。

### D4. WebSocket 实现

- `websockets` 库两条独立连接（Solana/BSC），ActionCable 报文按文档第 4.5 节锁定。
- 断线退避 `1/2/4/8/15/30` 秒 + 20% 抖动；鉴权失败停止该链重连并告警。
- G3 1 秒 K 线内存聚合为 W5/W10 窗口；final 判定按"下一秒事件或 open+2 秒"。
- 订阅准入：物理 100 上限 + 每日 WS 预算（`daily_credit_budget`）；计费响应本地估算，重启恢复当日计数（文档第 4.6 节简化版）。

### D5. 事件总线实现

- 进程内 registry：`plugins/<name>/register.py` 暴露 `register(app)`；`config/plugins.yaml` 控制启用与顺序。
- 分发：内存队列按序 fan-out，每个插件 handler 用 `asyncio.wait(..., timeout=dispatch_timeout_seconds)` 包裹；超时/异常计熔断（默认阈值 10），熔断后停止分发并告警，重启解除。
- 至少一次语义：信号落库与事件入队同事务；重启后按 `signals` 表状态补发未发布事件；插件以 `event_id` 幂等。

### D6. strategy.yaml Schema

- Pydantic v2 严格模式（extra=forbid），全字段按文档第 6 章；金额/百分比为引号 Decimal 字符串。
- canonical JSON（键字典序）与 `strategy_hash=SHA-256`；激活时写 `strategy_snapshots` + `strategy_activations`。
- Schema 拒绝任何执行/风险/晋升字段（验收第 9 条）。

### D7. 回测引擎

- 纯本地计算：数据准备（OHLCV 回补计划 + 管理员确认）→ 断开外部 API → 1m 成交模型回放 → 报告（文档第 10 章）。
- Pool OHLCV 分页 UTC 秒规则、`synthetic` 条隔离、`NOT_FILLED/DATA_GAP` 语义、run_id 确定性全部按文档实现。
- 回测配置独立于实盘参数（`backtest_model` 字段）。

### D8. Telegram

- python-telegram-bot 长轮询；`telegram_updates` 持久化 offset、`telegram_outbox` 投递幂等、`telegram_confirmations` 一次性 nonce，全部按文档第 8.3 节。
- 命令路由表：核心命令 + 插件注册命令；冲突拒绝。

### D9. 分阶段交付与验收

| 阶段 | 范围 | 验收 |
|---|---|---|
| 1 | 核心信号引擎，**含插件总线全部实现**（SDK 规范第 2-7 章行为是核心侧代码，`app/core/bus/`，本阶段落地） | 文档第 13 章清单中核心条目 + `signal-core`/`plugin-sdk` spec 场景 |
| 2 | 交易插件 | `trade-plugin` spec 场景 + 交易插件文档第 10 章契约测试项 |
| 3 | 波段插件 | `swing-plugin` spec 场景 + 波段文档第 7 章契约测试项 |

阶段间闸门：上一阶段验收通过前，下一阶段任务不启动。

### D10. 测试策略

- 单元测试：纯函数与归一化规则（文档第 13.1 节清单逐条）。
- 集成测试：供应商契约 fixture 回放、WS 断线注入、Telegram update 序列回放、SQLite 恢复。
- 每个阶段结束运行文档第 13.3 节对应验收条件并记录结果。

## Risks / Trade-offs

- [风险] WS 每日预算为本地估算，供应商计费契约变化会失真 → 缓解：`/key` 每 15 分钟对账显示偏差并告警；契约变化先更新探测报告
- [风险] CoinGecko 契约漂移（文档以 2026 年 8 月官方文档为准）→ 缓解：契约测试先行，失败即阻断相关模块，不静默降级
- [风险] Telegram 长轮询恢复语义复杂（SENDING→DELIVERY_UNKNOWN）→ 缓解：outbox 状态机单独模块 + 恢复路径集成测试
- [风险] DBotX 待契约测试项（Simulator list_tasks 等）在插件阶段才验证 → 缓解：交易插件文档已标记待测项清单，实现顺序上契约测试先于业务代码
- [权衡] LOCAL_SMART 推迟 v2 使 v1 信号少一个维度 → 接受：TOKEN_PROFITABLE 先跑通闭环，v2 按文档机制补
- [权衡] 额度控制粗化后存在月中额度耗尽可能 → 接受：告警阈值 + WS 每日预算 + 管理员知情回补，换取实现复杂度减半

## Migration Plan

- 无存量数据迁移（首建库）；配置从零创建（strategy.yaml 基准文件随阶段 1 交付）。
- 阶段回滚：每阶段产出独立模块，禁用插件/回退策略快照即可回到上一阶段状态。

## Open Questions

- `simple token_price` 是否替换 Multiple Pools 做结果追踪（阶段 1 首个 spike 验证后决定）
- 交易插件 TP/SL 组默认参数（占位值待回测调参，不阻塞阶段 2）
