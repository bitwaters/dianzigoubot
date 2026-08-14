# 设计：核心信号引擎 + 插件架构重构

## Context

现状：唯一交付物是 1347 行的《SOL-BSC-TGBOT-总控开发文档》，定义了发现（CoinGecko Analyst）、安检（CoinGecko+GoPlus）、评分、防追高、Telegram 推送、DBotX 交易执行、回测的完整契约。动机见 proposal.md。

本次重构是**文档架构重构**：没有存量代码需要迁移，产出物是三分文档及其中锁定的接口契约。DBotX 官方文档调研结论（服务端 TP/SL 组、WS 结果推送、0 credit 交易 API、钱包资产自带 PnL）是删除执行机器的依据。

## Goals / Non-Goals

**Goals:**
- 核心文档只保留信号质量相关契约，可独立交付、独立测试
- 插件 SDK 契约足够稳定，交易插件与波段插件可独立开发、独立演进
- 交易插件的执行复杂度降为"信号→DBotX 任务→WS 事件记账"的薄适配层
- 回测引擎保留但不带自动晋升机器

**Non-Goals:**
- 不改动核心发现/安检/评分/防追高的现有契约内容（只改归属与过时声明）
- 不引入 DBotX 数据 API 到核心发现层
- 不实现插件热加载、插件间通信、多进程隔离
- 第一版不实现客户端指标引擎（波段插件用服务端触发）

## Decisions

### D1. 文档拆分：三份 + 归属映射

| 新文档 | 接收自现文档 | 明确删除 |
|---|---|---|
| 《核心信号引擎总控》 | §1-3（改写）、§4 全部（删持仓监控/额度预留部分）、§5 全部、§6.1-6.8、§7.1-7.2（仅候选/信号）、§8（Telegram 框架，删交易命令）、§10（存储与结果标签，删订单/仓位表）、§11（回测引擎，删晋升门禁）、§12（故障安全，删交易条目）、§13-15（改写） | §4.5 仓位监控/动态额度账本、§6.9、§6.10、§7.3-7.6、§9 全部、§10.4 保护集、§11.5-11.6 晋升/回退机器 |
| 《插件 SDK 规范》（新增） | — | — |
| 《交易插件文档》（新增，含简化 DBotX 契约） | §6.9 风控/执行字段、§7.3 ENTRY_GATE、§7.4-7.6 状态机（大幅简化）、§9 参数映射（重写）、§8.2 交易命令 | 本地 TP/SL 分档引擎、TAKE_PROFIT_BATCH、curl transport、PAPER 1m 模型、UNKNOWN 冻结状态机 |
| 《波段插件文档》（新增） | — | — |

理由：现文档的复杂度集中在"自建执行引擎"，其正确性论证（订单幂等、UNKNOWN 处理）在 DBotX 原生能力面前大部分失效；拆分后每份文档可独立评审。

备选：单文档重组（改动小但插件无稳定契约参照物）；被否。

### D2. 插件总线形态：进程内注册 + 三个接口

- 插件包在 `plugins/<name>/` 下，暴露 `register(app)` 入口；核心启动时按配置顺序加载
- 三个接口：`event.subscribe(handler)`、`notify.send(...)`、`commands.register(...)`
- 事件 Schema 版本化（`schema_version` 字段）；核心发布事件走内存队列，插件 handler 同步 await
- 插件表前缀约定：`<plugin_name>_`；插件迁移独立目录 `plugins/<name>/migrations/`
- 故障隔离：handler 调用包 try/except，连续失败熔断（配置阈值），重启解除

备选：独立进程/IPC——隔离更好但违背"简单易维护"，已由用户决策否决（Q1）。

### D3. 交易插件 DBotX 契约重写要点

| 主题 | 新设计 |
|---|---|
| Transport | `httpx.AsyncClient`，`X-API-KEY` 头；删除 curl 子进程机制。凭证仅存在于插件环境变量 |
| 开仓 | `POST /automation/swap_order`（LIVE）或 `/simulator/sim_swap_order`（PAPER），显式传 `stopEarnGroup`/`stopLossGroup`/`trailingStopGroup`/`pnlOrderExpire*`，`retries` 按 DBotX 原生语义配置 |
| 状态推进 | 订阅 `wss://api-bot-v1.dbotx.com/trade/ws/` 的 `subscribeTradeResults`（全部 swap 类型事件），30s 心跳；无本地轮询状态机 |
| 兜底对账 | 重启/断线时：`GET /automation/swap_orders?ids=` 批量查 + `GET /kline/wallet/assets`（含 `pnl/pnlPercent/tokenBalanceUI`）+ `/account/swap_trades` 重建 |
| 钱包验证 | `GET /account/wallets`（type=solana/evm）确认专用钱包存在且链类型匹配 |
| 风控账本 | 沿用现文档语义（敞口/日亏损/连续亏损/冷却/无自动加仓）但表在插件命名空间；UNKNOWN 订单处理简化为"WS 事件未达 + 查询兜底 + 管理员确认"，不再有自动冻结机 |
| 命令 | `/mode paper|live`、`/positions`、`/closeall` 经核心命令路由注册 |

备选：保留本地 TP/SL 引擎——与"直连 DBotX"决策冲突且复杂度高，被否（Q5）。

### D4. 核心删除项的技术替代

- 持仓 G3 监控 + `dynamic_position_reserve_credits` → 核心不再有持仓概念，§4.6 额度账本仅保留发现/候选/回补的准入与预留
- PAPER 本地 1m 模型 → 只在回测引擎保留（离线确定性），实时 PAPER 不存在于核心
- 结果标签追踪：`simple token_price` 或 Multiple Pools 低频 REST 轮询（15m/1h/6h/24h 定点），不占 WS 订阅；优先用 `simple token_price` 批量接口降成本，契约测试确认可用性后锁定
- §6.5 trade_flow：修正"无 USD 聚合"的过时声明，标注 Multiple Pools 已提供 buy/sell/net buy volume（m5~h24）字段现状；新增净买入金额类特征留作后续独立变更（需回测证明 + 手动激活），本变更只修声明不加特征

### D5. 回测简化

保留：数据准备（历史 OHLCV 回补计划+管理员确认）、1m 成交模型、结果标签、覆盖率报告、run_id 确定性。
删除：`promotion_gates` 字段、70/30 样本外划分、baseline/candidate 自动对比、`PROTECTED` 保护集管理、采集专用 dry-run 激活路径（保留 schema 校验与手动激活）。
激活流程变为：`编辑 strategy.yaml → Schema 校验 → 回测报告 → 管理员确认 → ACTIVE 快照`。

### D6. 波段插件第一版形态

- 行情：DBotX `/kline/chart`（计算指标用 1m/5m/1h/4h）+ Pair Updates WS（实时价）
- 触发：DBotX Limit Order `settings[]`（triggerPriceUsd/triggerDirection/useMidPrice/expireExecute）
- 循环：`/kline/chart` 定箱体 → 挂单 → WS `limit_buy_success` 确认 → 挂卖单 → 循环；箱体破坏/安全恶化终止
- 客户端指标引擎（RSI/布林带等）列为 backlog，不进第一版

## Risks / Trade-offs

- [风险] DBotX WS 结果事件可能漏发（断线窗口） → 缓解：插件维持"事件驱动 + 定时批量查询兜底"双通道，幂等键为远端订单 ID
- [风险] 进程内插件 bug 拖垮核心 → 缓解：事件 handler 包裹隔离 + 熔断；插件禁止阻塞调用（写进 SDK 规范）
- [风险] `simple token_price` 的额度语义与批量上限未经契约测试 → 缓解：结果追踪先以 Multiple Pools 实现（已锁定契约），simple 接口作为验证后优化
- [风险] DBotX 文档部分能力（Simulator list_tasks、kline 字段单位）只有代码示例证据 → 缓解：交易插件文档标记为"待契约测试锁定"，正式契约以实测为准
- [权衡] 服务端 TP/SL 使实盘执行与回测 1m 模型脱钩 → 接受：回测只评价信号质量，实盘执行效果由插件自身的交易记录统计验证
- [权衡] 核心删除风控后，信号"能不能买"与"买不买"分离 → 接受：核心保证信号质量，插件保证资金安全，这正是本次重构的目的

## Migration Plan

两阶段串行交付，阶段间有验收闸门：

1. **第一阶段（本变更先行实施范围）**：以现文档为底稿产出逐节归属总表并与用户确认 → 写《核心信号引擎总控》（含插件总线事件 Schema v1，见 D2）→ 按 `signal-core` spec 全部场景验收；此阶段现总控文档保持原样不动
2. **第二阶段（第一阶段验收通过后才启动，不并行）**：写《插件 SDK 规范》（引用核心事件 Schema）→ 写《交易插件文档》《波段插件文档》→ 按对应 spec 场景验收
3. **最终验收**：四份文档互引无循环、无孤儿引用、术语一致；旧总控文档标记废弃并保留备份
4. **回滚**：任何阶段未验收前，现总控文档保留原样不动

## Open Questions

- 交易插件的服务端 TP/SL 组默认参数值（第一版用策略文件占位，回测调参后锁定）
