# SOL/BSC 潜力币信号引擎——波段插件文档

| 项目 | 规范 |
|---|---|
| 文档性质 | 波段插件的独立设计、开发、测试与验收规范 |
| 上游契约 | 核心事件 Schema v1、插件 SDK 规范 |
| 行情与执行供应商 | DBotX Data API（Kline/Pair Info/WS）与 DBotX Limit Order 任务 |
| 凭证 | DBotX API Key、波段钱包，全部由插件自持 |

---

## 1. 定位与边界

波段插件订阅核心 `signal_created` 事件作为波段候选来源，在单币内进行多次低买高卖循环：

- 核心对信号之后的波段行为不感知、不干预；`signal_invalidated` 不影响已建立的波段候选与循环，候选退出只由插件自身的终止条件决定。
- 波段循环不影响核心信号与交易插件的状态；两个插件各自独立决策与执行。
- 波段候选只是候选：插件自行评估是否进入循环，不由核心信号等级强制决定。

## 2. 凭证与环境

- `DBOTX_API_KEY` 由插件自持；波段使用专属 DBotX 钱包，默认与交易插件隔离；显式配置共享钱包时才共用，且盈亏账本仍分开记录。
- 凭证缺失或波段钱包不存在时插件进入禁用状态并告警。

## 3. 行情数据源

### 3.1 Kline（规则指标计算）

- `GET https://api-data-v1.dbotx.com/kline/chart?chain=&pair=&interval=&end=`
- `interval`：`1s/30s/1m/5m/15m/30m/1h/4h/12h/1d`；每次返回 `end` 之前 100 条，用本页最早 `time - 1` 向前翻页取历史。
- 用途：1h/4h 定箱体，1m/5m 做触发确认。字段单位（volume 币种）以第 7 章契约测试结论为准。
- 插件自行管理与限频：数据 API 额度由插件自理，核心无感知。

### 3.2 Pair Updates WS（实时价格观察）

- 订阅目标 pair 的价格变化与数据更新通知；订阅 0 credits，更新通知 1–5 credits。
- 用于箱体边界临近判断与触发辅助；触发执行本身由 Limit Order 服务端完成，WS 只做观察与状态判断。

### 3.3 安全信息

- 初始安全以 `signal_created` 载荷中的 `security_snapshot` 为准。
- 持续安全来源：DBotX Pair Info 的安全字段（mint/freeze authority、top10 持有率等）；插件不依赖核心在信号之后提供安全复检。

## 4. 服务端触发映射（第一版强制）

波段买卖的触发执行必须使用 DBotX Limit Order 服务端任务，禁止自建客户端轮询成交引擎：

- `POST https://api-bot-v1.dbotx.com/automation/limit_orders`
- `settings[]` 每个元素一个买/卖任务：

| 字段 | 映射 | 说明 |
|---|---|---|
| `enabled` | `true` | |
| `tradeType` | `buy` / `sell` | |
| `triggerPriceUsd` | 箱体下沿/上沿价位 | 触发价（USD 字符串） |
| `triggerDirection` | `down`（下沿买）/ `up`（上沿卖） | |
| `currencyAmountUI` | 波段单笔金额/卖出比例 | buy 为原生币金额；sell 为 0–1 |
| `useMidPrice` | 插件配置 | Anti-Spike 1 秒中间价触发，默认开启 |
| `expireDelta` | 插件配置 | 任务时长（ms），未触发自动过期 |
| `expireExecute` | `false`（第一版） | 过期不强制市价执行 |
| `maxSlippage` | 插件配置 | 0–1 |
| 费用字段 | 同交易插件映射 | priorityFee/gasFeeDelta/maxFeePerGas/jitoEnabled/jitoTip |
| `concurrentNodes`/`retries` | 插件配置 | |

- 任务由插件按仓位状态创建、取消或替换；成交结果经 DBotX WS `limit_buy_success/limit_buy_fail/limit_sell_success/limit_sell_fail` 事件确认。

## 5. 循环状态机

### 5.1 箱体判定

- 用 1h/4h K 线确定震荡区间：近 N 根 K 线的支撑/阻力或布林带上下轨；箱体有效条件（区间幅度、成交量下限）由插件配置定义。
- 箱体判定失败（单边趋势、区间过窄）时该候选不进入循环。

### 5.2 循环状态

每个代币维护：

```text
CANDIDATE / WAIT_BUY / HOLDING / WAIT_SELL / TERMINATED
```

- `CANDIDATE`：收到 `signal_created`，评估箱体；失败或拒绝即终止。
- `WAIT_BUY`：已挂买单（direction=down）；`limit_buy_success` 确认后进入 `HOLDING`。
- `HOLDING`：评估卖出点；到达上沿/目标收益后挂卖单（direction=up）进入 `WAIT_SELL`。
- `WAIT_SELL`：`limit_sell_success` 确认后结算本轮盈亏；轮次未达上限且箱体仍有效则回到 `WAIT_BUY` 开启下一轮。
- `TERMINATED`：达到终止条件，取消该代币全部未触发任务，记录结果。

### 5.3 终止条件

任一触发即自动终止该代币的波段循环：

- 最大轮数达到上限。
- 箱体破坏：价格突破箱体边界且超过终止阈值。
- 流动性崩塌：Pair Info 或 Kline 反映流动性低于下限。
- 安全恶化：Pair Info 检测到 mint/freeze authority 出现、top10 集中度超过阈值等。
- 管理员显式终止命令。

## 6. 资金隔离与记账

- 默认使用波段专属钱包；显式配置共享钱包时，盈亏与任务仍按插件独立记录，不与交易插件账本混算。
- 波段插件表（`swing_` 前缀）：`swing_candidates`（候选与箱体参数）、`swing_cycles`（循环状态与轮次）、`swing_trades`（每轮买卖、价格、盈亏）。
- 波段资金上限与单币仓位上限由插件配置定义，创建买入任务前在同一事务内检查预留。

## 7. 待契约测试项

- `/kline/chart` 的 volume 单位、`end` 分页边界与时间精度。
- Pair Updates WS 的通知字段结构与计费确认。
- Limit Order `settings[]` 多任务创建/取消/替换行为与 `limit_*_success/fail` 事件字段。
- Pair Info 安全字段与核心安全快照的口径差异处理。

正式契约以实测为准，本文档与实现随测试结论更新。
