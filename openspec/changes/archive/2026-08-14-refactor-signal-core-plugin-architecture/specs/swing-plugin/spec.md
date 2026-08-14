# swing-plugin

## Purpose

定义波段插件的独立行为契约：以核心信号为候选入口，使用 DBotX 自有行情与 Limit Order 服务端触发，在单币内进行多次低买高卖循环。

## ADDED Requirements

### Requirement: 行情自取
波段插件 SHALL 使用 DBotX 数据 API 自取行情：`/kline/chart`（1s/30s/1m~1d OHLCV）用于规则指标计算，Pair Updates WS 用于实时价格观察。插件 MUST NOT 占用核心的 G1/G3 订阅，MUST NOT 依赖核心内部行情状态。

#### Scenario: 行情独立获取
- **WHEN** 波段插件需要某币的 5m K 线序列
- **THEN** 插件自行调用 DBotX Kline API 取得，核心订阅集合无变化

### Requirement: 信号作为候选入口
波段插件 SHALL 订阅核心 `signal_created` 事件作为波段候选来源，并自行决定是否进入波段循环。核心对已发布信号之后的波段行为 MUST 不感知、不干预；波段循环 MUST 不影响核心信号与交易插件的状态。波段候选 MUST 独立于信号生命周期管理：`signal_invalidated` 事件 MUST NOT 影响已建立的波段候选与循环，候选的退出只由插件自身的终止条件决定。

#### Scenario: 波段与交易插件共存
- **WHEN** 同一信号同时被交易插件与波段插件订阅
- **THEN** 两个插件各自独立决策与执行，互不感知，核心无任何联动逻辑

### Requirement: 服务端触发优先
波段插件的第一版 MUST 使用 DBotX Limit Order 服务端触发执行买卖（`triggerPriceUsd` + `triggerDirection` + `useMidPrice` + `expireExecute`），MUST NOT 自行构建客户端轮询成交引擎。插件根据仓位状态创建、取消或替换 Limit Order 任务。

#### Scenario: 服务端挂单循环
- **WHEN** 波段插件在候选币的箱体下沿判定买入点
- **THEN** 插件创建方向为 down 的买入 Limit Order 任务，触发与成交由 DBotX 服务端完成

### Requirement: 循环状态机
波段插件 SHALL 维护每个代币的波段循环状态：持有/空仓、当前网格档位、已实现盈亏。每轮卖成交后 MUST 经 DBotX WS 结果确认再进入下一轮；循环终止条件（最大轮数、箱体破坏、流动性崩塌、安全恶化）MUST 定义明确并自动终止该代币的波段循环。安全状态的持续来源 MUST 为 DBotX 数据 API 的 Pair Safety Info（`pair_info`），初始安全以信号事件载荷中的 `security_snapshot` 为准；插件 MUST NOT 依赖核心在信号之后提供安全复检。

#### Scenario: 箱体破坏终止
- **WHEN** 处于循环中的代币价格突破箱体边界且超过终止阈值
- **THEN** 插件取消该代币全部未触发任务，结束循环并记录结果

#### Scenario: 安全恶化终止
- **WHEN** 循环中的代币经 Pair Safety Info 检测到 mint/freeze authority 出现或 top10 集中度超过插件阈值
- **THEN** 插件取消该代币全部未触发任务并终止循环，不依赖核心提供该安全数据

### Requirement: 风控与记账
波段插件 SHALL 使用自己的命名空间表记录每轮买卖与盈亏，MUST 遵守自身仓位上限与循环资金上限。波段钱包/资金 MUST 与交易插件隔离或显式配置共享，默认 MUST 为隔离。

#### Scenario: 资金隔离默认
- **WHEN** 波段插件创建第一个买入任务
- **THEN** 任务使用波段专属钱包（或显式配置的共享钱包），不与交易插件账本混算
