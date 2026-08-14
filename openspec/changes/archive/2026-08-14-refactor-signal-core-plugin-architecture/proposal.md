# 提案：核心专注信号引擎，交易拆为插件，文档一拆为三

## Why

现有《SOL-BSC-TGBOT-总控开发文档》把"潜力信号发现/筛选/推送"和"模拟/实盘交易执行"耦合在单一系统里，约 40% 篇幅（订单状态机、本地止盈止损引擎、PAPER 成交模型、钱包对账、curl transport）是自建执行机器。而 DBotX 官方 API 已原生提供服务端分档止盈/止损/追踪止损、WS 成交结果推送、自带 PnL 的钱包资产对账、全类型订单的 Simulator，且交易 API 全部 0 credits——这些自建机器大部分不需要存在。项目真正重要的是信号质量；交易（含未来的波段交易）应作为插件独立开发、独立演进。

## What Changes

- **BREAKING**：核心职责收缩为"信号引擎"——发现、安检、评分、防追高、信号落库、Telegram 推送、结果标签追踪、回测。核心永不调用交易 API，不持有 DBotX Key/钱包配置。
- **BREAKING**：删除核心中的本地执行机器：TP/SL 分档引擎（TAKE_PROFIT_BATCH 机制）、订单轮询/UNKNOWN 冻结状态机、DBotX curl 子进程 transport、PAPER 本地 1m 成交模型、Simulator 人工对账命令（`/paper reconcile`、`/order resolve`）、`/closeall`、持仓 G3 监控与 `dynamic_position_reserve_credits`。
- **BREAKING**：回测引擎保留，但删除自动晋升门禁（promotion_gates、70/30 样本外对比、PROTECTED 保护集），改为"回测报告 + 管理员手动 `/strategy activate`"。
- 新增插件总线：进程内插件注册、`signal_created` / `signal_invalidated` 最小事件契约、通知 API、Telegram 命令路由。
- 新增交易插件：订阅信号 → DBotX Fast Swap（服务端 TP/SL 组）→ WS 结果订阅记账 → 风险账本（单币/链/全局敞口、日亏损、连续亏损、无自动加仓）。PAPER 直连 DBotX Simulator。
- 新增波段插件：基于 DBotX Kline/WS 行情与服务端 Limit Order 触发的单币内低买高卖循环。
- 修正过时契约：删除"CoinGecko 无买卖 USD 聚合"的过时断言，标注 Multiple Pools 已提供 `buy_volume_usd/sell_volume_usd/net_buy_volume_usd` 字段现状；新增净买入金额类评分特征留作后续独立变更（需回测证明后经手动激活进入）。
- 文档拆分：现 1347 行总控文档重组为《核心信号引擎总控》《插件 SDK 规范》与各插件独立文档。
- **交付节奏**：分两个阶段串行交付，不并行开工。第一阶段只交付《核心信号引擎总控》（含插件总线事件 Schema v1）并完成验收；《插件 SDK 规范》、交易插件与波段插件文档作为第二阶段，在第一阶段验收通过后单独启动。

## Capabilities

### New Capabilities

- `signal-core`: 核心信号引擎的行为契约（发现、安检、评分、防追高、信号、推送、结果追踪、回测），由现总控文档瘦身而成
- `plugin-sdk`: 插件 SDK 规范（事件契约 Schema、注册与生命周期、通知 API、命令路由、插件表命名空间）
- `trade-plugin`: 交易插件文档（DBotX 执行契约、WS 结果处理、风险账本、Telegram 命令）
- `swing-plugin`: 波段插件文档（行情数据源、触发规则、Limit Order 任务管理）

### Modified Capabilities

无（仓库尚无既有 openspec 规格；现总控文档的变更全部反映在新增的 `signal-core` 能力规格中）。

## Impact

- 文档：`SOL-BSC-TGBOT-总控开发文档.md` 拆分重写；新增插件 SDK 与插件文档
- 代码：无既有代码（项目处于文档阶段）；后续实现按新文档结构 `app/core/`、`plugins/trade/`、`plugins/swing/`
- 外部依赖：核心依赖不变（CoinGecko Analyst、GoPlus）；DBotX 依赖从核心移至交易/波段插件
- 额度：CoinGecko credits 全部用于发现/监控/回测（删除持仓预留账本）；DBotX 交易 0 credits、数据 API 由插件自理
