# 提案：实施信号引擎核心与插件（按开发文档建设系统）

## Why

四份开发文档（信号引擎总控、插件 SDK 规范、交易插件文档、波段插件文档）已完成架构锁定，项目从文档阶段进入实现阶段。按既定节奏先建主体（核心信号引擎）、插件后行、两阶段间有验收闸门，不并行开工。

## What Changes

- 新增 `app/core/` 信号引擎应用：按《信号引擎总控开发文档》实现发现、安检、评分、防追高、信号、Telegram 推送、结果追踪、回测、手动策略激活与插件总线。
- 新增 `config/strategy.yaml`（完整 Schema，按总控文档第 6 章）与 `config/plugins.yaml`。
- 新增 `plugins/trade/` 交易插件：DBotX Fast Swap（服务端 TP/SL 组）、WS 结果订阅记账、风险账本、对账与恢复、Telegram 命令。
- 新增 `plugins/swing/` 波段插件：DBotX Kline/Pair Info/WS 行情自取、Limit Order 服务端触发、循环状态机、资金隔离记账。
- 新增依赖：httpx、websockets、python-telegram-bot、aiosqlite、pydantic、pytest。
- 同步规格：`plugin-sdk` 规格补充事件投递语义（至少一次 + 插件幂等）与分发超时要求（开发文档已更新，规格同步落账）。
- **交付节奏**：分三阶段串行交付，阶段间有验收闸门——第一阶段核心信号引擎（插件总线与 SDK 规范行为作为核心侧代码一并落地），第二阶段交易插件，第三阶段波段插件；上一阶段验收通过前下一阶段不启动。

## Capabilities

### New Capabilities

无（四个能力规格已在 `openspec/specs/` 常驻，本次为实施，不新增行为契约）。

### Modified Capabilities

- `plugin-sdk`: 补充事件投递语义要求（至少一次投递、插件按 `event_id` 幂等、核心分发超时并计熔断），与已更新的开发文档对齐

## Impact

- 代码：仓库从零实现（此前无代码）；目录结构按总控文档第 12.1 节
- 数据库：`bot.sqlite` 首次建库，含总控文档第 9.3 节全部核心表与迁移体系
- 外部依赖：CoinGecko Analyst（REST+WS）、GoPlus、Telegram Bot、DBotX（仅插件）
- 凭证：核心只持 CoinGecko/Telegram；DBotX 凭证由插件自持
- 测试：pytest 单元/集成测试；供应商契约以带元数据的探测报告 + 契约测试锁定（LEGACY_EVIDENCE 规则）
- 部署：单 Docker 容器；插件与核心同进程
