# signal-core

## Purpose

定义核心信号引擎的行为边界：发现、安检、评分、防追高、信号推送、结果追踪与回测；核心是纯信号系统，永不执行或管理交易。

## ADDED Requirements

### Requirement: 核心职责边界
核心 SHALL 只承担信号相关职责：候选发现、安全筛选、评分、防追高、信号落库、Telegram 推送、信号后结果追踪与回测。核心 MUST NOT 持有任何交易供应商凭证（DBotX API Key、钱包 ID）、MUST NOT 调用任何交易执行 API、MUST NOT 管理订单或仓位状态。

#### Scenario: 交易职责缺失
- **WHEN** 检查核心的运行环境变量与代码依赖
- **THEN** 不存在 DBotX 交易凭证配置，不存在交易执行 API client，不存在订单/仓位表

### Requirement: 信号生成流水线
核心 SHALL 按固定顺序生成信号：CoinGecko 发现（Megafilter/Trending/New Pools 四入口与三标签）→ CoinGecko/GoPlus 安全门禁 → 评分与防追高 → 信号落库 → Telegram 推送。安全危险或未知时 MUST 拒绝开仓信号；GoPlus PRE_EXECUTION_CHECK 必须通过后才允许发送买入信号。

#### Scenario: 安全未知时拒绝信号
- **WHEN** 候选的安全字段存在 UNKNOWN 状态
- **THEN** 该候选不产生买入信号，仅可继续观察

### Requirement: 信号事件发布
核心 SHALL 在信号生命周期关键节点通过插件总线发布事件：信号创建时发布 `signal_created`，信号失效或作废时发布 `signal_invalidated`。事件载荷 MUST 包含 `schema_version`、`signal_id`、链、代币地址、决定池地址、信号等级、总分、参考价、安全快照、有效期、策略 revision；事件 Schema MUST 版本化。

#### Scenario: 信号创建发布
- **WHEN** 一个候选通过全部门禁并生成买入信号
- **THEN** 插件总线收到一条 `signal_created` 事件，且载荷包含最小契约要求的全部字段

### Requirement: 信号后结果追踪
核心 SHALL 在信号发出后以低频 REST 轮询记录该代币 15 分钟、1 小时、6 小时、24 小时的价格、流动性结果标签，用于信号质量统计与回测。结果追踪 MUST NOT 占用 G1/G3 实时订阅。

#### Scenario: 结果标签落库
- **WHEN** 一条信号发出后经过 24 小时
- **THEN** 该信号拥有完整的 15m/1h/6h/24h 结果标签记录，且追踪过程未订阅任何实时频道

### Requirement: 回测与策略激活
核心 SHALL 保留回测引擎：本地 1m 成交模型、历史 OHLCV 回补、覆盖率报告。策略激活 MUST 为管理员手动决策：回测报告生成后由管理员执行激活或回退，MUST NOT 存在自动晋升门禁、样本外自动对比或动态保护集机制。

#### Scenario: 手动激活
- **WHEN** 管理员查看回测报告并决定激活新策略
- **THEN** 策略快照写入 ACTIVE，且不存在任何自动晋升指标阻止或触发该操作

### Requirement: 核心功能冻结
核心的数据源集合（CoinGecko 为主、GoPlus 为安全补充）与评分维度集合在重构后 MUST 冻结：新增数据源或评分维度 MUST 先由回测证明信号质量提升，经管理员确认后通过正式变更流程进入核心。

#### Scenario: 拒绝未经证明的扩展
- **WHEN** 有人提议在核心发现层引入新数据源且无回测证据
- **THEN** 该提议只能进入插件或 backlog，不能直接修改核心契约

### Requirement: Telegram 统一管理
核心 SHALL 独占 Telegram 机器人接入：bot token、投递幂等（outbox/delivery_key）、二次确认机制均在核心实现。插件 MUST NOT 自行连接 Telegram，只能通过核心的通知 API 发送消息、通过命令路由注册并接收命令。

#### Scenario: 插件通过核心发送通知
- **WHEN** 插件需要向管理员发送一条成交通知
- **THEN** 该通知经核心通知 API 进入统一 outbox，投递状态由核心管理
